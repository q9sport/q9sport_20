# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Compare stock at a location run by Shopify or another app with Odoo.

Where Shopify or a logistics partner holds the goods, Shopify's count is the
reference. The dialog reads Shopify's on hand quantity for every linked
product, shows the difference with Odoo, and applies only the lines left ticked
as ordinary Odoo inventory adjustments. Nothing changes without that review.
"""
import time

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .. import compat
from ..lib import documents, errors

READ_PAGE = 100
LOAD_BUDGET = 60.0


class ShopifyStockCompareWizard(models.TransientModel):
    _name = 'eh.shopify.stock.compare.wizard'
    _description = 'Compare Stock With Shopify'

    location_binding_id = fields.Many2one('eh.shopify.location', 'Shopify location', required=True, readonly=True)
    stock_location_id = fields.Many2one('stock.location', 'Odoo location', required=True, readonly=True)
    line_ids = fields.One2many('eh.shopify.stock.compare.line', 'wizard_id', 'Differences')
    difference_count = fields.Integer(compute='_compute_difference_count')
    note = fields.Char(readonly=True)

    @api.depends('line_ids')
    def _compute_difference_count(self):
        for wizard in self:
            wizard.difference_count = len(wizard.line_ids)

    @api.model
    def _open_for(self, location_binding):
        location_binding.ensure_one()
        if location_binding.delivery_mode != 'shopify':
            raise UserError(_('Only locations shipped by Shopify or another app take their stock count from Shopify.'))
        if not location_binding.location_ids:
            raise UserError(_('Choose the Odoo location that mirrors %s first.') % location_binding.name)
        wizard = self.create({'location_binding_id': location_binding.id,
                              'stock_location_id': location_binding.location_ids[:1].id})
        wizard._load_lines()
        return {'type': 'ir.actions.act_window', 'name': _('Compare stock with Shopify'), 'res_model': self._name,
                'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def _shopify_on_hand(self, store, binding, gids):
        client = store._client()
        text = store._doc_text(documents.INVENTORY_LEVELS)
        on_hand = {}
        for start in range(0, len(gids), READ_PAGE):
            data = client.execute(text, {'ids': gids[start:start + READ_PAGE],
                                         'locationId': binding.shopify_gid}).data or {}
            for node in data.get('nodes') or []:
                if not node or not node.get('inventoryLevel'):
                    continue
                quantities = {q.get('name'): int(q.get('quantity') or 0)
                              for q in node['inventoryLevel'].get('quantities') or []}
                on_hand[node['id']] = quantities.get('on_hand', 0)
        return on_hand

    def _load_lines(self):
        self.ensure_one()
        binding = self.location_binding_id.sudo()
        store = binding.store_id.with_context(eh_shopify_deadline=time.monotonic() + LOAD_BUDGET)
        variants = self.env['eh.shopify.variant'].sudo().search([
            ('store_id', '=', store.id), ('product_id', '!=', False), ('inventory_item_gid', '!=', False),
            ('active_in_shopify', '=', True)]).filtered(lambda variant: compat.is_storable(variant.product_id))
        by_gid, seen = {}, set()
        for variant in variants:
            if variant.product_id.id not in seen:
                seen.add(variant.product_id.id)
                by_gid[variant.inventory_item_gid] = variant
        try:
            on_hand = self._shopify_on_hand(store, binding, list(by_gid))
        except errors.ShopifyError as error:
            raise UserError(_('Shopify could not be read: %s') % store._explain(error))
        products = variants.mapped('product_id').with_context(location=binding.location_ids.ids)
        products.invalidate_recordset(['qty_available'])
        odoo = {product.id: product.qty_available for product in products}
        commands = []
        for gid, shopify_quantity in on_hand.items():
            product = by_gid[gid].product_id
            odoo_quantity = odoo.get(product.id, 0.0)
            if abs(odoo_quantity - shopify_quantity) < 1e-6:
                continue
            tracked = product.tracking in ('lot', 'serial')
            commands.append((0, 0, {
                'product_id': product.id, 'odoo_quantity': odoo_quantity, 'shopify_quantity': shopify_quantity,
                'apply': not tracked,
                'note': _('Needs lot or serial numbers, count it in Odoo.') if tracked else False}))
        self.write({'line_ids': commands,
                    'note': False if commands else _('Odoo and Shopify agree for every linked product.')})

    def action_apply(self):
        self.ensure_one()
        if not self.env.user.has_group('stock.group_stock_manager'):
            raise AccessError(_('Only an inventory administrator can apply stock counts.'))
        binding = self.location_binding_id
        if (binding.delivery_mode != 'shopify' or self.stock_location_id not in binding.location_ids
                or binding.company_id not in self.env.companies):
            raise UserError(_('This comparison no longer matches the location settings. Open it again.'))
        locations = binding.location_ids
        location = self.stock_location_id
        Quant = self.env['stock.quant'].with_context(inventory_mode=True)
        name = _('Shopify count %s') % fields.Date.context_today(self)
        applied = []
        for line in self.line_ids.filtered(lambda l: l.apply and l.product_id.tracking not in ('lot', 'serial')):
            product = line.product_id.with_context(location=locations.ids)
            product.invalidate_recordset(['qty_available'])
            difference = line.shopify_quantity - product.qty_available
            if abs(difference) < 1e-6:
                continue
            quant = Quant.search([('product_id', '=', product.id), ('location_id', '=', location.id),
                                  ('lot_id', '=', False), ('package_id', '=', False), ('owner_id', '=', False)],
                                 limit=1)
            if quant:
                quant.inventory_quantity = quant.quantity + difference
            else:
                quant = Quant.create({'product_id': product.id, 'location_id': location.id,
                                      'inventory_quantity': difference})
            quant.with_context(inventory_name=name)._apply_inventory()
            applied.append(product.display_name)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Stock counts applied'), 'type': 'success', 'sticky': False,
            'message': _('%s products now match Shopify.') % len(applied),
            'next': {'type': 'ir.actions.act_window_close'}}}


class ShopifyStockCompareLine(models.TransientModel):
    _name = 'eh.shopify.stock.compare.line'
    _description = 'Stock Difference With Shopify'

    wizard_id = fields.Many2one('eh.shopify.stock.compare.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', required=True, readonly=True)
    odoo_quantity = fields.Float('In Odoo', readonly=True)
    shopify_quantity = fields.Float('In Shopify', readonly=True)
    difference = fields.Float(compute='_compute_difference')
    apply = fields.Boolean('Apply', default=True)
    note = fields.Char(readonly=True)

    @api.depends('odoo_quantity', 'shopify_quantity')
    def _compute_difference(self):
        for line in self:
            line.difference = line.shopify_quantity - line.odoo_quantity
