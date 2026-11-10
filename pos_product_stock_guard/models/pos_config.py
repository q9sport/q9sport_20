# -*- coding: utf-8 -*-
from odoo import api , fields , models , _
from odoo.exceptions import ValidationError

STOCK_GUARD_QTY_FIELDS = {
    'on_hand' : 'qty_available' ,
    'free' : 'free_qty' ,
    'forecast' : 'virtual_available' ,
}


class PosConfig ( models.Model ) :
    _inherit = 'pos.config'

    def _default_stock_guard_override_group(self) :
        return self.env.ref (
            'point_of_sale.group_pos_manager' ,
            raise_if_not_found=False
        )

    stock_guard_show = fields.Boolean (
        string='Show Stock on Products' ,
        default=True ,
        help="Show the available quantity in a corner of every storable product card."
    )

    stock_guard_qty_type = fields.Selection (
        [
            ('on_hand' , 'On Hand') ,
            ('free' , 'Free to Use') ,
            ('forecast' , 'Forecasted')
        ] ,
        string='Quantity Shown' ,
        default='on_hand' ,
        required=True ,
        help="On Hand: what is in the location now.\n"
             "Free to Use: on hand minus what is already reserved for other transfers.\n"
             "Forecasted: on hand plus incoming minus outgoing transfers."
    )

    stock_guard_location_id = fields.Many2one (
        'stock.location' ,
        string='Stock Location' ,
        domain="[('usage', '=', 'internal'), ('company_id', 'in', [company_id, False])]" ,
        help="Location (with its sub-locations) the quantities are read from. "
             "Leave empty to use the source location of the Operation Type."
    )

    stock_guard_mode = fields.Selection (
        [
            ('none' , 'Allow') ,
            ('warn' , 'Warn') ,
            ('block' , 'Block')
        ] ,
        string='Out-of-Stock Products' ,
        default='warn' ,
        required=True ,
        help="Allow: sell without any message.\n"
             "Warn: ask for manager PIN before selling more than is available.\n"
             "Block: require manager PIN to sell more than is available."
    )

    stock_guard_override_group_id = fields.Many2one (
        'res.groups' ,
        string='Can Sell Out of Stock' ,
        default=_default_stock_guard_override_group ,
        help="Users of this group can approve selling out of stock."
    )

    stock_password_ids = fields.One2many (
        'pos.stock.guard.password' ,
        'config_id' ,
        string='Cashier Stock Passwords' ,
        copy=True ,
    )

    def _get_stock_guard_location(self) :
        self.ensure_one ()
        return (
                self.stock_guard_location_id
                or self.picking_type_id.default_location_src_id
        )

    def _stock_guard_user_can_override(self) :
        self.ensure_one ()
        group = self.stock_guard_override_group_id
        return bool ( group ) and group in self.env.user.all_group_ids

    def _get_stock_guard_quantities(self , products) :
        """Available quantity per storable product in the POS stock location."""
        self.ensure_one ()

        location = self._get_stock_guard_location ()
        storable = products.sudo ().filtered ( 'is_storable' )

        if not location or not storable :
            return {}

        qty_field = STOCK_GUARD_QTY_FIELDS[self.stock_guard_qty_type]
        storable = storable.with_context (
            location=location.id ,
            warehouse_id=False
        )

        return {
            product.id : product[qty_field]
            for product in storable
        }

    def get_stock_guard_data(self , product_ids) :
        """Called by the register when it starts and when new products are loaded."""
        self.ensure_one ()

        ids = [
            pid for pid in product_ids or []
            if isinstance ( pid , int ) and pid > 0
        ]

        products = self.env['product.product'].browse ( ids ).exists ()
        quantities = {}

        if self.stock_guard_show or self.stock_guard_mode != 'none' :
            quantities = self._get_stock_guard_quantities ( products )

        location = self._get_stock_guard_location ()

        return {
            'quantities' : {
                str ( product_id ) : qty
                for product_id , qty in quantities.items ()
            } ,
            'can_override' : self._stock_guard_user_can_override () ,
            'location_name' : (
                location.sudo ().display_name if location else ''
            ) ,
        }

    def verify_stock_guard_override_password(self , employee_id , password) :
        """
        Verify the stock password assigned to the selected cashier
        in this POS configuration.
        """
        self.ensure_one ()

        if (
                not isinstance ( employee_id , int )
                or employee_id <= 0
                or not isinstance ( password , str )
                or not password
        ) :
            return False

        employee = self.env['hr.employee'].sudo ().browse (
            employee_id
        ).exists ()

        if not employee or not employee.active :
            return False

        if employee.company_id and employee.company_id != self.company_id :
            return False

        password_line = self.sudo ().stock_password_ids.filtered (
            lambda line : (
                    line.employee_id.id == employee.id
                    and line.employee_id.active
            )
        )

        return bool (
            password_line
            and password_line[0].stock_password
            and password_line[0].stock_password == password
        )


### ADD variables for adding categories of products in All pos  ###
class PosCategory(models.Model):
    _inherit = 'pos.category'

    add_to_all_pos = fields.Boolean(
        string="Add to All POSs",
        default=True,
    )
    pos_category_sort_name = fields.Char(
        string='PoS Product Category',
        compute='_compute_pos_category_sort_name',
        store=True,
    )
    
    @api.depends('name', 'parent_id', 'parent_id.name')
    def _compute_pos_category_sort_name(self):
        for rec in self:
            rec.pos_category_sort_name = rec.display_name

    @api.onchange('add_to_all_pos')
    def _onchange_add_to_all_pos(self):
        for rec in self:
            if rec.add_to_all_pos:
                all_ids = self.env['pos.config'].sudo().search([]).ids
                rec.pos_config_ids = [(6, 0, all_ids)]
            else:
                rec.pos_config_ids = [(5, 0, 0)]

    def write(self, vals):
        vals = vals.copy()

        if 'add_to_all_pos' in vals:
            if vals['add_to_all_pos']:
                all_ids = self.env['pos.config'].sudo().search([]).ids
                vals['pos_config_ids'] = [(6, 0, all_ids)]
            else:
                vals['pos_config_ids'] = [(5, 0, 0)]

        return super().write(vals)
    


class PosStockGuardPassword ( models.Model ) :
    _name = 'pos.stock.guard.password'
    _description = 'POS Cashier Stock Password'
    _order = 'employee_id'

    config_id = fields.Many2one (
        'pos.config' ,
        string='Point of Sale' ,
        required=True ,
        ondelete='cascade' ,
        index=True ,
    )

    employee_id = fields.Many2one (
        'hr.employee' ,
        string='Cashier' ,
        required=True ,
        ondelete='cascade' ,
        domain="[('active', '=', True)]" ,
        index=True ,
    )

    stock_password = fields.Char (
        string='Stock Password' ,
        required=True ,
        copy=False ,
    )

    _sql_constraints = [
        (
            'pos_cashier_stock_password_unique' ,
            'unique(config_id, employee_id)' ,
            'A cashier can only have one stock password per POS configuration.'
        ) ,
    ]

    @api.constrains ( 'employee_id' , 'config_id' )
    def _check_employee_company(self) :
        for record in self :
            if (
                    record.employee_id.company_id
                    and record.employee_id.company_id != record.config_id.company_id
            ) :
                raise ValidationError (
                    _ ( "The cashier must belong to the same company as the POS." )
                )
