# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import _, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    eh_shopify_order_id = fields.Many2one('eh.shopify.order', string='Shopify order', readonly=True, copy=False,
                                          index=True)

    def _prepare_confirmation_values(self):
        vals = super()._prepare_confirmation_values()
        if self.env.context.get('eh_keep_date_order'):
            # Keep the moment the customer ordered in Shopify.
            vals.pop('date_order', None)
        return vals

    def action_cancel(self):
        if not self.env.context.get('eh_shopify_local_cancel'):
            bound = self.filtered(lambda order: order._eh_shopify_cancel_needs_dialog())
            if bound:
                locked = bound.filtered(lambda order: order.locked) if 'locked' in self._fields else \
                    bound.filtered(lambda order: order.state == 'done')
                if locked:
                    raise UserError(_('You cannot cancel a locked order. Please unlock it first.'))
                if len(self) > 1:
                    raise UserError(_('%s comes from Shopify. Cancel Shopify orders one at a time so the refund to '
                                      'the customer can be chosen.') % bound[0].name)
                return self.env['eh.shopify.cancel.wizard']._open_for(self)
        return super().action_cancel()

    def _eh_shopify_cancel_needs_dialog(self):
        """A live Shopify order is cancelled through Shopify, never silently here."""
        self.ensure_one()
        binding = self.sudo().eh_shopify_order_id
        return bool(binding and self.state != 'cancel' and binding.state != 'cancelled'
                    and binding.store_id.state in ('connected', 'attention'))
