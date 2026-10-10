# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import fields, models


class ShopifyFulfillment(models.Model):
    """A fulfillment created in Shopify for an Odoo shipment."""

    _name = 'eh.shopify.fulfillment'
    _description = 'Shopify Fulfillment'
    _order = 'id desc'
    _rec_name = 'name'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    order_binding_id = fields.Many2one('eh.shopify.order', 'Shopify order', ondelete='cascade', index=True)
    picking_id = fields.Many2one('stock.picking', ondelete='set null', index=True)
    shopify_gid = fields.Char('Shopify ID', readonly=True)
    name = fields.Char(readonly=True)
    status = fields.Char(readonly=True)
    tracking_company = fields.Char(readonly=True)
    tracking_numbers = fields.Char(readonly=True)
    quantities = fields.Json('Quantities per Shopify line', readonly=True)
    notified_customer = fields.Boolean(readonly=True)
    origin = fields.Selection([('odoo', 'Shipped from Odoo'), ('shopify', 'Shipped in Shopify')], default='odoo',
                              readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_fulfillment_gid_uniq "
                            "ON eh_shopify_fulfillment (store_id, shopify_gid) WHERE shopify_gid IS NOT NULL")
