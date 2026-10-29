# -*- coding: utf-8 -*-

from odoo import fields, models

STOCK_GUARD_QTY_FIELDS = {
    'on_hand': 'qty_available',
    'free': 'free_qty',
    'forecast': 'virtual_available',
}


class PosConfig(models.Model):
    _inherit = 'pos.config'

    def _default_stock_guard_override_group(self):
        return self.env.ref('point_of_sale.group_pos_manager', raise_if_not_found=False)

    stock_guard_show = fields.Boolean(
        string='Show Stock on Products',
        default=True,
        help="Show the available quantity in a corner of every storable product card.")
    stock_guard_qty_type = fields.Selection(
        [('on_hand', 'On Hand'), ('free', 'Free to Use'), ('forecast', 'Forecasted')],
        string='Quantity Shown',
        default='on_hand', required=True,
        help="On Hand: what is in the location now.\n"
             "Free to Use: on hand minus what is already reserved for other transfers.\n"
             "Forecasted: on hand plus incoming minus outgoing transfers.")
    stock_guard_location_id = fields.Many2one(
        'stock.location',
        string='Stock Location',
        domain="[('usage', '=', 'internal'), ('company_id', 'in', [company_id, False])]",
        help="Location (with its sub-locations) the quantities are read from. "
             "Leave empty to use the source location of the Operation Type.")
    stock_guard_mode = fields.Selection(
        [('none', 'Allow'), ('warn', 'Warn'), ('block', 'Block')],
        string='Out-of-Stock Products',
        default='warn', required=True,
        help="Allow: sell without any message.\n"
             "Warn: ask for confirmation before selling more than is available.\n"
             "Block: refuse to add the product, or to raise its quantity, above what is available.")
    stock_guard_override_group_id = fields.Many2one(
        'res.groups',
        string='Can Sell Out of Stock',
        default=_default_stock_guard_override_group,
        help="Users of this group get a confirmation instead of the block. Leave empty so nobody can override.")

    def _get_stock_guard_location(self):
        self.ensure_one()
        return self.stock_guard_location_id or self.picking_type_id.default_location_src_id

    def _stock_guard_user_can_override(self):
        self.ensure_one()
        group = self.stock_guard_override_group_id
        return bool(group) and group in self.env.user.all_group_ids

    def _get_stock_guard_quantities(self, products):
        """Available quantity per storable product in the stock location of the point of sale.

        Services, consumables and combos are not tracked and are left out of the result.
        """
        self.ensure_one()
        location = self._get_stock_guard_location()
        storable = products.sudo().filtered('is_storable')
        if not location or not storable:
            return {}
        qty_field = STOCK_GUARD_QTY_FIELDS[self.stock_guard_qty_type]
        storable = storable.with_context(location=location.id, warehouse_id=False)
        return {product.id: product[qty_field] for product in storable}

    def get_stock_guard_data(self, product_ids):
        """Called by the register when it starts and when new products are loaded."""
        self.ensure_one()
        ids = [pid for pid in product_ids or [] if isinstance(pid, int) and pid > 0]
        products = self.env['product.product'].browse(ids).exists()
        quantities = {}
        if self.stock_guard_show or self.stock_guard_mode != 'none':
            quantities = self._get_stock_guard_quantities(products)
        return {
            'quantities': {str(product_id): qty for product_id, qty in quantities.items()},
            'can_override': self._stock_guard_user_can_override(),
            'location_name': self._get_stock_guard_location().sudo().display_name or '',
        }
