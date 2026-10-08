# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import _, fields, models


class ShopifyStoreCatalogue(models.Model):
    _inherit = 'eh.shopify.store'

    products_enabled = fields.Boolean('Sync products', default=False,
                                      help='Read products from Shopify and link each variant to an Odoo product by '
                                           'internal reference or barcode.')
    products_polled_until = fields.Datetime(readonly=True, copy=False)
    products_swept_at = fields.Datetime('Last deleted products check', readonly=True, copy=False)
    product_binding_count = fields.Integer('Linked products', compute='_compute_product_binding_count')

    def _compute_product_binding_count(self):
        Product = self.env['eh.shopify.product']
        for store in self:
            store.product_binding_count = Product.search_count([('store_id', '=', store._origin.id)]) \
                if store._origin.id else 0

    def action_open_products(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('eh_shopify_connector.action_eh_shopify_product')
        action['domain'] = [('store_id', '=', self.id)]
        return action
