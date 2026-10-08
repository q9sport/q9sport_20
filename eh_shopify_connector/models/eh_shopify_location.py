# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models


class ShopifyLocation(models.Model):
    _name = 'eh.shopify.location'
    _description = 'Shopify Location'
    _order = 'store_id, name, id'
    _check_company_auto = True

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    name = fields.Char(required=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    legacy_id = fields.Char('Shopify number', readonly=True)
    active_in_shopify = fields.Boolean('Active in Shopify', readonly=True)
    fulfills_online_orders = fields.Boolean(readonly=True)
    ships_inventory = fields.Boolean(readonly=True)
    has_active_inventory = fields.Boolean(readonly=True)
    is_fulfillment_service = fields.Boolean('Run by another app', readonly=True)
    fulfillment_service_name = fields.Char(readonly=True)
    address = fields.Char(readonly=True)
    country_code = fields.Char(readonly=True)
    warehouse_id = fields.Many2one('stock.warehouse', check_company=True)
    location_ids = fields.Many2many('stock.location', 'eh_shopify_location_stock_location_rel', 'shopify_location_id',
                                    'stock_location_id', string='Odoo stock locations', check_company=True,
                                    domain="[('usage', '=', 'internal')]",
                                    help='Stock in these locations (and their sub locations) is offered at this '
                                         'Shopify location.')
    delivery_mode = fields.Selection([
        ('odoo', 'Odoo ships'),
        ('shopify', 'Shopify or another app ships'),
    ], default='odoo', required=True)
    sync_stock = fields.Boolean('Send stock', default=True)
    pickup_ready_when_reserved = fields.Boolean(
        'Pickups ready when stock is reserved',
        help='Tell Shopify a local pickup order is ready as soon as Odoo reserves its items, so the customer is '
             'invited to collect it without a separate click.')
    removed_in_shopify = fields.Boolean(readonly=True)
    fingerprint = fields.Char(readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_location_gid_uniq "
                            "ON eh_shopify_location (store_id, shopify_gid)")

    @api.onchange('warehouse_id')
    def _onchange_warehouse_id(self):
        for location in self:
            if location.warehouse_id and not location.location_ids:
                location.location_ids = location.warehouse_id.lot_stock_id

    @api.model
    def _vals_from_node(self, store, node):
        address = node.get('address') or {}
        parts = [address.get('address1'), address.get('address2'), address.get('city'), address.get('zip'),
                 address.get('provinceCode'), address.get('countryCode')]
        service = node.get('fulfillmentService') or {}
        return {
            'store_id': store.id,
            'name': node.get('name') or node['id'],
            'shopify_gid': node['id'],
            'legacy_id': node.get('legacyResourceId'),
            'active_in_shopify': bool(node.get('isActive')),
            'fulfills_online_orders': bool(node.get('fulfillsOnlineOrders')),
            'ships_inventory': bool(node.get('shipsInventory')),
            'has_active_inventory': bool(node.get('hasActiveInventory')),
            'is_fulfillment_service': bool(node.get('isFulfillmentService')),
            'fulfillment_service_name': service.get('serviceName'),
            'address': ', '.join(p for p in parts if p),
            'country_code': address.get('countryCode'),
        }

    @api.model
    def _defaults_for_new(self, store, node):
        vals = {'delivery_mode': 'shopify' if node.get('isFulfillmentService') else 'odoo',
                'sync_stock': not node.get('isFulfillmentService')}
        if store.warehouse_id:
            vals['warehouse_id'] = store.warehouse_id.id
            vals['location_ids'] = [(6, 0, store.warehouse_id.lot_stock_id.ids)]
        return vals

    def action_compare_stock(self):
        self.ensure_one()
        return self.env['eh.shopify.stock.compare.wizard']._open_for(self)
