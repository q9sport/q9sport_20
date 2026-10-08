# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify bulk exports, used to import a whole catalogue, or one very large
product, at once.

Shopify runs the export on its side and publishes a JSON lines file. The file
is downloaded into the Odoo data directory in passes that resume from the
bytes already saved, and it is only used once it holds as many rows as Shopify
reported, so a connection that closed early never passes for a complete file.
The file is then read in passes that resume from a byte offset. Products are
packed with their variants into import chunks sized by rows, while only a
bounded number of rows is held in memory. Shopify only promises that a child
row comes after its parent, so a variant can arrive after its product was
already packed; such a product is imported again through the API, so nothing
is lost. A chunk never overwrites a product that a newer update already
imported, and never brings back a product deleted after the export started.
An export that stops making progress is closed, and a manager can stop one.
"""
import datetime
import json
import logging
import os
import tempfile
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

from .. import compat
from ..lib import documents, errors, util
from .eh_shopify_product import DEFER_IMAGES, _parse_dt

_logger = logging.getLogger(__name__)

CHUNK_ROOTS = 200
CHUNK_ROWS = 2000
OPEN_ROWS_LIMIT = 20000
PASS_BUDGET = 50.0
CHUNK_BUDGET = 50.0
DOWNLOAD_BUDGET = 45.0
SHORT_DOWNLOAD_RETRIES = 3
POLL_DELAYS = (30, 60, 120)
MAX_POLLS = 3000
DOWNLOAD_TIMEOUT = (10, 300)
KINDS = {'products': documents.BULK_PRODUCTS, 'product_ids': documents.BULK_PRODUCT_IDS,
         'product': documents.BULK_ONE_PRODUCT, 'match': documents.BULK_PRODUCTS,
         'order_ids': documents.BULK_ORDER_IDS, 'customers': documents.BULK_CUSTOMERS}
SEEN_BATCH = 1000
SWEEP_EVERY = datetime.timedelta(hours=24)
STUCK_AFTER = datetime.timedelta(hours=2)
ACTIVE_STATES = ('running', 'reading', 'importing')


def _count_rows(path):
    rows, last = 0, b'\n'
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            rows += block.count(b'\n')
            last = block[-1:]
    return rows + (0 if last == b'\n' else 1)


class ShopifyBulk(models.Model):
    _name = 'eh.shopify.bulk'
    _description = 'Shopify Export'
    _order = 'id desc'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    kind = fields.Selection([('products', 'All products'), ('product_ids', 'Deleted products check'),
                             ('product', 'One large product'), ('match', 'Catalogue check'),
                             ('order_ids', 'Order history'), ('customers', 'Customers')], required=True,
                            readonly=True)
    product_gid = fields.Char('Shopify product', readonly=True, index=True)
    last_known_product_id = fields.Integer('Last product read', readonly=True)
    operation_gid = fields.Char('Shopify export ID', readonly=True, index=True)
    status = fields.Char('Status in Shopify', readonly=True)
    error_code = fields.Char(readonly=True)
    object_count = fields.Integer('Rows', readonly=True)
    root_object_count = fields.Integer('Records', readonly=True)
    file_size = fields.Float(readonly=True)
    url = fields.Char('Result file address', readonly=True)
    partial = fields.Boolean('Partial export', readonly=True)
    state = fields.Selection([
        ('running', 'Running in Shopify'),
        ('reading', 'Reading the export'),
        ('importing', 'Importing'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], default='running', required=True, readonly=True, index=True)
    note = fields.Char(readonly=True)
    lines_read = fields.Integer(readonly=True)
    read_offset = fields.Float(readonly=True, digits=(20, 0))
    download_resumable = fields.Boolean(default=True, readonly=True)
    download_attempts = fields.Integer(readonly=True)
    open_roots = fields.Json(readonly=True)
    late_gids = fields.Json(readonly=True)
    poll_count = fields.Integer(readonly=True)
    pass_count = fields.Integer(readonly=True)
    chunks_enqueued = fields.Integer('Chunks', readonly=True)
    chunks_done = fields.Integer('Chunks imported', compute='_compute_progress')
    started_at = fields.Datetime('Started', readonly=True)
    completed_at = fields.Datetime('Finished in Shopify', readonly=True)

    def _chunk_domain(self):
        self.ensure_one()
        return [('store_id', '=', self.store_id.id), ('job_type', '=', 'bulk.chunk'),
                ('dedup_key', '=like', 'bulk:%s:%%' % self.id)]

    def _live_job_domain(self):
        self.ensure_one()
        return [('store_id', '=', self.store_id.id), ('state', 'in', ('pending', 'running')), '|',
                ('resource_key', '=', 'bulk:%s' % self.id), ('dedup_key', '=like', 'bulk:%s:%%' % self.id)]

    def _compute_progress(self):
        Job = self.env['eh.shopify.job'].sudo()
        for bulk in self:
            bulk.chunks_done = Job.search_count(bulk._chunk_domain() + [('state', '=', 'done')]) if bulk.id else 0

    # ------------------------------------------------------------------ scheduling
    def _enqueue_poll(self, delay=0):
        self.ensure_one()
        self.poll_count += 1
        return self.env['eh.shopify.job'].sudo()._enqueue(
            self.store_id, 'bulk.poll', name=_('Check Shopify export %s') % self.id, payload={'bulk_id': self.id},
            dedup_key='bulk.poll:%s:%s' % (self.id, self.poll_count), resource_key='bulk:%s' % self.id, priority=7,
            run_at=fields.Datetime.now() + datetime.timedelta(seconds=delay))

    def _enqueue_pass(self):
        self.ensure_one()
        self.pass_count += 1
        return self.env['eh.shopify.job'].sudo()._enqueue(
            self.store_id, 'bulk.read', name=_('Read Shopify export %s') % self.id, payload={'bulk_id': self.id},
            dedup_key='bulk.read:%s:%s' % (self.id, self.pass_count), resource_key='bulk:%s' % self.id, priority=7)

    # ------------------------------------------------------------------ jobs
    def _job_bulk_start(self, job):
        store = job.store_id
        payload = job.payload or {}
        kind = payload.get('kind') or 'products'
        query = self._bulk_query(store, kind, payload)
        gid = payload.get('gid') if kind == 'product' else False
        client = store._client()
        try:
            result = client.mutate(store._doc_text(documents.BULK_RUN_QUERY), {'query': query},
                                   documents.BULK_RUN_QUERY.payload_key)
        except errors.UserErrors as error:
            raise errors.TransientError(_('Shopify did not start the export yet: %s') % error.message, retry_after=300)
        operation = result.get('bulkOperation') or {}
        self.env.cr.execute("SELECT COALESCE(MAX(id), 0) FROM eh_shopify_product WHERE store_id = %s", (store.id,))
        bulk = self.create({'store_id': store.id, 'kind': kind, 'operation_gid': operation.get('id'),
                            'status': operation.get('status'), 'started_at': fields.Datetime.now(),
                            'product_gid': gid, 'last_known_product_id': self.env.cr.fetchone()[0]})
        bulk._enqueue_poll(delay=POLL_DELAYS[0])
        return {'bulk': bulk.id, 'operation': operation.get('id')}

    @api.model
    def _bulk_query(self, store, kind, payload):
        """Query text for an export of ``kind``, with its parameters filled in."""
        query = store._doc_text(KINDS[kind])
        if kind == 'product':
            gid = payload.get('gid') or ''
            legacy = gid.rsplit('/', 1)[-1]
            if not legacy.isdigit():
                raise errors.MappingError(_('%s is not a Shopify product.') % gid)
            query = query.replace('"id:0"', '"id:%s"' % legacy, 1)
        return query

    @api.model
    def _start_product_export(self, store, gid, requested_at=None):
        """Import one product with too many variants for the API through an
        export. An export of it that started after the request covers it."""
        running = self.search([('store_id', '=', store.id), ('kind', '=', 'product'), ('product_gid', '=', gid),
                               ('state', '=', 'running')], order='id desc', limit=1)
        if running and requested_at and running.started_at and running.started_at >= requested_at:
            return {'export_running': running.id}
        self.env['eh.shopify.job'].sudo()._enqueue(
            store, 'bulk.start', name=_('Export large product %s from Shopify') % gid.rsplit('/', 1)[-1],
            payload={'kind': 'product', 'gid': gid}, dedup_key='bulk.start:product:%s' % gid, priority=7)
        return {'exporting': gid}

    def _job_bulk_poll(self, job):
        bulk = self.browse((job.payload or {}).get('bulk_id')).exists()
        if not bulk or bulk.state != 'running':
            return {'skipped': 'the export is not waiting'}
        store = bulk.store_id
        data = store._client().execute(store._doc_text(documents.BULK_STATUS), {'id': bulk.operation_gid}).data or {}
        operation = data.get('bulkOperation') or {}
        status = operation.get('status')
        vals = {'status': status, 'error_code': operation.get('errorCode'),
                'object_count': int(operation.get('objectCount') or 0),
                'root_object_count': int(operation.get('rootObjectCount') or 0),
                'file_size': float(operation.get('fileSize') or 0)}
        if status in ('CREATED', 'RUNNING', 'CANCELING'):
            if bulk.poll_count >= MAX_POLLS:
                vals.update({'state': 'failed', 'note': _('Shopify did not finish the export in time.')})
                bulk.write(vals)
                return {'failed': bulk.id}
            bulk.write(vals)
            bulk._enqueue_poll(delay=POLL_DELAYS[min(bulk.poll_count, len(POLL_DELAYS)) - 1])
            return {'status': status}
        now = fields.Datetime.now()
        if status == 'COMPLETED':
            if not operation.get('url'):
                vals.update({'state': 'done', 'completed_at': now, 'note': _('Shopify had nothing to export.')})
                bulk.write(vals)
                return {'done': bulk.id}
            vals.update({'url': operation['url'], 'state': 'reading', 'completed_at': now})
            bulk.write(vals)
            bulk._enqueue_pass()
            return {'reading': bulk.id}
        note = _('Shopify stopped the export (%s).') % (operation.get('errorCode') or status or '').replace('_', ' ').lower()
        if operation.get('partialDataUrl'):
            vals.update({'url': operation['partialDataUrl'], 'partial': True, 'state': 'reading', 'completed_at': now,
                         'note': note + ' ' + _('The part already exported is imported; run the import again for '
                                                'the rest.')})
            bulk.write(vals)
            bulk._enqueue_pass()
            return {'partial': bulk.id}
        vals.update({'state': 'failed', 'completed_at': now, 'note': note})
        bulk.write(vals)
        return {'failed': bulk.id}

    def _file_path(self):
        self.ensure_one()
        if compat.in_test_mode(self.env):
            base = tempfile.gettempdir()
        else:
            base = os.path.join(config['data_dir'], 'eh_shopify_bulk', self.env.cr.dbname)
        os.makedirs(base, exist_ok=True)
        created = int(self.create_date.timestamp()) if self.create_date else 0
        return os.path.join(base, '%s-bulk-%s-%s.jsonl' % (self.env.cr.dbname, self.id, created))

    def unlink(self):
        """The downloaded file goes with the record, so deleting a store leaves nothing behind."""
        self._remove_files()
        return super().unlink()

    def _remove_files(self):
        for bulk in self:
            path = bulk._file_path()
            for name in (path, path + '.part'):
                try:
                    os.remove(name)
                except OSError:
                    pass

    def _fail(self, note):
        for bulk in self:
            bulk.write({'state': 'failed', 'note': note})
            bulk._remove_files()

    def _ensure_file(self):
        """Download the export in passes. Returns the path once the whole file
        is saved, or None while another pass has to continue."""
        self.ensure_one()
        path = self._file_path()
        if os.path.exists(path):
            return path
        partial_path = path + '.part'
        offset = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0
        deadline = time.monotonic() + DOWNLOAD_BUDGET if self.download_resumable else None
        with open(partial_path, 'ab') as handle:
            outcome = self.store_id._transport().download_to(self.url, handle, offset, DOWNLOAD_TIMEOUT, deadline)
        if outcome.get('restart') and self.download_resumable:
            # The server ignored the byte range, so later passes cannot resume
            # and download in one go instead.
            self.download_resumable = False
        if not outcome.get('complete'):
            return None
        rows = _count_rows(partial_path)
        if not self.partial and self.object_count and rows < self.object_count:
            os.remove(partial_path)
            self.download_attempts += 1
            if self.download_attempts >= SHORT_DOWNLOAD_RETRIES:
                raise errors.BulkOperationError(_('the file ended after %(rows)s of %(total)s rows') % {
                    'rows': rows, 'total': self.object_count})
            _logger.info('Shopify export %s downloaded %s of %s rows, downloading again', self.id, rows,
                         self.object_count)
            return None
        os.replace(partial_path, path)
        return path

    def _job_bulk_read(self, job):
        bulk = self.browse((job.payload or {}).get('bulk_id')).exists()
        if not bulk or bulk.state != 'reading':
            return {'skipped': 'the export is not being read'}
        started = time.monotonic()
        try:
            path = bulk._ensure_file()
        except errors.BulkOperationError as error:
            bulk._fail(_('The export file could not be downloaded (%s). Run the import again.') % error.message)
            return {'failed': bulk.id}
        if not path:
            bulk._enqueue_pass()
            return {'downloading': bulk.id}
        if bulk.kind == 'product_ids':
            return bulk._read_product_ids(path, started)
        open_roots = {root['id']: root for root in bulk.open_roots or []}
        open_rows = sum(1 + len(root.get('_children') or []) for root in open_roots.values())
        late = set(bulk.late_gids or [])
        chunk_index = bulk.chunks_enqueued
        lines_read = bulk.lines_read
        offset = int(bulk.read_offset or 0)
        finished = True
        with open(path, 'rb') as handle:
            handle.seek(offset)
            processed = 0
            for raw in handle:
                # Every pass reads at least one line, so a slow download that
                # used up the budget can never leave the export stuck.
                if processed and time.monotonic() - started > PASS_BUDGET:
                    finished = False
                    break
                processed += 1
                offset += len(raw)
                lines_read += 1
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    bulk._fail(_('Line %s of the export file could not be read. Run the import again.') % lines_read)
                    return {'failed': bulk.id}
                parent = row.pop('__parentId', None)
                if parent is None:
                    row['_children'] = []
                    open_roots[row['id']] = row
                    open_rows += 1
                elif parent in open_roots:
                    open_roots[parent]['_children'].append(row)
                    open_rows += 1
                else:
                    late.add(parent)
                if open_rows >= OPEN_ROWS_LIMIT:
                    chunk_index, emitted = bulk._emit_chunk(open_roots, chunk_index)
                    open_rows -= emitted
        if not finished:
            bulk.write({'lines_read': lines_read, 'read_offset': offset, 'open_roots': list(open_roots.values()),
                        'late_gids': sorted(late), 'chunks_enqueued': chunk_index})
            bulk._enqueue_pass()
            return {'lines_read': lines_read, 'more': True}
        while open_roots:
            chunk_index, _emitted = bulk._emit_chunk(open_roots, chunk_index)
        bulk._handle_late(sorted(late))
        bulk.write({'lines_read': lines_read, 'read_offset': offset, 'open_roots': [], 'late_gids': [],
                    'chunks_enqueued': chunk_index, 'state': 'importing' if chunk_index else 'done'})
        bulk._remove_files()
        return {'lines_read': lines_read, 'chunks': chunk_index, 'late': len(late)}

    def _handle_late(self, gids):
        """Products whose variant rows arrived after the product was packed."""
        Product = self.env['eh.shopify.product']
        for gid in gids:
            Product._enqueue_product_import(self.store_id, gid)

    def _read_product_ids(self, path, started):
        """Stamp every product binding the export lists; after a complete
        export, bindings that existed before it started and were not listed are
        marked removed. A partial or short export removes nothing."""
        self.ensure_one()
        cr = self.env.cr
        lines_read = self.lines_read
        offset = int(self.read_offset or 0)
        finished = True
        batch = []

        def stamp():
            if batch:
                cr.execute("UPDATE eh_shopify_product SET last_seen_export_id = %s "
                           "WHERE store_id = %s AND shopify_gid = ANY(%s)", (self.id, self.store_id.id, list(batch)))
                del batch[:]

        with open(path, 'rb') as handle:
            handle.seek(offset)
            processed = 0
            for raw in handle:
                if processed and time.monotonic() - started > PASS_BUDGET:
                    finished = False
                    break
                processed += 1
                offset += len(raw)
                lines_read += 1
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    stamp()
                    self._fail(_('Line %s of the product list could not be read, so nothing was marked '
                                 'removed.') % lines_read)
                    return {'failed': self.id}
                if '__parentId' not in row and row.get('id'):
                    batch.append(row['id'])
                if len(batch) >= SEEN_BATCH:
                    stamp()
        stamp()
        self.env['eh.shopify.product'].invalidate_model(['last_seen_export_id'])
        if not finished:
            self.write({'lines_read': lines_read, 'read_offset': offset})
            self._enqueue_pass()
            return {'lines_read': lines_read, 'more': True}
        complete = not self.partial and not (self.object_count and lines_read < self.object_count)
        removed = self.env['eh.shopify.product']
        if complete:
            removed = self.env['eh.shopify.product'].search([
                ('store_id', '=', self.store_id.id), ('state', '!=', 'removed'),
                ('id', '<=', self.last_known_product_id), ('last_seen_export_id', '!=', self.id)])
            removed._mark_removed()
        note = _('%s products no longer exist in Shopify.') % len(removed) if removed else False
        if not complete:
            note = _('The product list was incomplete, so nothing was marked removed.')
        self.write({'lines_read': lines_read, 'read_offset': offset, 'state': 'done', 'note': note})
        self._remove_files()
        return {'removed': len(removed)}

    @api.model
    def _start_sweep(self, store):
        if self.search_count([('store_id', '=', store.id), ('state', 'in', ACTIVE_STATES)]):
            return self.env['eh.shopify.job']
        store.sudo().products_swept_at = fields.Datetime.now()
        return self.env['eh.shopify.job'].sudo()._enqueue(
            store, 'bulk.start', name=_('Check Shopify for deleted products'), payload={'kind': 'product_ids'},
            dedup_key='bulk.start:product_ids', priority=9)

    def _emit_chunk(self, open_roots, chunk_index):
        """Queue the oldest open products as one chunk, bounded by products and
        by rows. Returns the next chunk index and the rows taken."""
        batch, rows = [], 0
        for gid in list(open_roots):
            size = 1 + len(open_roots[gid].get('_children') or [])
            if batch and (len(batch) >= CHUNK_ROOTS or rows + size > CHUNK_ROWS):
                break
            batch.append(open_roots.pop(gid))
            rows += size
        if batch:
            self.env['eh.shopify.job'].sudo()._enqueue(
                self.store_id, 'bulk.chunk', name=_('Import part %(part)s of Shopify export %(bulk)s') % {
                    'part': chunk_index + 1, 'bulk': self.id},
                payload={'bulk_id': self.id, 'kind': self.kind, 'roots': batch},
                resource_key='bulk:%s' % self.id,
                dedup_key='bulk:%s:%s' % (self.id, chunk_index), priority=8)
            chunk_index += 1
        return chunk_index, rows

    def _job_bulk_chunk(self, job):
        payload = job.payload or {}
        store = job.store_id
        bulk = self.browse(payload.get('bulk_id')).exists()
        Product = self.env['eh.shopify.product'].with_context(**{DEFER_IMAGES: True})
        counts = {'imported': 0, 'queued_again': 0, 'older': 0}
        roots = payload.get('roots') or []
        started = time.monotonic()
        for position, root in enumerate(roots):
            if position and time.monotonic() - started > CHUNK_BUDGET:
                rest = roots[position:]
                self.env['eh.shopify.job'].sudo()._enqueue(
                    store, 'bulk.chunk', name=job.name, payload=dict(payload, roots=rest),
                    dedup_key='%s.%s' % (job.dedup_key or 'bulk:%s:rest' % payload.get('bulk_id'), job.id),
                    priority=job.priority)
                counts['continued'] = len(rest)
                break
            product = {key: value for key, value in root.items() if key != '_children'}
            variants = sorted(root.get('_children') or [], key=lambda variant: variant.get('position') or 0)
            expected = (product.get('variantsCount') or {}).get('count')
            if expected is not None and expected != len(variants):
                Product._enqueue_product_import(store, product['id'])
                counts['queued_again'] += 1
                continue
            binding = Product.search([('store_id', '=', store.id), ('shopify_gid', '=', product['id'])], limit=1)
            updated = _parse_dt(product.get('updatedAt'))
            newer_import = updated and binding.shopify_updated_at and updated < binding.shopify_updated_at
            deleted_since = binding.removed_at and bulk and bulk.started_at and binding.removed_at >= bulk.started_at
            if binding and (newer_import or deleted_since):
                counts['older'] += 1
                continue
            product['variants'] = {'nodes': variants}
            try:
                with self.env.cr.savepoint():
                    binding = Product._upsert_product(store, product)
                    binding._link_variants(product)
                    binding.fingerprint = util.fingerprint(product)
            except errors.ShopifyError as error:
                _logger.info('Shopify product %s from an export is imported again: %s', product.get('id'), error)
                Product._enqueue_product_import(store, product['id'])
                counts['queued_again'] += 1
                continue
            counts['imported'] += 1
        if bulk and bulk.state == 'importing':
            others = self.env['eh.shopify.job'].sudo().search_count(
                bulk._chunk_domain() + [('state', 'in', ('pending', 'running')), ('id', '!=', job.id)])
            if not others:
                bulk._close_import()
        return counts

    def _close_import(self):
        Job = self.env['eh.shopify.job'].sudo()
        for bulk in self:
            vals = {'state': 'done'}
            failed = Job.search_count(bulk._chunk_domain() + [('state', 'in', ('failed', 'dead'))])
            if failed:
                vals['note'] = ' '.join(filter(None, [bulk.note, _(
                    '%s parts could not be imported. Retry them under Sync jobs, or run the import again.') % failed]))
            bulk.write(vals)

    @api.model
    def _finish_idle_imports(self, store):
        """Close imports whose chunks all ran, and exports that stopped making
        progress because their last job gave up."""
        Job = self.env['eh.shopify.job'].sudo()
        stuck_before = fields.Datetime.now() - STUCK_AFTER
        for bulk in self.search([('store_id', '=', store.id), ('state', 'in', ACTIVE_STATES)]):
            if Job.search_count(bulk._live_job_domain()):
                continue
            if bulk.state == 'importing':
                bulk._close_import()
            elif bulk.write_date and bulk.write_date < stuck_before:
                bulk._fail(_('The export stopped making progress, so it was closed. Run it again.'))

    def action_stop(self):
        self.env['eh.shopify.job']._require_manager()
        Job = self.env['eh.shopify.job'].sudo()
        for bulk in self.sudo().filtered(lambda record: record.state in ACTIVE_STATES):
            Job.search(bulk._live_job_domain() + [('state', '=', 'pending')])._close_cancelled()
            bulk._fail(_('Stopped by %s.') % self.env.user.name)
        return True


class ShopifyStoreBulk(models.Model):
    _inherit = 'eh.shopify.store'

    def action_import_all_products(self):
        self.ensure_one()
        compat.check_access(self, 'write')
        if not self.products_enabled:
            raise UserError(_('Switch on Sync products first.'))
        if self.env['eh.shopify.bulk'].search_count([('store_id', '=', self.id), ('kind', '=', 'products'),
                                                     ('state', 'in', ACTIVE_STATES)]):
            return self._notify(_('Import already running'), _('Progress shows under Activity, Shopify Exports.'))
        self.env['eh.shopify.job'].sudo()._enqueue(self, 'bulk.start', name=_('Export all products from Shopify'),
                                                   payload={'kind': 'products'}, dedup_key='bulk.start:products',
                                                   priority=7)
        return self._notify(_('Product import started'), _(
            'Shopify prepares an export of the whole catalogue, then each product is linked or created here. Progress '
            'shows under Activity, Shopify Exports.'))
