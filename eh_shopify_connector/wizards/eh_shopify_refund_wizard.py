# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import json
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..lib import documents, errors, order_math

KEY_NAMESPACE = uuid.UUID('9b2c6a0e-3f1d-4c7a-8e5b-1d2f3a4b5c6d')
REASONS = [('CUSTOMER', 'Customer'), ('DAMAGE', 'Damaged goods'), ('RESTOCK', 'Restocking'), ('OTHER', 'Other')]


class ShopifyRefundWizard(models.TransientModel):
    _name = 'eh.shopify.refund.wizard'
    _description = 'Refund in Shopify'

    credit_note_id = fields.Many2one('account.move', required=True, readonly=True)
    binding_id = fields.Many2one('eh.shopify.order', readonly=True)
    currency_code = fields.Char(readonly=True)
    credit_total = fields.Float('Credit note total', readonly=True)
    shipping_amount = fields.Float('Shipping to refund', readonly=True)
    suggested_total = fields.Float('Shopify suggests', readonly=True)
    maximum_refundable = fields.Float('Shopify can refund at most', readonly=True)
    plan = fields.Json(readonly=True)
    blocked_reason = fields.Char(readonly=True)
    discrepancy_reason = fields.Selection(REASONS, string='Why the amount differs')
    notify = fields.Boolean('Email the customer', default=False)
    line_ids = fields.One2many('eh.shopify.refund.wizard.line', 'wizard_id', 'Items')

    @api.model
    def _expected(self, credit_note):
        """Everything the dialog shows, worked out from the credit note itself."""
        binding = credit_note._eh_shopify_order_binding().sudo()
        lines = {line.sale_line_id.id: line for line in binding.line_ids if line.sale_line_id and line.shopify_gid}
        items, shipping = [], 0.0
        for move_line in credit_note.invoice_line_ids.filtered(lambda l: l.display_type in (False, 'product')):
            order_line = next((lines[sale_line.id] for sale_line in move_line.sale_line_ids if sale_line.id in lines), None)
            if order_line and order_line.kind in ('product', 'gift_card'):
                items.append((order_line.shopify_gid, move_line.name, int(round(move_line.quantity))))
            elif order_line and order_line.kind == 'shipping':
                shipping += move_line.price_subtotal
        return {'binding': binding, 'currency_code': credit_note.currency_id.name,
                'credit_total': credit_note.amount_total, 'shipping': shipping, 'lines': items}

    @api.model
    def _open_for(self, credit_note):
        if not credit_note.eh_shopify_can_refund:
            raise UserError(_('This credit note cannot be refunded in Shopify.'))
        expected = self._expected(credit_note)
        commands = [(0, 0, {'shopify_line_gid': gid, 'name': name, 'quantity': quantity, 'restock_type': 'NO_RESTOCK'})
                    for gid, name, quantity in expected['lines']]
        wizard = self.create({'credit_note_id': credit_note.id, 'binding_id': expected['binding'].id,
                              'currency_code': expected['currency_code'], 'credit_total': expected['credit_total'],
                              'shipping_amount': expected['shipping'], 'line_ids': commands})
        wizard._preview()
        return {'type': 'ir.actions.act_window', 'name': _('Refund in Shopify'), 'res_model': self._name,
                'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def _variables(self):
        variables = {'id': self.binding_id.shopify_gid, 'lines': [
            {'lineItemId': line.shopify_line_gid, 'quantity': line.quantity, 'restockType': line.restock_type}
            for line in self.line_ids if line.quantity > 0]}
        if self.shipping_amount:
            variables['shippingAmount'] = '%.2f' % self.shipping_amount
        return variables

    def _preview(self):
        self.ensure_one()
        binding = self.binding_id.sudo()
        store = binding.store_id
        try:
            data = store._client().execute(store._doc_text(documents.SUGGEST_REFUND), self._variables()).data or {}
        except errors.ShopifyError as error:
            self.blocked_reason = _('Shopify could not be reached: %s') % store._explain(error)
            return
        suggested = ((data.get('order') or {}).get('suggestedRefund')) or {}
        key = 'presentmentMoney' if binding.currency_mode == 'presentment' else 'shopMoney'

        def money(bag):
            return float(order_math.dec(((bag or {}).get(key) or {}).get('amount')))
        plan, currencies = [], set()
        for item in suggested.get('suggestedTransactions') or []:
            money_bag = item.get('maximumRefundableSet') or item.get('amountSet') or {}
            bag = money_bag.get(key) or {}
            currencies.add(bag.get('currencyCode'))
            # Shopify refunds in the customer's currency, so that is the one that must match.
            currencies.add((money_bag.get('presentmentMoney') or {}).get('currencyCode'))
            plan.append({'parent_id': (item.get('parentTransaction') or {}).get('id'), 'gateway': item.get('gateway'),
                         'maximum': money(item.get('maximumRefundableSet') or item.get('amountSet'))})
        maximum = sum(item['maximum'] for item in plan)
        blocked = False
        if currencies - {self.currency_code, None}:
            blocked = _('Shopify refunds this order in %(shopify)s but the credit note is in %(odoo)s. Refund it in '
                        'Shopify directly.') % {'shopify': ', '.join(sorted(c for c in currencies if c)),
                                                'odoo': self.currency_code}
        elif self.credit_total > maximum + 0.005:
            blocked = _('The credit note is %(credit)s but Shopify can refund at most %(maximum)s.') % {
                'credit': round(self.credit_total, 2), 'maximum': round(maximum, 2)}
        self.write({'suggested_total': money(suggested.get('amountSet')), 'maximum_refundable': maximum,
                    'plan': plan, 'blocked_reason': blocked})

    def action_preview(self):
        self._preview()
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
                'target': 'new'}

    def action_confirm(self):
        self.ensure_one()
        if not self.env.user.has_group('eh_shopify_connector.group_shopify_manager'):
            raise AccessError(_('Only a Shopify manager can refund customers in Shopify.'))
        self._preview()
        if self.blocked_reason:
            raise UserError(self.blocked_reason)
        credit_note = self.credit_note_id
        if not credit_note.eh_shopify_can_refund:
            raise UserError(_('This credit note cannot be refunded in Shopify.'))
        expected = self._expected(credit_note)
        shown = sorted((line.shopify_line_gid, line.quantity) for line in self.line_ids)
        if (self.binding_id != expected['binding'] or self.currency_code != expected['currency_code']
                or round(self.credit_total - expected['credit_total'], 2)
                or round(self.shipping_amount - expected['shipping'], 2)
                or shown != sorted((gid, quantity) for gid, _name, quantity in expected['lines'])):
            raise UserError(_('The credit note no longer matches this dialog. Close it and open Refund in Shopify '
                              'again.'))
        difference = round(self.credit_total - self.suggested_total, 2)
        if abs(difference) >= 0.01 and not self.discrepancy_reason:
            raise UserError(_('The credit note differs from Shopify\'s suggestion by %s. Choose why before refunding.')
                            % difference)
        binding = self.binding_id.sudo()
        remaining = round(self.credit_total, 2)
        transactions = []
        for item in self.plan or []:
            take = round(min(remaining, item['maximum']), 2)
            if take <= 0:
                continue
            transactions.append({'orderId': binding.shopify_gid, 'parentId': item['parent_id'],
                                 'gateway': item['gateway'], 'kind': 'REFUND', 'amount': '%.2f' % take})
            remaining = round(remaining - take, 2)
        if remaining > 0.005:
            raise UserError(_('Shopify can refund at most %s for this order.') % round(self.maximum_refundable, 2))
        refund_input = {'orderId': binding.shopify_gid, 'note': credit_note.name, 'notify': bool(self.notify),
                        'currency': self.currency_code, 'transactions': transactions,
                        'refundLineItems': self._variables()['lines']}
        if self.shipping_amount:
            refund_input['shipping'] = {'amount': '%.2f' % self.shipping_amount}
        if abs(difference) >= 0.01:
            refund_input['discrepancyReason'] = self.discrepancy_reason
        key = uuid.uuid5(KEY_NAMESPACE, 'refund:%s:%s' % (credit_note.id, json.dumps(refund_input, sort_keys=True))).hex
        self.env['eh.shopify.job'].sudo()._enqueue(
            binding.store_id, 'refund.create', name=_('Refund %s in Shopify') % credit_note.name,
            payload={'credit_note_id': credit_note.id, 'input': refund_input, 'key': key},
            dedup_key='refund.create:%s' % credit_note.id, resource_key=binding.shopify_gid, priority=3,
            record=credit_note)
        credit_note.sudo().write({'eh_shopify_refund_state': 'queued', 'eh_shopify_refund_note': False})
        return {'type': 'ir.actions.act_window_close'}


class ShopifyRefundWizardLine(models.TransientModel):
    _name = 'eh.shopify.refund.wizard.line'
    _description = 'Refund in Shopify, item'

    wizard_id = fields.Many2one('eh.shopify.refund.wizard', required=True, ondelete='cascade')
    shopify_line_gid = fields.Char(readonly=True)
    name = fields.Char('Item', readonly=True)
    quantity = fields.Integer(readonly=True)
    restock_type = fields.Selection([
        ('NO_RESTOCK', 'No stock change in Shopify'),
        ('RETURN', 'Returned, put back in stock'),
        ('CANCEL', 'Not shipped, cancel'),
    ], string='Stock in Shopify', default='NO_RESTOCK', required=True)
