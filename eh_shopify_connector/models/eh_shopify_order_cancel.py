# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Cancel an order in Shopify on request from Odoo.

Shopify offers no idempotency key for orderCancel, so the job reads the order
before acting and again after any refusal. An unanswered request is reported
instead of replayed. Odoo cancels its own order only when the order import
sees Shopify's cancellation, so Odoo never shows a cancellation or a refund
that did not happen.
"""
import datetime

from odoo import _, api, fields, models

from ..lib import documents, errors

SHIPPED = ('FULFILLED', 'PARTIALLY_FULFILLED')


class ShopifyOrderCancel(models.Model):
    _inherit = 'eh.shopify.order'

    cancel_requested_at = fields.Datetime('Cancellation sent to Shopify', readonly=True, copy=False)

    @api.model
    def _enqueue_cancel(self, binding, payload):
        return self.env['eh.shopify.job'].sudo()._enqueue(
            binding.store_id, 'order.cancel', name=_('Cancel order %s in Shopify') % (binding.name or binding.shopify_gid),
            payload=dict(payload, gid=binding.shopify_gid), dedup_key='cancel:%s' % binding.shopify_gid,
            resource_key=binding.shopify_gid, priority=3, max_attempts=3)

    def _read_order_state(self, client):
        self.ensure_one()
        data = client.execute(self.store_id._doc_text(documents.ORDER_STATE), {'id': self.shopify_gid}).data or {}
        return data.get('order')

    def _cancel_variables(self, payload):
        variables = {'orderId': self.shopify_gid, 'reason': payload.get('reason') or 'OTHER',
                     'restock': bool(payload.get('restock')), 'notifyCustomer': bool(payload.get('notify_customer'))}
        if payload.get('staff_note'):
            variables['staffNote'] = payload['staff_note'][:255]
        method = payload.get('refund_method')
        if method == 'original':
            variables['refundMethod'] = {'originalPaymentMethodsRefund': True}
        elif method == 'store_credit':
            amount = float(payload.get('refund_amount') or 0.0)
            if amount <= 0 or not payload.get('refund_currency'):
                raise errors.MappingError(_('There is no paid amount to give back as store credit.'))
            variables['refundMethod'] = {'storeCreditRefund': {'amount': {
                'amount': '%.2f' % amount, 'currencyCode': payload['refund_currency']}}}
        return variables

    def _job_order_cancel(self, job):
        payload = job.payload or {}
        store = job.store_id
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', payload.get('gid'))], limit=1)
        if not binding:
            return {'skipped': 'the order is not linked any more'}
        shipped = binding.sale_order_id.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and p.state == 'done')
        if shipped:
            raise errors.MappingError(_('Odoo already shipped %(pickings)s for order %(name)s, so it is not cancelled in '
                                        'Shopify. Create a return first.') % {
                'pickings': ', '.join(shipped.mapped('name')), 'name': binding.name})
        client = store._client()
        order = binding._read_order_state(client)
        if not order:
            raise errors.NotFound(_('Shopify order %s no longer exists.') % (binding.name or binding.shopify_gid))
        if order.get('cancelledAt'):
            self._enqueue_import(store, binding.shopify_gid, priority=3)
            return {'already_cancelled': binding.shopify_gid}
        if order.get('displayFulfillmentStatus') in SHIPPED:
            raise errors.MappingError(_('Shopify already shipped part of order %s, so it cannot be cancelled. Create '
                                        'a return and a refund in Shopify.') % binding.name)
        variables = binding._cancel_variables(payload)
        try:
            result = client.mutate(store._doc_text(documents.ORDER_CANCEL), variables, documents.ORDER_CANCEL.payload_key)
        except errors.UserErrors as error:
            again = binding._read_order_state(client)
            if again and again.get('cancelledAt'):
                self._enqueue_import(store, binding.shopify_gid, priority=3)
                return {'already_cancelled': binding.shopify_gid}
            raise errors.MappingError(_('Shopify refused to cancel order %(name)s: %(error)s') % {
                'name': binding.name, 'error': error.message}, details={'user_errors': error.errors})
        binding.cancel_requested_at = fields.Datetime.now()
        self._enqueue_import(store, binding.shopify_gid, priority=3,
                             run_at=fields.Datetime.now() + datetime.timedelta(seconds=20))
        if binding.sale_order_id:
            binding.sale_order_id.message_post(body=_('Shopify accepted the cancellation. This order is cancelled '
                                                      'here as soon as Shopify confirms it.'))
        return {'cancel_requested': binding.shopify_gid, 'shopify_job': (result.get('job') or {}).get('id')}
