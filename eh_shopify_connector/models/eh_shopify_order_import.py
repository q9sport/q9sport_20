# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Order import: fetch, complete, gate, bind.

Every order is imported by exactly one job at a time (the job resource key is
the order id), whether it was announced by a webhook or found by the poller.
The fetch always reads the order fresh from Shopify, so a lost, duplicated or
out of order webhook can never import stale data.
"""
import datetime
import logging

from odoo import _, api, fields, models

from ..lib import documents, errors, order_math, util
from ..lib.documents import scope_granted

_logger = logging.getLogger(__name__)

POLL_OVERLAP = datetime.timedelta(minutes=10)
POLL_PAGE = 250
RISK_HOLD = {'CANCEL', 'INVESTIGATE'}


def _parse_dt(value):
    if not value:
        return False
    try:
        return datetime.datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return False


# Context key of orders imported from the Shopify order history.
HISTORICAL = 'eh_shopify_historical'

class ShopifyOrderImport(models.Model):
    _inherit = 'eh.shopify.order'

    # ------------------------------------------------------------------ queueing
    @api.model
    def _enqueue_import(self, store, gid, priority=6, run_at=None, historical=False):
        payload = {'gid': gid, 'historical': True} if historical else {'gid': gid}
        return self.env['eh.shopify.job'].sudo()._enqueue(
            store, 'order.import', name=_('Import order %s') % gid.rsplit('/', 1)[-1], payload=payload,
            dedup_key='order:%s' % gid, resource_key=gid, priority=priority, run_at=run_at)

    # ------------------------------------------------------------------ fetch
    @api.model
    def _fetch_order(self, store, gid):
        client = store._client()
        response = client.execute(store._doc_text(documents.ORDER), {'id': gid},
                                  tolerate=errors.is_protected_data_error)
        order = (response.data or {}).get('order')
        if not order:
            if not scope_granted('read_all_orders', store._granted_scope_set()):
                raise errors.ScopeError(_('Order %s is not visible. It was deleted, or it is older than 60 days and '
                                          'the app lacks the read_all_orders permission.') % gid,
                                        missing_scopes=['read_all_orders'])
            raise errors.NotFound(_('Order %s no longer exists in Shopify.') % gid)
        order['_customerDataRestricted'] = bool(response.warnings)
        version = store.api_version
        order['lineItems'] = {'nodes': client.complete_connection(
            order, 'lineItems', documents.ORDER_LINE_ITEMS.text_for(version), gid, ('node', 'lineItems'),
            page_size=documents.ORDER_LINE_ITEMS.page_size)}
        order['discountApplications'] = {'nodes': client.complete_connection(
            order, 'discountApplications', documents.ORDER_DISCOUNT_APPLICATIONS.text_for(version), gid,
            ('node', 'discountApplications'), page_size=documents.ORDER_DISCOUNT_APPLICATIONS.page_size)}
        order['shippingLines'] = {'nodes': client.complete_connection(
            order, 'shippingLines', documents.ORDER_SHIPPING_LINES.text_for(version), gid,
            ('node', 'shippingLines'), page_size=documents.ORDER_SHIPPING_LINES.page_size)}
        fulfillment_orders = client.complete_connection(
            order, 'fulfillmentOrders', documents.ORDER_FULFILLMENT_ORDERS.text_for(version), gid,
            ('node', 'fulfillmentOrders'), page_size=documents.ORDER_FULFILLMENT_ORDERS.page_size)
        for fulfillment_order in fulfillment_orders:
            fulfillment_order['lineItems'] = {'nodes': client.complete_connection(
                fulfillment_order, 'lineItems', documents.FULFILLMENT_ORDER_LINE_ITEMS.text_for(version),
                fulfillment_order['id'], ('node', 'lineItems'),
                page_size=documents.FULFILLMENT_ORDER_LINE_ITEMS.page_size)}
        order['fulfillmentOrders'] = {'nodes': fulfillment_orders}
        if order_math.dec(((order.get('totalRefundedSet') or {}).get('shopMoney') or {}).get('amount')) > 0:
            order['_refundedQuantities'] = self._refunded_quantities(client, store, gid, order=order)
        expected = ((order.get('transactionsCount') or {}).get('count'))
        if expected is not None and expected > len(order.get('transactions') or []):
            # The order is still imported: the payments that came back are recorded and the rest is explained.
            order['_transactionsTruncated'] = expected
        return order

    @api.model
    def _refunded_quantities(self, client, store, gid, order=None):
        """Units refunded per order line, so the order is imported as sold."""
        version = store.api_version
        response = client.execute(documents.ORDER_REFUND_IDS.text_for(version), {'id': gid})
        refunds = ((response.data or {}).get('order') or {}).get('refunds') or []
        if len(refunds) >= 250:
            raise errors.IncompleteData(_('Order %s has more refunds than can be read in one request.') % gid)
        if order is not None:
            order['_refundIds'] = [refund['id'] for refund in refunds]
        quantities = {}
        for refund in refunds:
            nodes = client.complete_connection(
                {'refundLineItems': {'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': ''}}},
                'refundLineItems', documents.REFUND_LINE_ITEMS.text_for(version), refund['id'],
                ('node', 'refundLineItems'), page_size=documents.REFUND_LINE_ITEMS.page_size)
            for node in nodes:
                line_gid = (node.get('lineItem') or {}).get('id')
                if line_gid:
                    quantities[line_gid] = quantities.get(line_gid, 0) + int(node.get('quantity') or 0)
        return quantities

    # ------------------------------------------------------------------ gate
    @api.model
    def _gate(self, store, order, historical=False):
        """Return (state, reason) when the order must not become a sales order. The
        order history import skips only the import start date."""
        if order.get('test') and not store.import_test_orders:
            return 'skipped', _('Test order. Turn on "Import test orders" to bring test orders in.')
        processed = _parse_dt(order.get('processedAt') or order.get('createdAt'))
        if not historical and store.order_import_from and processed and processed < store.order_import_from:
            return 'skipped', _('Placed before the import start date of this store.')
        if order.get('cancelledAt'):
            return 'skipped', _('Cancelled in Shopify before it was imported.')
        status = (order.get('displayFinancialStatus') or '').upper()
        if status not in store._financial_status_set():
            return 'skipped', _('Payment status %s is not imported for this store. It is checked again when the '
                                'order changes.') % (status or _('unknown'))
        recommendation = ((order.get('risk') or {}).get('recommendation') or '').upper()
        if store.hold_risky_orders and recommendation in RISK_HOLD:
            return 'held', _('Shopify recommends to %s this order because of fraud risk. Review it in Shopify, '
                             'then retry.') % recommendation.lower()
        return None, None

    # ------------------------------------------------------------------ binding
    @api.model
    def _risk_level(self, order):
        levels = [(a.get('riskLevel') or '') for a in ((order.get('risk') or {}).get('assessments') or [])]
        for level in ('HIGH', 'MEDIUM', 'LOW', 'NONE', 'PENDING'):
            if level in levels:
                return level
        return False

    @api.model
    def _utm(self, order):
        summary = order.get('customerJourneySummary') or {}
        visit = summary.get('lastVisit') or summary.get('firstVisit') or {}
        utm = visit.get('utmParameters') or {}
        return {'utm_source': utm.get('source') or visit.get('source') or False,
                'utm_medium': utm.get('medium') or False,
                'utm_campaign': utm.get('campaign') or False}

    @api.model
    def _upsert_binding(self, store, order, state, reason):
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', order['id'])], limit=1)
        vals = {
            'store_id': store.id,
            'shopify_gid': order['id'],
            'legacy_id': str(order.get('legacyResourceId') or ''),
            'name': order.get('name'),
            'state': state,
            'reason': reason or False,
            'financial_status': order.get('displayFinancialStatus'),
            'fulfillment_status': order.get('displayFulfillmentStatus'),
            'return_status': order.get('returnStatus'),
            'source_name': order.get('sourceName'),
            'is_test': bool(order.get('test')),
            'is_pos': bool(order.get('retailLocation')) or (order.get('sourceName') or '').lower() == 'pos',
            'retail_location_gid': (order.get('retailLocation') or {}).get('id') or False,
            'processed_at': _parse_dt(order.get('processedAt')),
            'cancelled_at': _parse_dt(order.get('cancelledAt')),
            'shopify_updated_at': _parse_dt(order.get('updatedAt')),
            'risk_recommendation': (order.get('risk') or {}).get('recommendation') or False,
            'risk_level': self._risk_level(order),
            'currency_mode': store.order_currency_mode,
            'customer_data_restricted': bool(order.get('_customerDataRestricted')),
        }
        vals.update(self._utm(order))
        if binding:
            # The money basis of an order is fixed when it is first imported, so
            # its refunds keep matching its invoices after the store setting changes.
            vals.pop('currency_mode')
            binding.write(vals)
        else:
            binding = self.create(vals)
        return binding

    def _record_transactions(self, order):
        self.ensure_one()
        Transaction = self.env['eh.shopify.transaction']
        Gateway = self.env['eh.shopify.gateway']
        key = 'presentmentMoney' if self.currency_mode == 'presentment' else 'shopMoney'
        existing = {t.shopify_gid: t for t in Transaction.search([('store_id', '=', self.store_id.id),
                                                                   ('order_binding_id', '=', self.id)])}
        for tx in order.get('transactions') or []:
            money = (tx.get('amountSet') or {}).get(key) or {}
            vals = {
                'store_id': self.store_id.id,
                'order_binding_id': self.id,
                'shopify_gid': tx['id'],
                'parent_gid': (tx.get('parentTransaction') or {}).get('id') or False,
                'kind': tx.get('kind'),
                'status': tx.get('status'),
                'gateway': tx.get('gateway'),
                'gateway_id': Gateway._for(self.store_id, tx.get('gateway'),
                                           manual=bool(tx.get('manualPaymentGateway'))).id,
                'amount': float(order_math.dec(money.get('amount'))),
                'currency_code': money.get('currencyCode'),
                'processed_at': _parse_dt(tx.get('processedAt') or tx.get('createdAt')),
                'is_test': bool(tx.get('test')),
            }
            record = existing.get(tx['id'])
            if record:
                update = {k: v for k, v in vals.items() if k not in ('store_id', 'order_binding_id')}
                if (record.state == 'ignored' and record.status != 'SUCCESS' and tx.get('status') == 'SUCCESS'
                        and tx.get('kind') in ('SALE', 'CAPTURE', 'REFUND')):
                    update.update(state='pending', note=False)
                record.write(update)
            else:
                if tx.get('status') != 'SUCCESS' or tx.get('kind') not in ('SALE', 'CAPTURE', 'REFUND'):
                    vals.update(state='ignored', note=_('Only successful sales, captures and refunds are recorded.'))
                Transaction.create(vals)

    # ------------------------------------------------------------------ job
    def _job_order_import(self, job):
        store = job.store_id
        gid = (job.payload or {}).get('gid')
        historical = bool((job.payload or {}).get('historical'))
        order = self._fetch_order(store, gid)
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', gid)], limit=1)
        digest = util.fingerprint(order)
        if binding and binding.fingerprint == digest and binding.state in ('imported', 'skipped', 'cancelled'):
            return {'unchanged': gid}
        state, reason = self._gate(store, order, historical=historical)
        if state and not (binding and binding.sale_order_id):
            binding = self._upsert_binding(store, order, state, reason)
            binding._record_transactions(order)
            binding.fingerprint = digest
            return {'state': state, 'reason': reason}
        binding = self._upsert_binding(store, order, 'imported', False)
        binding._record_transactions(order)
        if order.get('_transactionsTruncated'):
            binding.reason = _('Shopify lists %(all)s payments on this order and Odoo read the first %(read)s. The '
                               'rest are not recorded.') % {'all': order['_transactionsTruncated'],
                                                            'read': len(order.get('transactions') or [])}
        if historical and not binding.sale_order_id:
            binding = binding.with_context(**{HISTORICAL: True, 'tracking_disable': True, 'mail_notrack': True})
        result = binding._apply_to_odoo(order)
        binding.fingerprint = digest
        return result

    def _apply_to_odoo(self, order):
        """Create or update the sales order. Implemented by the sales layer."""
        self.ensure_one()
        amounts = order_math.OrderAmounts(order, currency=self.currency_mode or 'shop',
                                          refunded_quantities=order.get('_refundedQuantities')).compute()
        self.write({'target_total': float(amounts['target_total']), 'currency_code': amounts['currency']})
        return {'prepared': self.shopify_gid, 'lines': len(amounts['lines'])}


class ShopifyStoreOrderPolling(models.Model):
    _inherit = 'eh.shopify.store'

    def _poll_orders(self, limit=5000, after=None, since=None):
        """Queue every order updated since the last check. Webhooks are faster;
        this makes sure nothing is ever missed. A sweep that hits ``limit``
        continues in another job from the cursor it stopped at, and the mark of
        what has been read moves only once the whole sweep is done."""
        self.ensure_one()
        now = fields.Datetime.now()
        since = fields.Datetime.to_datetime(since) if since else (
            (self.orders_polled_until or self.order_import_from or now - datetime.timedelta(days=1)) - POLL_OVERLAP)
        query = "updated_at:>='%s'" % since.strftime('%Y-%m-%dT%H:%M:%SZ')
        client = self._client()
        Order = self.env['eh.shopify.order']
        known = {}
        queued = unchanged = 0
        document = documents.ORDERS_UPDATED
        more, page_cursor = False, after
        for nodes, page_cursor, has_next in client.iter_pages(self._doc_text(document), {'query': query},
                                                        document.connection, page_size=POLL_PAGE, after=after):
            for node in nodes:
                gid = node['id']
                if gid not in known:
                    binding = Order.search([('store_id', '=', self.id), ('shopify_gid', '=', gid)], limit=1)
                    known[gid] = binding
                binding = known[gid]
                if binding and binding.shopify_updated_at \
                        and binding.shopify_updated_at >= _parse_dt(node.get('updatedAt')):
                    unchanged += 1
                    continue
                Order._enqueue_import(self, gid, priority=7)
                queued += 1
            if queued >= limit and has_next:
                more = True
                break
        if more:
            self.env['eh.shopify.job'].sudo()._enqueue(
                self, 'orders.poll', name=_('Read the rest of the changed Shopify orders'),
                payload={'after': page_cursor, 'since': fields.Datetime.to_string(since)},
                dedup_key='orders.poll:%s' % page_cursor, priority=7)
        else:
            self.sudo().write({'orders_polled_until': now})
        result = {'queued': queued, 'unchanged': unchanged, 'since': str(since)}
        if more:
            result['more'] = True
        return result

    def _job_orders_poll(self, job):
        payload = job.payload or {}
        return job.store_id._poll_orders(after=payload.get('after'), since=payload.get('since'))
