# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.exceptions import AccessError, UserError
from odoo import _, api, fields, models

from ..lib import util

# Shopify's sourceName values for its own channels, as the Shopify admin names them.
CHANNELS = {
    'web': 'Online Store',
    'pos': 'Point of Sale',
    'shopify_draft_order': 'Draft order',
    'iphone': 'Shopify mobile',
    'android': 'Shopify mobile',
}


class ShopifyOrder(models.Model):
    _name = 'eh.shopify.order'
    _description = 'Shopify Order'
    _order = 'processed_at desc, id desc'
    _rec_name = 'name'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    legacy_id = fields.Char('Shopify number', readonly=True)
    name = fields.Char('Shopify order', readonly=True)
    sale_order_id = fields.Many2one('sale.order', 'Sales order', ondelete='set null', index=True, readonly=True)
    customer_binding_id = fields.Many2one('eh.shopify.customer', 'Customer', ondelete='set null', readonly=True)
    state = fields.Selection([
        ('imported', 'Imported'),
        ('held', 'On hold'),
        ('skipped', 'Not imported'),
        ('cancelled', 'Cancelled'),
        ('error', 'Needs attention'),
    ], readonly=True, index=True)
    reason = fields.Char(readonly=True)
    financial_status = fields.Char(readonly=True)
    fulfillment_status = fields.Char(readonly=True)
    return_status = fields.Char(readonly=True)
    source_name = fields.Char('Sales channel code', readonly=True)
    source_label = fields.Char('Sales channel', compute='_compute_readable_statuses')
    is_test = fields.Boolean('Test order', readonly=True)
    is_pos = fields.Boolean('Point of sale', readonly=True)
    retail_location_gid = fields.Char('Shopify till location', readonly=True)
    processed_at = fields.Datetime('Placed', readonly=True)
    cancelled_at = fields.Datetime('Cancelled', readonly=True)
    shopify_updated_at = fields.Datetime('Updated in Shopify', readonly=True)
    risk_recommendation = fields.Char('Fraud recommendation code', readonly=True)
    risk_level = fields.Char('Fraud risk code', readonly=True)
    # Shopify's values are kept as sent, because the import compares against them;
    # these read them back the way a person would say them.
    financial_status_label = fields.Char('Payment', compute='_compute_readable_statuses')
    fulfillment_status_label = fields.Char('Fulfillment', compute='_compute_readable_statuses')
    return_status_label = fields.Char('Returns', compute='_compute_readable_statuses')
    risk_level_label = fields.Char('Fraud risk', compute='_compute_readable_statuses')
    risk_recommendation_label = fields.Char('Fraud recommendation', compute='_compute_readable_statuses')
    currency_code = fields.Char('Currency', readonly=True)
    currency_mode = fields.Selection([('shop', 'Store currency'), ('presentment', 'Customer currency')],
                                     string='Currency used', readonly=True)
    target_total = fields.Float('Total in Shopify', digits=(16, 4), readonly=True)
    odoo_total = fields.Float('Total in Odoo', digits=(16, 4), readonly=True)
    residual = fields.Float('Difference', digits=(16, 4), readonly=True)
    customer_data_restricted = fields.Boolean(
        'Customer details hidden by Shopify', readonly=True,
        help='Shopify withheld names, emails, phones or addresses because the app is not approved for protected '
             'customer data on this plan. Amounts and products still import.')
    parity_state = fields.Selection([
        ('ok', 'Matches Shopify'),
        ('adjusted', 'Matches after rounding line'),
        ('mismatch', 'Does not match Shopify'),
    ], string='Total check', readonly=True)
    utm_source = fields.Char('Campaign source', readonly=True)
    utm_medium = fields.Char('Campaign medium', readonly=True)
    utm_campaign = fields.Char('Campaign', readonly=True)
    fingerprint = fields.Char(readonly=True)
    line_ids = fields.One2many('eh.shopify.order.line', 'order_binding_id', readonly=True)
    transaction_ids = fields.One2many('eh.shopify.transaction', 'order_binding_id', readonly=True)
    fulfillment_ids = fields.One2many('eh.shopify.fulfillment', 'order_binding_id', 'Shipments', readonly=True)
    refund_ids = fields.One2many('eh.shopify.refund', 'order_binding_id', readonly=True)

    @api.depends('financial_status', 'fulfillment_status', 'return_status', 'risk_level', 'risk_recommendation',
                 'source_name')
    def _compute_readable_statuses(self):
        for binding in self:
            binding.financial_status_label = util.readable_enum(binding.financial_status)
            binding.fulfillment_status_label = util.readable_enum(binding.fulfillment_status)
            binding.return_status_label = util.readable_enum(binding.return_status)
            binding.risk_level_label = util.readable_enum(binding.risk_level)
            binding.risk_recommendation_label = util.readable_enum(binding.risk_recommendation)
            binding.source_label = CHANNELS.get(binding.source_name) or util.readable_enum(binding.source_name)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_order_gid_uniq "
                            "ON eh_shopify_order (store_id, shopify_gid)")

    def action_open_sale_order(self):
        self.ensure_one()
        if not self.sale_order_id:
            return False
        return {'type': 'ir.actions.act_window', 'res_model': 'sale.order', 'res_id': self.sale_order_id.id,
                'view_mode': 'form'}


    def action_import_again(self):
        """Ask Shopify for this order once more.

        An order on hold or not imported was left alone for a reason the merchant can
        usually fix in Odoo: a product that was missing, a currency that was not set
        up, a customer that could not be created. Once it is fixed there has to be a
        way to ask for the order again without waiting for the next change in Shopify.
        """
        if not self.env.user.has_group('eh_shopify_connector.group_shopify_manager'):
            raise AccessError(_('Only a Shopify manager can import an order again.'))
        Order = self.env['eh.shopify.order']
        for binding in self:
            if binding.store_id.state not in ('connected', 'attention'):
                raise UserError(_('%s belongs to a store that is not connected. Connect the store first, then import '
                                  'the order again.') % (binding.name or binding.shopify_gid))
            Order._enqueue_import(binding.store_id, binding.shopify_gid, priority=4)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Queued'),
            'message': _('%s orders are being imported again. Watch them in Sync Jobs.') % len(self),
            'type': 'success', 'sticky': False}}

    def unlink(self):
        linked = self.filtered('sale_order_id')
        if linked:
            raise UserError(_('%s came from Shopify and is linked to a sales order. Deleting the link would import '
                              'the order again as a second sales order; archive the store instead.')
                            % ', '.join(linked.mapped(lambda binding: binding.name or binding.shopify_gid)))
        return super().unlink()


class ShopifyOrderLine(models.Model):
    _name = 'eh.shopify.order.line'
    _description = 'Shopify Order Line'
    _order = 'order_binding_id, id'

    order_binding_id = fields.Many2one('eh.shopify.order', 'Shopify order', required=True, ondelete='cascade',
                                       index=True)
    store_id = fields.Many2one('eh.shopify.store', related='order_binding_id.store_id', store=True, index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    shopify_gid = fields.Char('Shopify ID', readonly=True)
    kind = fields.Selection([
        ('product', 'Product'), ('gift_card', 'Gift card'), ('shipping', 'Shipping'), ('tip', 'Tip'),
        ('fee', 'Fee'), ('duty', 'Duty'), ('rounding', 'Rounding'),
    ], required=True, readonly=True)
    sale_line_id = fields.Many2one('sale.order.line', 'Sales order line', ondelete='set null', index=True,
                                   readonly=True)
    variant_gid = fields.Char('Shopify variant', readonly=True)
    sku = fields.Char('SKU', readonly=True)
    quantity = fields.Float(readonly=True)
    fulfilled_quantity = fields.Float('Shipped', readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_order_line_uniq "
                            "ON eh_shopify_order_line (order_binding_id, coalesce(shopify_gid, ''), kind)")


class ShopifyTransaction(models.Model):
    _name = 'eh.shopify.transaction'
    _description = 'Shopify Payment Transaction'
    _order = 'processed_at desc, id desc'
    _rec_name = 'shopify_gid'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    order_binding_id = fields.Many2one('eh.shopify.order', 'Shopify order', ondelete='cascade', index=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    parent_gid = fields.Char('Parent transaction', readonly=True)
    kind = fields.Char(readonly=True)
    status = fields.Char(readonly=True)
    gateway = fields.Char('Gateway name', readonly=True)
    gateway_id = fields.Many2one('eh.shopify.gateway', 'Payment method', ondelete='set null', readonly=True)
    amount = fields.Float(digits=(16, 4), readonly=True)
    currency_code = fields.Char('Currency', readonly=True)
    processed_at = fields.Datetime('Processed', readonly=True)
    is_test = fields.Boolean(readonly=True)
    payment_id = fields.Many2one('account.payment', ondelete='set null', readonly=True)
    state = fields.Selection([
        ('pending', 'Not recorded yet'),
        ('recorded', 'Recorded'),
        ('ignored', 'Not recorded by design'),
        ('error', 'Needs attention'),
    ], default='pending', readonly=True)
    note = fields.Char(readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_transaction_gid_uniq "
                            "ON eh_shopify_transaction (store_id, shopify_gid)")
