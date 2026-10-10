# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Dialog shown when a user cancels an order that came from Shopify.

Cancelling in Shopify is what refunds the customer and puts the items back on
sale there, so the dialog reads the order from Shopify first, explains when
Shopify cannot cancel it, and queues the cancellation. Cancelling in Odoo only
stays available for duplicates and mistakes.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .. import compat
from ..lib import documents, errors

REASONS = [
    ('CUSTOMER', 'The customer changed or cancelled the order'),
    ('DECLINED', 'The payment was declined'),
    ('FRAUD', 'The order was fraudulent'),
    ('INVENTORY', 'Items are unavailable'),
    ('STAFF', 'Staff error'),
    ('OTHER', 'Other'),
]
SHIPPED = ('FULFILLED', 'PARTIALLY_FULFILLED')


class ShopifyCancelWizard(models.TransientModel):
    _name = 'eh.shopify.cancel.wizard'
    _description = 'Cancel a Shopify Order'

    sale_order_id = fields.Many2one('sale.order', required=True, readonly=True)
    binding_id = fields.Many2one('eh.shopify.order', readonly=True)
    shopify_name = fields.Char('Shopify order', readonly=True)
    shopify_state = fields.Char('In Shopify', readonly=True)
    blocked_reason = fields.Char(readonly=True)
    reason = fields.Selection(REASONS, required=True, default='CUSTOMER')
    refund_method = fields.Selection([
        ('original', 'Refund to the original payment methods'),
        ('store_credit', 'Refund as store credit'),
        ('none', 'Do not refund'),
    ], string='Refund', default='original', required=True)
    refund_amount = fields.Float('Paid by the customer', readonly=True)
    refund_currency = fields.Char(readonly=True)
    restock = fields.Boolean('Put the items back on sale in Shopify', default=True)
    notify_customer = fields.Boolean('Email the customer', default=True)
    staff_note = fields.Char('Note for staff', size=255)

    @api.model
    def _open_for(self, sale_order):
        binding = sale_order.sudo().eh_shopify_order_id
        wizard = self.create({'sale_order_id': sale_order.id, 'binding_id': binding.id,
                              'shopify_name': binding.name or binding.shopify_gid})
        wizard._check_shopify()
        return {'type': 'ir.actions.act_window', 'name': _('Cancel Shopify order'), 'res_model': self._name,
                'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def _check_shopify(self):
        self.ensure_one()
        binding = self.binding_id.sudo()
        store = binding.store_id
        shipped = self.sale_order_id.sudo().picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and p.state == 'done')
        odoo_block = _('Odoo already shipped %s for this order, so cancelling would refund goods that left the '
                       'warehouse. Create a return first.') % ', '.join(shipped.mapped('name')) if shipped else False
        try:
            order = binding._read_order_state(store._client())
        except errors.ShopifyError as error:
            self.shopify_state = _('Shopify could not be reached: %s') % store._explain(error)
            self.blocked_reason = odoo_block
            return
        if not order:
            self.blocked_reason = odoo_block or _('This order no longer exists in Shopify.')
            return
        self.shopify_state = ', '.join(filter(None, [order.get('displayFinancialStatus'),
                                                     order.get('displayFulfillmentStatus')])).replace('_', ' ').lower()
        money = (order.get('netPaymentSet') or {}).get('shopMoney') or {}
        self.refund_amount = float(money.get('amount') or 0.0)
        self.refund_currency = money.get('currencyCode')
        if order.get('cancelledAt'):
            self.blocked_reason = _('Shopify already cancelled this order. Odoo follows with the next update.')
            self.env['eh.shopify.order'].sudo()._enqueue_import(store, binding.shopify_gid, priority=3)
        elif order.get('displayFulfillmentStatus') in SHIPPED:
            self.blocked_reason = _('Shopify already shipped part of this order, so it cannot be cancelled. Create a '
                                    'return and a refund in Shopify.')
        elif odoo_block:
            self.blocked_reason = odoo_block

    def action_cancel_in_shopify(self):
        self.ensure_one()
        if not self.env.user.has_group('eh_shopify_connector.group_shopify_manager'):
            raise AccessError(_('Only a Shopify manager can cancel orders in Shopify, because it refunds the '
                                'customer.'))
        if self.blocked_reason:
            raise UserError(self.blocked_reason)
        if self.refund_method == 'store_credit' and self.refund_amount <= 0:
            raise UserError(_('There is no paid amount to give back as store credit.'))
        self.env['eh.shopify.order'].sudo()._enqueue_cancel(self.binding_id.sudo(), {
            'reason': self.reason, 'restock': self.restock, 'notify_customer': self.notify_customer,
            'staff_note': (self.staff_note or '')[:255] or None, 'refund_method': self.refund_method,
            'refund_amount': self.refund_amount, 'refund_currency': self.refund_currency,
            'requested_by': self.env.user.id})
        self.sale_order_id.message_post(body=_('Cancellation requested in Shopify: %s.') % dict(REASONS)[self.reason])
        return {'type': 'ir.actions.act_window_close'}

    def action_cancel_in_odoo_only(self):
        self.ensure_one()
        # The standard cancel keeps its own checks, including the locked order rule.
        self.sale_order_id.with_context(eh_shopify_local_cancel=True, disable_cancel_warning=True).action_cancel()
        self.sale_order_id.message_post(body=_('Cancelled in Odoo only. The order is still open in Shopify.'))
        return {'type': 'ir.actions.act_window_close'}
