# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import _, fields, models

DEFAULT_FINANCIAL_STATUSES = 'PAID,PARTIALLY_PAID,PARTIALLY_REFUNDED,AUTHORIZED,PENDING'


class ShopifyStoreOrders(models.Model):
    _inherit = 'eh.shopify.store'

    orders_enabled = fields.Boolean('Import orders', default=True)
    order_import_from = fields.Datetime('Import orders from',
                                        help='Orders created before this moment stay in Shopify only. Use the '
                                             'historical import to bring older orders in.')
    import_financial_statuses = fields.Char('Import orders that are', default=DEFAULT_FINANCIAL_STATUSES)
    order_currency_mode = fields.Selection([
        ('shop', 'Store currency'),
        ('presentment', 'Customer currency'),
    ], string='Order currency', default='shop', required=True)
    order_confirm = fields.Boolean('Confirm imported orders', default=True)
    invoice_policy = fields.Selection([
        ('paid', 'When paid in Shopify'),
        ('delivered', 'When delivered'),
        ('manual', 'Manually'),
    ], string='Create invoices', default='paid', required=True)
    unmatched_product = fields.Selection([
        ('exception', 'Stop and ask me'),
        ('create', 'Create the product'),
    ], string='When a product is not found', default='exception', required=True)
    hold_risky_orders = fields.Boolean('Hold orders Shopify flags as risky', default=True)
    auto_create_taxes = fields.Boolean('Create missing taxes', default=True)
    auto_create_payment_terms = fields.Boolean(
        'Create missing payment terms', default=True,
        help='B2B orders due a number of days after invoicing get an Odoo payment term with that delay, created as '
             'Net N when none exists.')
    order_team_id = fields.Many2one('crm.team', string='Sales team', check_company=True)
    order_user_id = fields.Many2one('res.users', string='Salesperson')
    tax_exempt_fiscal_position_id = fields.Many2one('account.fiscal.position', string='Tax exempt fiscal position',
                                                    check_company=True)
    shipping_product_id = fields.Many2one('product.product', string='Shipping product')
    refund_adjustment_product_id = fields.Many2one('product.product', string='Refund adjustment product')
    return_auto_validate = fields.Boolean(
        'Receive returns automatically', default=False,
        help='When Shopify closes a return, validate the Odoo return transfer for the units put back in stock. Off: '
             'the transfer waits for the warehouse.')
    pos_walk_in_partner_id = fields.Many2one('res.partner', 'Point of sale walk-in customer',
                                             help='Till sales without a customer are recorded on this contact.')
    refund_auto_post = fields.Boolean('Post Shopify refunds automatically', default=True,
                                      help='Credit notes that match the Shopify refund are posted at once. Differences '
                                           'always stay in draft with the reason.')
    move_fulfillment_orders = fields.Boolean('Move Shopify fulfillment orders to the shipping location', default=True,
                                             help='When Odoo ships from a warehouse other than the one Shopify '
                                                  'assigned, move the Shopify fulfillment order there first, when '
                                                  'Shopify allows it.')
    auto_create_carriers = fields.Boolean('Create carriers for new tracking companies', default=True,
                                          help='When Shopify ships with a carrier Odoo does not know yet, create it '
                                               'once and reuse it for later shipments.')
    tip_product_id = fields.Many2one('product.product', string='Tip product')
    fee_product_id = fields.Many2one('product.product', string='Fee product')
    duty_product_id = fields.Many2one('product.product', string='Duty product')
    gift_card_product_id = fields.Many2one('product.product', string='Gift card product')
    rounding_product_id = fields.Many2one('product.product', string='Rounding product')
    import_test_orders = fields.Boolean('Import test orders', default=False)
    orders_polled_until = fields.Datetime('Orders checked until', readonly=True, copy=False)
    order_count = fields.Integer(compute='_compute_order_count')

    def _compute_order_count(self):
        Order = self.env['eh.shopify.order'].sudo()
        for store in self:
            store.order_count = Order.search_count([('store_id', '=', store.id)]) if store.id else 0

    def _financial_status_set(self):
        self.ensure_one()
        return {s.strip().upper() for s in (self.import_financial_statuses or '').split(',') if s.strip()}

    def action_open_orders(self):
        self.ensure_one()
        return self._open('eh.shopify.order', 'Orders', [('store_id', '=', self.id)])

    def _pos_walk_in_partner(self):
        self.ensure_one()
        if not self.pos_walk_in_partner_id:
            partner = self.env['res.partner'].sudo().create({
                'name': _('Walk-in customer (%s)') % self.name, 'company_id': self.company_id.id,
                'customer_rank': 1})
            self.sudo().pos_walk_in_partner_id = partner
        return self.pos_walk_in_partner_id
