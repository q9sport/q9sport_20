# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Catalogue match check before going live.

Before a catalogue is linked, a manager sees what the import would do without
anything changing: which Shopify variants link to which Odoo product and why,
which products would be created, which need a decision because several Odoo
products share a reference or barcode, which Shopify variants match nothing,
which saleable Odoo products no Shopify variant matches, and which references
several Shopify variants use. The check reads the catalogue through a bulk
export and follows the same matching rules as the import. Counts are complete;
lists keep the first rows of each result.
"""
from odoo import _, api, fields, models

from .. import compat
from .eh_shopify_product import _odoo_option_key, _shopify_option_key, _variant_barcode

LINE_LIMIT = 2000
KINDS = [
    ('linked', 'Already linked'),
    ('link', 'Will link'),
    ('create', 'Will be created in Odoo'),
    ('ambiguous', 'Needs a decision'),
    ('unmatched', 'Only in Shopify'),
    ('odoo_only', 'Only in Odoo'),
    ('duplicate', 'Reference used twice in Shopify'),
]
COUNTED = ('linked', 'link', 'create', 'ambiguous', 'unmatched')


class ShopifyMatchReport(models.Model):
    _name = 'eh.shopify.match.report'
    _description = 'Shopify Catalogue Check'
    _order = 'id desc'
    _rec_name = 'store_id'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True, readonly=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    bulk_id = fields.Many2one('eh.shopify.bulk', 'Shopify export', ondelete='set null', readonly=True)
    state = fields.Selection([('running', 'Reading Shopify'), ('ready', 'Ready'), ('failed', 'Failed')],
                             default='running', required=True, readonly=True, index=True)
    note = fields.Char(readonly=True)
    product_count = fields.Integer('Shopify products', default=0, readonly=True)
    variant_count = fields.Integer('Shopify variants', default=0, readonly=True)
    linked_count = fields.Integer('Already linked', default=0, readonly=True)
    link_count = fields.Integer('Will link', default=0, readonly=True)
    create_count = fields.Integer('Will be created', default=0, readonly=True)
    ambiguous_count = fields.Integer('Needs a decision', default=0, readonly=True)
    unmatched_count = fields.Integer('Only in Shopify', default=0, readonly=True)
    odoo_only_count = fields.Integer('Only in Odoo', default=0, readonly=True)
    duplicate_count = fields.Integer('References used twice', default=0, readonly=True)
    unchecked_count = fields.Integer('Not checked', default=0, readonly=True)
    finished_at = fields.Datetime('Finished', readonly=True)
    line_ids = fields.One2many('eh.shopify.match.line', 'report_id', readonly=True)

    def init(self):
        self.env.cr.execute("""CREATE TABLE IF NOT EXISTS eh_shopify_match_seen (
                                   report_id INTEGER NOT NULL, sku VARCHAR, product_id INTEGER)""")
        self.env.cr.execute("CREATE INDEX IF NOT EXISTS eh_shopify_match_seen_report "
                            "ON eh_shopify_match_seen (report_id)")

    def unlink(self):
        if self.ids:
            self.env.cr.execute("DELETE FROM eh_shopify_match_seen WHERE report_id IN %s", (tuple(self.ids),))
        return super().unlink()

    # ------------------------------------------------------------------ actions
    def action_open_lines(self):
        self.ensure_one()
        kind = self.env.context.get('match_kind')
        domain = [('report_id', '=', self.id)] + ([('kind', '=', kind)] if kind else [])
        return {'type': 'ir.actions.act_window', 'name': dict(KINDS).get(kind) or _('Details'),
                'res_model': 'eh.shopify.match.line', 'view_mode': 'list', 'domain': domain,
                'context': {'create': False}}

    def action_link_now(self):
        self.ensure_one()
        return self.store_id.action_import_all_products()

    def action_check_again(self):
        self.ensure_one()
        return self.store_id.action_check_catalogue_match()

    # ------------------------------------------------------------------ evaluation
    def _room(self):
        self.env['eh.shopify.match.line'].flush_model()
        self.env.cr.execute("SELECT kind, count(*) FROM eh_shopify_match_line WHERE report_id = %s GROUP BY kind",
                            (self.id,))
        used = dict(self.env.cr.fetchall())
        return {kind: LINE_LIMIT - used.get(kind, 0) for kind, _label in KINDS}

    def _detail(self, kind, method):
        # A method, not a static one: the translation language comes from the environment of the record.
        if kind == 'linked':
            return self.env._('Already linked')
        if kind == 'link':
            return {'sku': self.env._('Same internal reference'), 'barcode': self.env._('Same barcode'),
                    'options': self.env._('Same product and options')}.get(method, self.env._('Existing link'))
        if kind == 'ambiguous':
            return self.env._('Several Odoo products have this internal reference or barcode')
        if kind == 'create':
            return self.env._('Created by the import, because no variant of this product matches')
        return self.env._('No Odoo product has this internal reference or barcode')

    def _add(self, column, amount):
        if amount:
            self.env.cr.execute('UPDATE eh_shopify_match_report SET "%s" = COALESCE("%s", 0) + %%s WHERE id = %%s'
                                % (column, column), (amount, self.id))
            self.invalidate_recordset([column])

    def _evaluate(self, roots):
        """Record what the import would do with these export products."""
        self.ensure_one()
        store = self.store_id
        Variant = self.env['eh.shopify.variant'].sudo()
        Binding = self.env['eh.shopify.product'].sudo()
        lang = store._content_lang()
        counts = dict.fromkeys(COUNTED, 0)
        room = self._room()
        lines, seen_skus, seen_products = [], [], []
        for root in roots:
            binding = Binding.search([('store_id', '=', store.id), ('shopify_gid', '=', root['id'])], limit=1)
            by_options = {}
            if binding.product_tmpl_id:
                by_options = {_odoo_option_key(variant): variant
                              for variant in binding.product_tmpl_id.with_context(lang=lang).product_variant_ids}
            outcomes = []
            for node in root.get('_children') or []:
                sku = (node.get('sku') or '').strip()
                barcode = _variant_barcode(node)
                link = Variant.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1)
                product, method, problem = Variant._match_catalogue_variant(store, link, sku, barcode)
                if not product and problem == 'unmatched' and by_options:
                    candidate = by_options.get(_shopify_option_key(node.get('selectedOptions')))
                    if candidate:
                        product, method, problem = candidate, 'options', None
                if product:
                    kind = 'linked' if link.product_id == product else 'link'
                elif problem == 'ambiguous':
                    kind = 'ambiguous'
                else:
                    kind = 'unmatched'
                outcomes.append([node, kind, product, method, sku, barcode])
                seen_skus.append(sku or None)
                seen_products.append(product.id or None)
            if (outcomes and store.product_create_missing and not root.get('isGiftCard')
                    and all(outcome[1] == 'unmatched' for outcome in outcomes)):
                for outcome in outcomes:
                    outcome[1] = 'create'
            for node, kind, product, method, sku, barcode in outcomes:
                counts[kind] += 1
                if room[kind] > 0:
                    room[kind] -= 1
                    lines.append({'report_id': self.id, 'kind': kind, 'shopify_product': root.get('title'),
                                  'shopify_variant': node.get('title'), 'shopify_gid': node['id'], 'sku': sku or False,
                                  'barcode': barcode or False, 'product_id': product.id or False,
                                  'detail': self._detail(kind, method)})
        if lines:
            self.env['eh.shopify.match.line'].sudo().create(lines)
        if seen_skus:
            self.env.cr.execute("INSERT INTO eh_shopify_match_seen (report_id, sku, product_id) "
                                "SELECT %s, unnest(%s::varchar[]), unnest(%s::integer[])",
                                (self.id, seen_skus, seen_products))
        self._add('product_count', len(roots))
        self._add('variant_count', len(seen_skus))
        for kind, amount in counts.items():
            self._add('%s_count' % kind, amount)

    def _finalize(self):
        cr = self.env.cr
        for report in self:
            cr.execute("""SELECT sku, count(*) FROM eh_shopify_match_seen WHERE report_id = %s AND sku IS NOT NULL
                          GROUP BY sku HAVING count(*) > 1 ORDER BY sku""", (report.id,))
            duplicates = cr.fetchall()
            self.env['product.product'].flush_model(['active', 'product_tmpl_id'])
            self.env['product.template'].flush_model(['active', 'sale_ok', 'company_id'])
            cr.execute("""SELECT pp.id FROM product_product pp JOIN product_template pt ON pt.id = pp.product_tmpl_id
                           WHERE pp.active AND pt.active AND pt.sale_ok
                             AND (pt.company_id IS NULL OR pt.company_id = %s)
                             AND NOT EXISTS (SELECT 1 FROM eh_shopify_match_seen seen
                                              WHERE seen.report_id = %s AND seen.product_id = pp.id)
                           ORDER BY pp.default_code, pp.id""", (report.store_id.company_id.id, report.id))
            odoo_only = [row[0] for row in cr.fetchall()]
            lines = [{'report_id': report.id, 'kind': 'duplicate', 'sku': sku,
                      'detail': _('Used by %s Shopify variants') % count} for sku, count in duplicates[:LINE_LIMIT]]
            for product in self.env['product.product'].sudo().browse(odoo_only[:LINE_LIMIT]):
                lines.append({'report_id': report.id, 'kind': 'odoo_only', 'product_id': product.id,
                              'sku': product.default_code or False, 'barcode': product.barcode or False,
                              'detail': _('No Shopify variant matches this product')})
            if lines:
                self.env['eh.shopify.match.line'].sudo().create(lines)
            notes = []
            if report.bulk_id.partial:
                notes.append(_('Shopify stopped the export early, so this check covers part of the catalogue.'))
            if report.unchecked_count:
                notes.append(_('%s Shopify products could not be checked because Shopify sent their variants out of '
                               'order. The import reads them one by one.') % report.unchecked_count)
            largest = max([report['%s_count' % kind] for kind in COUNTED] + [len(duplicates), len(odoo_only)])
            if largest > LINE_LIMIT:
                notes.append(_('Lists show the first %s rows of each result; the counts are complete.') % LINE_LIMIT)
            report.write({'state': 'ready', 'duplicate_count': len(duplicates), 'odoo_only_count': len(odoo_only),
                          'finished_at': fields.Datetime.now(), 'note': ' '.join(notes) or False})
            cr.execute("DELETE FROM eh_shopify_match_seen WHERE report_id = %s", (report.id,))


class ShopifyMatchLine(models.Model):
    _name = 'eh.shopify.match.line'
    _description = 'Shopify Catalogue Check Line'
    _order = 'id'

    report_id = fields.Many2one('eh.shopify.match.report', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='report_id.company_id', store=True, index=True)
    kind = fields.Selection(KINDS, 'Result', required=True, index=True)
    shopify_product = fields.Char('Shopify product')
    shopify_variant = fields.Char('Shopify variant')
    shopify_gid = fields.Char('Shopify ID')
    sku = fields.Char('Internal reference')
    barcode = fields.Char()
    product_id = fields.Many2one('product.product', 'Odoo product', ondelete='set null')
    detail = fields.Char('Why')


class ShopifyBulkMatch(models.Model):
    _inherit = 'eh.shopify.bulk'

    match_report_id = fields.Many2one('eh.shopify.match.report', 'Catalogue check', ondelete='set null',
                                      readonly=True)

    def write(self, vals):
        result = super().write(vals)
        if vals.get('state') in ('done', 'failed'):
            for bulk in self.filtered(lambda record: record.kind == 'match'
                                      and record.match_report_id.state == 'running'):
                if vals['state'] == 'done':
                    bulk.match_report_id._finalize()
                else:
                    bulk.match_report_id.write({'state': 'failed', 'note': bulk.note,
                                                'finished_at': fields.Datetime.now()})
        return result

    def _job_bulk_start(self, job):
        result = super()._job_bulk_start(job)
        payload = job.payload or {}
        if payload.get('kind') == 'match' and result.get('bulk'):
            report = self.env['eh.shopify.match.report'].browse(payload.get('report_id')).exists()
            if report:
                bulk = self.browse(result['bulk'])
                bulk.match_report_id = report
                report.bulk_id = bulk
        return result

    def _handle_late(self, gids):
        if self.kind != 'match':
            return super()._handle_late(gids)
        if gids and self.match_report_id:
            self.match_report_id._add('unchecked_count', len(gids))
        return None

    def _job_bulk_chunk(self, job):
        payload = job.payload or {}
        if payload.get('kind') != 'match':
            return super()._job_bulk_chunk(job)
        bulk = self.browse(payload.get('bulk_id')).exists()
        report = bulk.match_report_id
        if not report or report.state != 'running':
            return {'skipped': 'the catalogue check is not running'}
        complete, unchecked = [], 0
        for root in payload.get('roots') or []:
            expected = (root.get('variantsCount') or {}).get('count')
            if expected is not None and expected != len(root.get('_children') or []):
                unchecked += 1
                continue
            complete.append(root)
        report._evaluate(complete)
        report._add('unchecked_count', unchecked)
        if bulk.state == 'importing':
            others = self.env['eh.shopify.job'].sudo().search_count(
                bulk._chunk_domain() + [('state', 'in', ('pending', 'running')), ('id', '!=', job.id)])
            if not others:
                bulk._close_import()
        return {'checked': len(complete), 'unchecked': unchecked}


class ShopifyStoreMatch(models.Model):
    _inherit = 'eh.shopify.store'

    def action_check_catalogue_match(self):
        """Start a catalogue check, or show the one already running."""
        self.ensure_one()
        compat.check_access(self, 'write')
        Report = self.env['eh.shopify.match.report'].sudo()
        Job = self.env['eh.shopify.job'].sudo()
        report = Report.search([('store_id', '=', self.id), ('state', '=', 'running')], limit=1)
        if report and not report.bulk_id and not Job.search_count([
                ('store_id', '=', self.id), ('dedup_key', '=', 'bulk.start:match'),
                ('state', 'in', ('pending', 'running'))]):
            report.write({'state': 'failed', 'note': _('The check did not start. It was started again.'),
                          'finished_at': fields.Datetime.now()})
            report = Report
        if not report:
            report = Report.create({'store_id': self.id})
            Job._enqueue(self, 'bulk.start', name=_('Check the catalogue match with Shopify'),
                         payload={'kind': 'match', 'report_id': report.id}, dedup_key='bulk.start:match', priority=7)
        return {'type': 'ir.actions.act_window', 'res_model': 'eh.shopify.match.report', 'res_id': report.id,
                'view_mode': 'form', 'target': 'current'}
