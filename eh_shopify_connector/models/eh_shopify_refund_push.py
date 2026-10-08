# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Refund in Shopify from an Odoo credit note.

A manager reviews Shopify's suggested refund for the credit note, then the job
sends refundCreate with an idempotency key built from the credit note and its
exact input, so a click repeated after a timeout gets Shopify's first answer
instead of a second refund. Shopify keeps that key for 24 hours, so before
sending, the job also looks for a refund of the order that already carries the
credit note's name. The amount sent is always the credit note total, never the
whole order. Refund payments are recorded when Shopify answers, and the
refunds/create echo finds the refund already recorded.
"""
from odoo import Command, _, api, fields, models

from ..lib import documents, errors

LIVE_STATES = ('connected', 'attention')


class AccountMoveShopifyRefundPush(models.Model):
    _inherit = 'account.move'

    eh_shopify_refund_state = fields.Selection([
        ('queued', 'Sent to Shopify'),
        ('done', 'Refunded in Shopify'),
        ('error', 'Not refunded in Shopify'),
    ], string='Shopify refund', readonly=True, copy=False)
    eh_shopify_refund_note = fields.Char(readonly=True, copy=False)
    eh_shopify_can_refund = fields.Boolean(compute='_compute_eh_shopify_can_refund')

    def _eh_shopify_order_binding(self):
        self.ensure_one()
        source = self.reversed_entry_id or self
        orders = source.sudo().invoice_line_ids.mapped('sale_line_ids.order_id')
        return orders.mapped('eh_shopify_order_id')[:1]

    @api.depends('move_type', 'state', 'eh_shopify_refund_state')
    def _compute_eh_shopify_can_refund(self):
        Refund = self.env['eh.shopify.refund'].sudo()
        Job = self.env['eh.shopify.job'].sudo()
        for move in self:
            allowed = False
            # A request whose job was skipped, cancelled or gave up must not hide the button for good.
            sent = move.eh_shopify_refund_state == 'queued' and Job.search_count([
                ('job_type', '=', 'refund.create'), ('res_model', '=', 'account.move'), ('res_id', '=', move.id),
                ('state', 'in', ('pending', 'running'))])
            if move.id and move.move_type == 'out_refund' and move.state == 'posted' \
                    and move.eh_shopify_refund_state != 'done' and not sent:
                binding = move._eh_shopify_order_binding()
                allowed = bool(binding and binding.store_id.state in LIVE_STATES
                               and not Refund.search_count([('credit_note_ids', 'in', move.ids)]))
            move.eh_shopify_can_refund = allowed

    def action_eh_shopify_refund(self):
        self.ensure_one()
        return self.env['eh.shopify.refund.wizard']._open_for(self)


class ShopifyRefundPush(models.Model):
    _inherit = 'eh.shopify.refund'

    def _existing_refund_for(self, store, client, order_gid, credit_note_name):
        data = client.execute(store._doc_text(documents.ORDER_REFUND_IDS), {'id': order_gid}).data or {}
        for refund in ((data.get('order') or {}).get('refunds') or []):
            if credit_note_name and (refund.get('note') or '') == credit_note_name:
                return refund['id']
        return None

    def _job_refund_create(self, job):
        payload = job.payload or {}
        store = job.store_id
        credit_note = self.env['account.move'].browse(payload.get('credit_note_id')).exists()
        if not credit_note or credit_note.state != 'posted':
            if credit_note:
                credit_note.sudo().write({'eh_shopify_refund_state': False, 'eh_shopify_refund_note': False})
            return {'skipped': 'the credit note is no longer posted'}
        if self.search_count([('credit_note_ids', 'in', credit_note.ids), ('state', '=', 'done')]):
            credit_note.eh_shopify_refund_state = 'done'
            return {'skipped': 'already refunded in Shopify'}
        refund_input = payload['input']
        order_gid = refund_input['orderId']
        client = store._client()
        refund_gid = self._existing_refund_for(store, client, order_gid, refund_input.get('note'))
        if not refund_gid:
            try:
                result = client.mutate(store._doc_text(documents.REFUND_CREATE), {'input': refund_input},
                                       documents.REFUND_CREATE.payload_key, idempotency_key=payload['key'])
            except errors.UserErrors as error:
                credit_note.write({'eh_shopify_refund_state': 'error',
                                   'eh_shopify_refund_note': _('Not refunded in Shopify: %s') % error.message})
                return {'refused': error.message}
            refund_gid = (result.get('refund') or {}).get('id')
        order_binding = self.env['eh.shopify.order'].search([('store_id', '=', store.id),
                                                             ('shopify_gid', '=', order_gid)], limit=1)
        refund = self._read_refund(store, refund_gid) or {'transactions': {'nodes': []}}
        key = 'presentmentMoney' if order_binding.currency_mode == 'presentment' else 'shopMoney'
        total = ((refund.get('totalRefundedSet') or {}).get(key) or {})
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', refund_gid)], limit=1)
        vals = {'store_id': store.id, 'order_binding_id': order_binding.id, 'shopify_gid': refund_gid,
                'name': _('Refund %s') % (refund.get('legacyResourceId') or refund_gid.rsplit('/', 1)[-1]),
                'note': credit_note.name, 'currency_code': total.get('currencyCode'),
                'total_refunded': float(total.get('amount') or 0.0), 'state': 'done', 'reason': False,
                'credit_note_ids': [Command.set(credit_note.ids)]}
        if binding:
            binding.write(vals)
        else:
            binding = self.create(vals)
        notes = binding._record_refund_payments(refund, order_binding, credit_note)
        if refund.get('refundLineItems'):
            cancelled, restocked = binding._stock_changes(refund, order_binding)
            binding._apply_stock(cancelled, restocked, refund)
        if notes:
            binding.reason = ' '.join(notes)
        credit_note.write({'eh_shopify_refund_state': 'done', 'eh_shopify_refund_note': False})
        return {'refund': refund_gid}
