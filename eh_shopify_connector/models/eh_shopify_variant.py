# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import math

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ShopifyVariant(models.Model):
    """Link between a Shopify variant and an Odoo product variant."""

    _name = 'eh.shopify.variant'
    _description = 'Shopify Product Variant'
    _order = 'id desc'
    _rec_name = 'title'
    _check_company_auto = True

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    product_gid = fields.Char('Shopify product ID', readonly=True)
    inventory_item_gid = fields.Char('Shopify inventory item ID', readonly=True, index=True)
    product_id = fields.Many2one('product.product', ondelete='cascade', index=True)
    title = fields.Char(readonly=True)
    sku = fields.Char('SKU', readonly=True, index=True)
    barcode = fields.Char(readonly=True)
    match_method = fields.Selection([
        ('binding', 'Linked'), ('sku', 'SKU'), ('barcode', 'Barcode'), ('manual', 'Manual'), ('created', 'Created'),
    ], readonly=True)
    active_in_shopify = fields.Boolean(default=True, readonly=True)
    shopify_updated_at = fields.Datetime('Updated in Shopify', readonly=True)
    fingerprint = fields.Char(readonly=True)
    stock_policy = fields.Selection([
        ('computed', 'From Odoo stock'),
        ('fixed', 'Fixed quantity'),
        ('off', 'Do not send'),
    ], string='Stock sent', default='computed', required=True)
    stock_share = fields.Integer('Share of stock (%)', default=100,
                                 help='Part of the Odoo stock offered in this store, for merchants selling the same '
                                      'stock in several places.')
    stock_max = fields.Integer('Most offered', default=0, help='Never offer more than this. 0 means no limit.')
    stock_fixed = fields.Integer('Fixed quantity', default=0,
                                 help='Always offered in Shopify whatever Odoo holds, for example made to order items.')

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_variant_gid_uniq "
                            "ON eh_shopify_variant (store_id, shopify_gid)")

    @api.constrains('stock_share', 'stock_max', 'stock_fixed')
    def _check_stock_rules(self):
        for variant in self:
            if not 0 <= (variant.stock_share or 0) <= 100:
                raise ValidationError(_('The share of stock must be between 0 and 100 percent.'))
            if (variant.stock_max or 0) < 0 or (variant.stock_fixed or 0) < 0:
                raise ValidationError(_('Stock quantities cannot be negative.'))

    def write(self, vals):
        result = super().write(vals)
        if {'stock_policy', 'stock_share', 'stock_max', 'stock_fixed', 'product_id', 'inventory_item_gid'} & set(vals):
            self.env['eh.shopify.store']._mark_inventory_dirty(self.mapped('product_id'))
        return result

    def _eh_offered_qty(self, pool, reading, safety):
        """Quantity Shopify should offer for this link at one location.

        ``pool`` is the whole units Odoo can sell there (free plus this store's
        own reservations), ``reading`` the level just read from Shopify. Override
        this method to plug in another availability rule.
        """
        self.ensure_one()
        if self.stock_policy == 'fixed':
            return max(0, self.stock_fixed or 0)
        share = min(100, max(0, self.stock_share or 0))
        offered = int(math.floor(pool * share / 100.0 + 1e-6)) - reading['committed'] - reading['reserved'] - safety
        if self.stock_max:
            offered = min(offered, self.stock_max)
        return max(0, offered)

    @api.model
    def _product_domain(self, store):
        return ['|', ('company_id', '=', False), ('company_id', '=', store.company_id.id)]

    @api.model
    def _resolve_line(self, store, line):
        """Return (product, reason). ``reason`` explains a failed match in words."""
        variant = line.get('variant') or {}
        sku = (line.get('sku') or variant.get('sku') or '').strip()
        barcode = (variant.get('barcode') or '').strip()
        Product = self.env['product.product']
        if not variant.get('id'):
            return Product, _('"%s" is a custom item without a Shopify product.') % (line.get('name') or '')
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', variant['id'])], limit=1)
        if binding.product_id and binding.product_id.active:
            self._refresh(binding, line)
            return binding.product_id, None
        product = Product
        method = None
        if sku:
            found = Product.search(self._product_domain(store) + [('default_code', '=', sku)], limit=2)
            if len(found) > 1:
                return Product, _('More than one Odoo product has the internal reference %s.') % sku
            product, method = found, 'sku'
        if not product and barcode:
            found = Product.search(self._product_domain(store) + [('barcode', '=', barcode)], limit=2)
            if len(found) > 1:
                return Product, _('More than one Odoo product has the barcode %s.') % barcode
            product, method = found, 'barcode'
        if not product:
            return Product, _('No Odoo product has the internal reference %s%s.') % (
                sku or _('(no SKU in Shopify)'), (_(' or the barcode %s') % barcode) if barcode else '')
        vals = {'store_id': store.id, 'shopify_gid': variant['id'],
                'product_gid': (line.get('product') or {}).get('id'),
                'inventory_item_gid': (variant.get('inventoryItem') or {}).get('id'),
                'product_id': product.id, 'title': line.get('name') or line.get('title'), 'sku': sku or False,
                'barcode': barcode or False, 'match_method': method}
        if binding:
            binding.write(vals)
        else:
            self.create(vals)
        return product, None

    @api.model
    def _refresh(self, binding, line):
        variant = line.get('variant') or {}
        item = (variant.get('inventoryItem') or {}).get('id')
        if item and binding.inventory_item_gid != item:
            binding.inventory_item_gid = item
