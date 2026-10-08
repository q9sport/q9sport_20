# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Send sellable stock from Odoo to Shopify.

What Shopify offers at a location is computed as:

    the free quantity in the mapped Odoo stock locations,
    plus Odoo reservations already made for this store's own orders,
    minus units Shopify has committed to unfulfilled orders,
    minus units Shopify holds back (reserved),
    minus the store safety stock, never below zero.

Shopify's committed units include orders Odoo has not imported yet, so a
sale in Shopify is never offered a second time. Reservations for this store's
imported orders are added back because Shopify already counts them.

Every quantity is sent compare and set against the value just read. When
Shopify refuses because stock moved in between, the level is read again and
the target recomputed, so a Shopify sale during the update is kept.

Stock changes in Odoo only insert rows into a queue table and never update a
shared row, so busy warehouses never block each other. The job runner turns
the queue into one job per store and drains it in chunks.
"""
import datetime
import logging
import math
import uuid

from odoo import _, api, fields, models

from .. import compat
from ..lib import documents, errors, inventory_plan

_logger = logging.getLogger(__name__)

LIVE_STATES = ('connected', 'attention')
CHUNK_PAIRS = 400
MIN_CHUNK_PRODUCTS = 20
FAILURE_COOLDOWN = datetime.timedelta(hours=1)
READ_PAGE = 100
RECONCILE_EVERY = datetime.timedelta(hours=24)


def _retry_code(code):
    """Refusals that mean 'read again and recompute', not 'broken'."""
    code = code or ''
    return 'STALE' in code or code == 'ITEM_NOT_STOCKED_AT_LOCATION'


class ShopifyInventoryQueue(models.Model):
    _name = 'eh.shopify.inventory.dirty'
    _description = 'Shopify Stock Waiting To Be Sent'
    _log_access = False
    _order = 'id'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', required=True, ondelete='cascade', index=True)
    queued_at = fields.Datetime('Queued', default=fields.Datetime.now)
    reconcile = fields.Boolean('Full check', index=True)


class ShopifyInventoryLevel(models.Model):
    _name = 'eh.shopify.inventory.level'
    _description = 'Shopify Stock Level'
    _order = 'id desc'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    variant_binding_id = fields.Many2one('eh.shopify.variant', 'Shopify variant', required=True,
                                         ondelete='cascade', index=True)
    location_binding_id = fields.Many2one('eh.shopify.location', 'Shopify location', required=True,
                                          ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', related='variant_binding_id.product_id', store=True)
    odoo_free = fields.Float('Free in Odoo', readonly=True)
    odoo_reserved_for_store = fields.Float('Reserved for this store', readonly=True)
    shopify_committed = fields.Integer('Committed in Shopify', readonly=True)
    shopify_reserved = fields.Integer('Held in Shopify', readonly=True)
    target_quantity = fields.Integer('Should offer', readonly=True)
    shopify_available = fields.Integer('Offered in Shopify', readonly=True)
    last_checked_at = fields.Datetime('Last checked', readonly=True)
    last_pushed_at = fields.Datetime('Last sent', readonly=True)
    state = fields.Selection([
        ('synced', 'In sync'),
        ('drift', 'Changed while sending'),
        ('untracked', 'Not tracked in Shopify'),
        ('error', 'Needs attention'),
    ], readonly=True)
    note = fields.Char(readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_inventory_level_uniq "
                            "ON eh_shopify_inventory_level (variant_binding_id, location_binding_id)")


class ShopifyStoreInventory(models.Model):
    _inherit = 'eh.shopify.store'

    inventory_enabled = fields.Boolean('Send stock to Shopify', default=True)
    inventory_safety_stock = fields.Integer('Safety stock', default=0,
                                            help='Units kept back in Odoo and never offered in Shopify.')
    inventory_reconciled_at = fields.Datetime('Last full stock check', readonly=True, copy=False)
    inventory_attention_count = fields.Integer('Stock issues', compute='_compute_inventory_attention_count')

    def _compute_inventory_attention_count(self):
        Level = self.env['eh.shopify.inventory.level']
        for store in self:
            store_id = store._origin.id
            store.inventory_attention_count = Level.search_count(
                [('store_id', '=', store_id), ('state', 'in', ('error', 'drift'))]) if store_id else 0

    # ------------------------------------------------------------------ queue
    @api.model
    def _mark_inventory_dirty(self, products):
        """Queue products whose sellable quantity may have changed. Insert only."""
        if not products:
            return 0
        cr = self.env.cr
        self.flush_model(['active', 'inventory_enabled', 'state'])
        cr.execute("SELECT 1 FROM eh_shopify_store WHERE active AND inventory_enabled AND state IN %s LIMIT 1",
                   (LIVE_STATES,))
        if not cr.fetchone():
            return 0
        products = products | self._kits_containing(products)
        self.env['eh.shopify.variant'].flush_model(['store_id', 'product_id', 'inventory_item_gid'])
        cr.execute("""
            INSERT INTO eh_shopify_inventory_dirty (store_id, product_id, queued_at, reconcile)
            SELECT DISTINCT v.store_id, v.product_id, (now() AT TIME ZONE 'UTC'), FALSE
              FROM eh_shopify_variant v
              JOIN eh_shopify_store s ON s.id = v.store_id
             WHERE v.product_id = ANY(%s)
               AND v.inventory_item_gid IS NOT NULL
               AND s.active AND s.inventory_enabled AND s.state IN %s
        """, (list(products.ids), LIVE_STATES))
        count = cr.rowcount
        if count:
            self.env['eh.shopify.job'].sudo()._trigger_runner()
        return count

    @api.model
    def _kits_containing(self, products):
        Product = self.env['product.product']
        if 'mrp.bom' not in self.env:
            return Product
        boms = self.env['mrp.bom'].sudo().search([('type', '=', 'phantom'),
                                                  ('bom_line_ids.product_id', 'in', products.ids)])
        kits = boms.mapped('product_id') | boms.filtered(lambda b: not b.product_id).mapped(
            'product_tmpl_id.product_variant_ids')
        return Product.browse(kits.ids)

    def _mark_all_inventory_dirty(self):
        """Queue every linked product of these stores for a full check. Rows are
        flagged so real stock changes drain first, and a check still draining is
        not queued a second time."""
        cr = self.env.cr
        self.env['eh.shopify.variant'].flush_model(['store_id', 'product_id', 'inventory_item_gid'])
        self.env['eh.shopify.inventory.dirty'].flush_model()
        for store in self:
            cr.execute("SELECT 1 FROM eh_shopify_inventory_dirty WHERE store_id = %s AND reconcile LIMIT 1",
                       (store.id,))
            if not cr.fetchone():
                cr.execute("""
                    INSERT INTO eh_shopify_inventory_dirty (store_id, product_id, queued_at, reconcile)
                    SELECT DISTINCT v.store_id, v.product_id, (now() AT TIME ZONE 'UTC'), TRUE
                      FROM eh_shopify_variant v
                     WHERE v.store_id = %s AND v.product_id IS NOT NULL AND v.inventory_item_gid IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM eh_shopify_inventory_dirty d
                                        WHERE d.store_id = v.store_id AND d.product_id = v.product_id)
                """, (store.id,))
                if cr.rowcount:
                    self.env['eh.shopify.job'].sudo()._trigger_runner()
            store.sudo().with_context(eh_shopify_no_stock_requeue=True).inventory_reconciled_at = fields.Datetime.now()

    @api.model
    def _schedule_inventory_flushes(self):
        """One flush job per store with queued stock. Run by the job runner.

        A flush waiting for a retry keeps its backoff (Shopify's Retry-After
        included), and a store whose last flush failed for good is tried again
        only after a cool-down, so neither outages nor a missing permission
        turn into a burst of attempts or a permanent stop."""
        cr = self.env.cr
        cr.execute("SELECT DISTINCT store_id FROM eh_shopify_inventory_dirty")
        store_ids = [row[0] for row in cr.fetchall()]
        Job = self.env['eh.shopify.job'].sudo()
        now = fields.Datetime.now()
        queued = 0
        for store in self.sudo().with_context(active_test=False).browse(store_ids).exists():
            if not store._inventory_live():
                cr.execute("DELETE FROM eh_shopify_inventory_dirty WHERE store_id = %s", (store.id,))
                store.with_context(eh_shopify_no_stock_requeue=True).inventory_reconciled_at = False
                continue
            if Job.search_count([('store_id', '=', store.id), ('dedup_key', '=', 'inventory.flush'),
                                 ('state', 'in', ('pending', 'running'))]):
                continue
            last = Job.search([('store_id', '=', store.id), ('dedup_key', '=', 'inventory.flush'),
                               ('state', 'in', ('done', 'failed', 'dead'))], order='id desc', limit=1)
            if last.state in ('failed', 'dead') and last.finished_at and last.finished_at > now - FAILURE_COOLDOWN:
                cr.execute("""DELETE FROM eh_shopify_inventory_dirty d USING eh_shopify_inventory_dirty k
                               WHERE d.store_id = %s AND k.store_id = d.store_id
                                 AND k.product_id = d.product_id AND k.id < d.id""", (store.id,))
                continue
            Job._enqueue(store, 'inventory.flush', name=_('Send stock to Shopify'), dedup_key='inventory.flush',
                         priority=6)
            queued += 1
        return queued

    def _inventory_live(self):
        self.ensure_one()
        return bool(self.active and self.inventory_enabled and self.state in LIVE_STATES)

    def write(self, vals):
        watched = {'inventory_enabled', 'inventory_safety_stock', 'state', 'active'} & set(vals)
        before = {}
        if watched and not self.env.context.get('eh_shopify_no_stock_requeue'):
            before = {store.id: (store._inventory_live(), store.inventory_safety_stock) for store in self}
        result = super().write(vals)
        if before:
            changed = self.filtered(lambda store: store._inventory_live() and (
                not before[store.id][0] or before[store.id][1] != store.inventory_safety_stock))
            if changed:
                changed._mark_all_inventory_dirty()
        return result

    def _job_inventory_flush(self, job):
        store = job.store_id
        cr = self.env.cr
        per_job = max(MIN_CHUNK_PRODUCTS, CHUNK_PAIRS // max(1, len(store._inventory_locations())))
        cr.execute("SELECT id, product_id FROM eh_shopify_inventory_dirty WHERE store_id = %s "
                   "ORDER BY reconcile NULLS FIRST, id LIMIT %s", (store.id, per_job * 10))
        product_ids, row_ids = [], []
        seen = set()
        for row_id, product_id in cr.fetchall():
            if product_id not in seen:
                if len(seen) >= per_job:
                    continue
                seen.add(product_id)
                product_ids.append(product_id)
            row_ids.append(row_id)
        if not row_ids:
            return {'products': 0}
        products = self.env['product.product'].with_context(active_test=False).browse(product_ids).exists()
        result = store._push_inventory(products)
        cr.execute("DELETE FROM eh_shopify_inventory_dirty WHERE id = ANY(%s)", (row_ids,))
        cr.execute("SELECT 1 FROM eh_shopify_inventory_dirty WHERE store_id = %s LIMIT 1", (store.id,))
        if cr.fetchone():
            result['more'] = True
            self.env['eh.shopify.job'].sudo()._trigger_runner()
        result['products'] = len(product_ids)
        return result

    # ------------------------------------------------------------------ push
    def _inventory_locations(self):
        self.ensure_one()
        return self.env['eh.shopify.location'].sudo().search([
            ('store_id', '=', self.id), ('sync_stock', '=', True), ('removed_in_shopify', '=', False),
            ('active_in_shopify', '=', True), ('delivery_mode', '=', 'odoo')]).filtered('location_ids')

    def _push_inventory(self, products=None):
        """Send the sellable quantity of ``products`` (all linked when None)."""
        self.ensure_one()
        totals = {'pairs': 0, 'sent': 0, 'unchanged': 0, 'failed': 0, 'drift': 0}
        if not self.inventory_enabled:
            return dict(totals, skipped='stock sync is switched off')
        locations = self._inventory_locations()
        domain = [('store_id', '=', self.id), ('product_id', '!=', False), ('inventory_item_gid', '!=', False),
                  ('stock_policy', '!=', 'off'), ('active_in_shopify', '=', True)]
        if products is not None:
            domain.append(('product_id', 'in', products.ids))
        variants = self.env['eh.shopify.variant'].sudo().search(domain).filtered(
            lambda variant: variant.stock_policy == 'fixed' or compat.is_storable(variant.product_id))
        if not locations or not variants:
            return totals
        client = self._client()
        self.env.flush_all()
        for location in locations:
            self._push_location(client, location, variants, totals)
        return totals

    def _push_location(self, client, location, variants, totals):
        stock_locations = location.location_ids
        products = variants.mapped('product_id').with_context(location=stock_locations.ids)
        products.invalidate_recordset(['free_qty'])
        free = {product.id: product.free_qty for product in products}
        held = self._reserved_for_own_orders(products, location)
        safety = max(0, self.inventory_safety_stock or 0)
        levels = self._inventory_levels(variants, location)
        by_gid = {variant.inventory_item_gid: variant for variant in variants}
        totals['pairs'] += len(by_gid)
        pending = list(by_gid)
        for _attempt in range(2):
            now = fields.Datetime.now()
            readings = self._read_inventory(client, pending, location)
            changes = []
            for gid in pending:
                variant, level, reading = by_gid[gid], levels[gid], readings.get(gid)
                if reading is None:
                    level.write({'state': 'error', 'last_checked_at': now,
                                 'note': _('This item no longer exists in Shopify. Link the product again.')})
                    totals['failed'] += 1
                    continue
                if not reading['tracked']:
                    level.write({'state': 'untracked', 'last_checked_at': now,
                                 'note': _('Shopify does not track stock for this item, so no quantity is sent.')})
                    continue
                if not reading['stocked']:
                    if not self._activate_inventory(client, gid, location, level):
                        totals['failed'] += 1
                        continue
                    reading = dict(reading, available=0, committed=0, reserved=0)
                odoo_free = free.get(variant.product_id.id, 0.0)
                odoo_held = held.get(variant.product_id.id, 0.0)
                pool = int(math.floor(odoo_free + odoo_held + 1e-6))
                target = variant._eh_offered_qty(pool, reading, safety)
                level.write({'odoo_free': odoo_free, 'odoo_reserved_for_store': odoo_held,
                             'shopify_committed': reading['committed'], 'shopify_reserved': reading['reserved'],
                             'shopify_available': reading['available'], 'target_quantity': target,
                             'last_checked_at': now})
                changes.append({'inventory_item_gid': gid, 'location_gid': location.shopify_gid,
                                'quantity': target, 'last_known': reading['available']})
            stale = self._send_inventory(client, changes, levels, totals)
            pending = [gid for gid in pending if gid in stale]
            if not pending:
                return
        for gid in pending:
            levels[gid].write({'state': 'drift', 'note': _(
                'Stock changed in Shopify twice while it was being updated. It is checked again with the next '
                'stock change or the daily check.')})
            totals['drift'] += 1

    def _inventory_levels(self, variants, location):
        Level = self.env['eh.shopify.inventory.level'].sudo()
        existing = {level.variant_binding_id.id: level for level in Level.search(
            [('location_binding_id', '=', location.id), ('variant_binding_id', 'in', variants.ids)])}
        missing = [variant for variant in variants if variant.id not in existing]
        if missing:
            for level in Level.create([{'store_id': self.id, 'variant_binding_id': variant.id,
                                        'location_binding_id': location.id} for variant in missing]):
                existing[level.variant_binding_id.id] = level
        return {variant.inventory_item_gid: existing[variant.id] for variant in variants}

    def _reserved_for_own_orders(self, products, location):
        """Units Odoo already set aside for this store's orders at this location.

        Counted in the mapped stock locations and, for a mapped warehouse, in
        its packing and output areas, so picked units still waiting to ship are
        not subtracted twice. A kit reserves its components, so those reservations
        are turned back into whole kits: Shopify commits the kit, not its
        components."""
        field = compat.move_line_reserved_field(self.env)
        where = [('location_id', 'child_of', location.location_ids.ids)]
        warehouse = location.warehouse_id
        if warehouse and warehouse.view_location_id and warehouse.lot_stock_id:
            where = ['|'] + where + ['&', ('location_id', 'child_of', warehouse.view_location_id.id),
                                     '!', ('location_id', 'child_of', warehouse.lot_stock_id.id)]
        lines = self.env['stock.move.line'].sudo().search([
            ('product_id', 'in', products.ids), ('state', 'not in', ('done', 'cancel')),
            ('move_id.sale_line_id.order_id.eh_shopify_order_id.store_id', '=', self.id)] + where)
        held, kits = {}, {}
        for line in lines:
            move = line.move_id
            sale_line = move.sale_line_id
            if move.product_id != sale_line.product_id:
                reserved = kits.setdefault(sale_line, {})
                reserved[move.product_id] = reserved.get(move.product_id, 0.0) + (line[field] or 0.0)
                continue
            held[line.product_id.id] = held.get(line.product_id.id, 0.0) + (line[field] or 0.0)
        for sale_line, reserved in kits.items():
            components = compat.kit_components(self.env, sale_line.product_id, company_id=sale_line.company_id.id)
            if not components:
                continue
            units = min(reserved.get(product, 0.0) / per_unit for product, per_unit in components.items())
            if units > 0:
                held[sale_line.product_id.id] = held.get(sale_line.product_id.id, 0.0) + units
        return held

    def _read_inventory(self, client, gids, location):
        readings = {}
        text = self._doc_text(documents.INVENTORY_LEVELS)
        for start in range(0, len(gids), READ_PAGE):
            chunk = gids[start:start + READ_PAGE]
            data = client.execute(text, {'ids': chunk, 'locationId': location.shopify_gid}).data or {}
            for node in data.get('nodes') or []:
                if not node or not node.get('id'):
                    continue
                stock = node.get('inventoryLevel')
                quantities = {q.get('name'): int(q.get('quantity') or 0)
                              for q in (stock or {}).get('quantities') or []}
                readings[node['id']] = {
                    'tracked': node.get('tracked') is not False,
                    'stocked': stock is not None,
                    'available': quantities.get('available', 0),
                    'committed': max(0, quantities.get('committed', 0)),
                    'reserved': max(0, quantities.get('reserved', 0)),
                }
        return readings

    def _activate_inventory(self, client, gid, location, level):
        try:
            client.mutate(self._doc_text(documents.INVENTORY_ACTIVATE),
                          {'inventoryItemId': gid, 'locationId': location.shopify_gid},
                          documents.INVENTORY_ACTIVATE.payload_key, idempotency_key=uuid.uuid4().hex)
        except errors.UserErrors as error:
            level.write({'state': 'error', 'note': _('Shopify refused to stock this item at %(location)s: %(error)s')
                         % {'location': location.name, 'error': str(error)[:200]}})
            return False
        return True

    def _send_inventory(self, client, changes, levels, totals):
        batches, _needs_read, skipped = inventory_plan.prepare(changes)
        for change in skipped:
            levels[change['inventory_item_gid']].write({'state': 'synced', 'note': False})
            totals['unchanged'] += 1
        text = self._doc_text(documents.INVENTORY_SET)
        reference = 'gid://erp-heritage/StockSync/%s' % self.id

        def send(batch):
            # A fresh key per request: Shopify replays the first answer for a
            # reused key for 24 hours, which would hide a later real change.
            # The client reuses this key for its own retries of the request.
            try:
                client.mutate(text, {'input': {'name': 'available', 'reason': 'correction',
                                               'referenceDocumentUri': reference, 'quantities': batch}},
                              documents.INVENTORY_SET.payload_key, idempotency_key=uuid.uuid4().hex)
            except errors.UserErrors as error:
                return error.errors or [{'message': str(error)}]
            return []

        outcome = inventory_plan.send_all(batches, send)
        now = fields.Datetime.now()
        for item in outcome['applied']:
            levels[item['inventoryItemId']].write({'shopify_available': item['quantity'], 'last_pushed_at': now,
                                                   'state': 'synced', 'note': False})
            totals['sent'] += 1
        stale = set()
        for item, error in outcome['failed']:
            if _retry_code(error.get('code')):
                stale.add(item['inventoryItemId'])
                continue
            levels[item['inventoryItemId']].write({'state': 'error',
                                                   'note': (error.get('message') or error.get('code') or '')[:250]})
            totals['failed'] += 1
        return stale

    # ------------------------------------------------------------------ user actions
    def action_push_inventory(self):
        self.ensure_one()
        compat.check_access(self, 'write')
        self._mark_all_inventory_dirty()
        self.env['eh.shopify.job'].sudo()._enqueue(self, 'inventory.flush', name=_('Send stock to Shopify'),
                                                   dedup_key='inventory.flush', priority=6)
        return self._notify(_('Stock check queued'), _('Every linked product is checked and sent to Shopify in '
                                                       'the background. Progress shows under Sync Jobs.'))

    def action_open_inventory_levels(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'eh_shopify_connector.action_eh_shopify_inventory_level')
        action['domain'] = [('store_id', '=', self.id)]
        action['context'] = {'search_default_attention': 1}
        return action


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _eh_shopify_mark_stock(self):
        products = self.mapped('product_id')
        if products:
            self.env['eh.shopify.store']._mark_inventory_dirty(products)

    def _action_done(self, cancel_backorder=False):
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        moves.filtered(lambda move: move.state == 'done')._eh_shopify_mark_stock()
        return moves

    def _action_assign(self, force_qty=False):
        result = super()._action_assign(force_qty=force_qty)
        self.filtered(lambda move: move.state in ('assigned', 'partially_available'))._eh_shopify_mark_stock()
        return result

    def _do_unreserve(self):
        result = super()._do_unreserve()
        self._eh_shopify_mark_stock()
        return result


RESERVATION_FIELDS = {'quantity', 'quantity_product_uom', 'reserved_uom_qty', 'reserved_qty', 'qty_done', 'location_id',
                      'product_id', 'lot_id', 'package_id', 'owner_id', 'state'}


class StockMoveLineInventory(models.Model):
    _inherit = 'stock.move.line'

    # Reservations also change when quantities are typed or scanned on a move
    # line, without going through the move hooks above.
    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        self.env['eh.shopify.store']._mark_inventory_dirty(lines.mapped('product_id'))
        return lines

    def write(self, vals):
        products = self.mapped('product_id') if RESERVATION_FIELDS & set(vals) else None
        result = super().write(vals)
        if products is not None:
            self.env['eh.shopify.store']._mark_inventory_dirty(products | self.mapped('product_id'))
        return result

    def unlink(self):
        products = self.mapped('product_id')
        result = super().unlink()
        self.env['eh.shopify.store']._mark_inventory_dirty(products)
        return result


class ShopifyLocationInventory(models.Model):
    _inherit = 'eh.shopify.location'

    @api.model_create_multi
    def create(self, vals_list):
        locations = super().create(vals_list)
        locations._requeue_store_stock()
        return locations

    def write(self, vals):
        result = super().write(vals)
        if {'sync_stock', 'location_ids', 'delivery_mode', 'warehouse_id', 'removed_in_shopify',
                'active_in_shopify'} & set(vals):
            self._requeue_store_stock()
        return result

    def _requeue_store_stock(self):
        stores = self.sudo().mapped('store_id').filtered(lambda store: store._inventory_live())
        if stores:
            stores._mark_all_inventory_dirty()
