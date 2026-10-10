# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from ..lib import errors
from ..models import eh_shopify_order_import
from .common import ShopifyCase


def bag(amount, currency='USD'):
    return {'shopMoney': {'amount': str(amount), 'currencyCode': currency},
            'presentmentMoney': {'amount': str(amount), 'currencyCode': currency}}


def make_order(number=1, status='PAID', lines=1, test=False, risk=None, updated='2026-09-15T10:00:00Z',
               processed='2026-09-15T09:00:00Z', cancelled=None, transactions=None):
    gid = 'gid://shopify/Order/%s' % number
    items = [{'id': 'gid://shopify/LineItem/%s%03d' % (number, i), 'sku': 'SKU-%s' % i, 'name': 'Item %s' % i,
              'quantity': 1, 'currentQuantity': 1, 'isGiftCard': False, 'taxable': False, 'requiresShipping': True,
              'variant': {'id': 'gid://shopify/ProductVariant/%s' % i, 'sku': 'SKU-%s' % i, 'barcode': None,
                          'inventoryItem': {'id': 'gid://shopify/InventoryItem/%s' % i}},
              'product': {'id': 'gid://shopify/Product/%s' % i}, 'originalUnitPriceSet': bag('10.00'),
              'discountAllocations': [], 'taxLines': [], 'duties': []} for i in range(lines)]
    txs = transactions if transactions is not None else [
        {'id': 'gid://shopify/OrderTransaction/%s1' % number, 'kind': 'SALE', 'status': 'SUCCESS',
         'gateway': 'shopify_payments', 'manualPaymentGateway': False, 'test': False,
         'processedAt': processed, 'createdAt': processed, 'amountSet': bag('%.2f' % (10 * lines)),
         'parentTransaction': None}]
    return {
        'id': gid, 'legacyResourceId': number, 'name': '#%s' % (1000 + number), 'test': test,
        'createdAt': processed, 'processedAt': processed, 'updatedAt': updated, 'cancelledAt': cancelled,
        'displayFinancialStatus': status, 'displayFulfillmentStatus': 'UNFULFILLED', 'returnStatus': 'NO_RETURN',
        'sourceName': 'web', 'currencyCode': 'USD', 'presentmentCurrencyCode': 'USD', 'taxesIncluded': False,
        'dutiesIncluded': False, 'email': 'buyer%s@example.com' % number,
        'currentTotalPriceSet': bag('%.2f' % (10 * lines)), 'totalPriceSet': bag('%.2f' % (10 * lines)),
        'totalRefundedSet': bag('0.00'), 'totalTipReceivedSet': bag('0.00'),
        'risk': {'recommendation': risk or 'ACCEPT', 'assessments': [{'riskLevel': 'HIGH' if risk else 'LOW',
                                                                        'facts': []}]},
        'customerJourneySummary': {'ready': True, 'lastVisit': {'source': 'google', 'utmParameters': {
            'source': 'newsletter', 'medium': 'email', 'campaign': 'spring'}}},
        'transactionsCount': {'count': len(txs)}, 'transactions': txs,
        '_all': {'lineItems': items, 'discountApplications': [], 'shippingLines': [], 'fulfillmentOrders': []},
    }


@tagged('post_install', '-at_install', 'eh_shopify')
class TestOrderImport(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['product.product'].create([{'name': 'Item %s' % i, 'default_code': 'SKU-%s' % i} for i in range(50)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Job = self.env['eh.shopify.job']

    def _import(self, order):
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        return job, self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def test_nested_line_items_are_fully_fetched(self):
        order = self.shop.add_order(make_order(lines=45))
        fetched = self.Order._fetch_order(self.store, order['id'])
        self.assertEqual(len(fetched['lineItems']['nodes']), 45)
        self.assertEqual({r['payload']['query'].split('(')[0].split()[-1] for r in self.fake.requests
                          if 'payload' in r} >= {'EhOrder', 'EhOrderLineItems'}, True)

    def test_more_payments_than_one_read_returns_are_explained_not_refused(self):
        order = make_order()
        order['transactionsCount'] = {'count': 60}
        self.shop.add_order(order)
        fetched = self.Order._fetch_order(self.store, order['id'])
        self.assertEqual(fetched['_transactionsTruncated'], 60)
        job, binding = self._import(order)
        self.assertEqual((job.state, binding.state), ('done', 'imported'), job.last_error)
        self.assertIn('60', binding.reason, 'the merchant is told how many payments were not read')

    def test_paid_order_is_bound_with_transactions_and_attribution(self):
        job, binding = self._import(make_order())
        self.assertEqual(job.state, 'done')
        self.assertEqual(binding.name, '#1001')
        self.assertEqual((binding.utm_source, binding.utm_medium, binding.utm_campaign),
                         ('newsletter', 'email', 'spring'))
        self.assertEqual(binding.transaction_ids.kind, 'SALE')
        self.assertEqual(binding.transaction_ids.gateway_id.name, 'shopify_payments')
        self.assertEqual(binding.target_total, 10.0)

    def test_gates_explain_why_orders_are_not_imported(self):
        _job, test_order = self._import(make_order(number=2, test=True))
        self.assertEqual(test_order.state, 'skipped')
        self.assertIn('Test order', test_order.reason)
        _job, pending = self._import(make_order(number=3, status='VOIDED'))
        self.assertEqual(pending.state, 'skipped')
        self.assertIn('VOIDED', pending.reason)
        _job, cancelled = self._import(make_order(number=4, cancelled='2026-09-15T09:30:00Z'))
        self.assertIn('Cancelled', cancelled.reason)
        self.store.order_import_from = datetime.datetime(2026, 9, 16)
        _job, old = self._import(make_order(number=5))
        self.assertIn('import start date', old.reason)

    def test_risky_order_is_held(self):
        _job, binding = self._import(make_order(number=6, risk='CANCEL'))
        self.assertEqual(binding.state, 'held')
        self.assertEqual(binding.risk_level, 'HIGH')
        self.assertIn('fraud', binding.reason)

    def test_unchanged_order_is_not_reprocessed(self):
        order = make_order(number=7, status='VOIDED')
        self._import(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.result, {'unchanged': order['id']})

    def test_webhook_and_poller_share_one_job_per_order(self):
        order = self.shop.add_order(make_order(number=8, updated='2026-09-15T12:00:00Z'))
        event, _created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-o8', 'X-Shopify-Topic': 'orders/create'},
            {'id': 8, 'admin_graphql_api_id': order['id']})
        event.job_id._run_inline()
        self.store.orders_polled_until = datetime.datetime(2026, 9, 15, 11, 0, 0)
        result = self.store._poll_orders()
        self.assertEqual(result['queued'], 1)
        jobs = self.Job.search([('store_id', '=', self.store.id), ('job_type', '=', 'order.import')])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs.resource_key, order['id'])
        self.assertGreater(self.store.orders_polled_until, datetime.datetime(2026, 9, 15, 11, 0, 0))

    def test_poller_skips_orders_already_seen(self):
        order = make_order(number=9, status='VOIDED', updated='2026-09-15T12:00:00Z')
        self._import(order)
        self.store.orders_polled_until = datetime.datetime(2026, 9, 15, 11, 0, 0)
        self.assertEqual(self.store._poll_orders(), {'queued': 0, 'unchanged': 1,
                                                     'since': '2026-09-15 10:50:00'})

    def test_refunded_order_imports_as_sold_with_refund_detail(self):
        order = make_order(number=11, lines=2)
        order['totalRefundedSet'] = bag('10.00')
        order['_all']['lineItems'][0]['currentQuantity'] = 0
        order['_all']['refunds'] = [{'id': 'gid://shopify/Refund/1', 'lines': [
            {'quantity': 1, 'restockType': 'RETURN', 'lineItem': {'id': order['_all']['lineItems'][0]['id']}}]}]
        self.shop.add_order(order)
        fetched = self.Order._fetch_order(self.store, order['id'])
        self.assertEqual(fetched['_refundedQuantities'], {order['_all']['lineItems'][0]['id']: 1})

    def test_protected_customer_data_flags_the_order(self):
        order = make_order(number=12)
        self.shop.add_order(order)
        response = {'data': {'order': dict({k: v for k, v in order.items() if k != '_all'}, email=None, customer=None,
                                            lineItems={'nodes': order['_all']['lineItems'],
                                                       'pageInfo': {'hasNextPage': False, 'endCursor': None}})},
                    'errors': [{'message': 'This app is not approved to access the Customer object.',
                                'path': ['order', 'customer']}]}
        self.fake.script((200, {}, response))
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        binding = self.Order.search([('shopify_gid', '=', order['id'])])
        self.assertEqual(job.state, 'done')
        self.assertTrue(binding.customer_data_restricted)

    def test_order_topics_registered_when_orders_enabled(self):
        self.assertIn('ORDERS_CREATE', self.store._webhook_topics())
        self.store.orders_enabled = False
        self.assertNotIn('ORDERS_CREATE', self.store._webhook_topics())

    def test_a_long_sweep_continues_instead_of_stepping_over_orders(self):
        # A first sweep with no mark looks back one day from now, so the orders are stamped
        # relative to the clock; fixed dates would fall out of that window as the calendar moves.
        recent = (fields.Datetime.now() - datetime.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        for number in (61, 62, 63):
            self.shop.add_order(make_order(number=number, updated=recent, processed=recent))
        Job = self.env['eh.shopify.job']
        with patch.object(eh_shopify_order_import, 'POLL_PAGE', 1):
            result = self.store._poll_orders(limit=1)
        self.assertEqual((result['queued'], result.get('more')), (1, True))
        self.assertFalse(self.store.orders_polled_until, 'the mark of what was read waits for the whole sweep')
        follow_up = Job.search([('store_id', '=', self.store.id), ('job_type', '=', 'orders.poll'),
                                ('state', '=', 'pending')])
        self.assertTrue((follow_up.payload or {}).get('after'), 'the follow up carries the cursor')
        with patch.object(eh_shopify_order_import, 'POLL_PAGE', 1):
            follow_up._run_inline()
        self.assertEqual(follow_up.state, 'done', follow_up.last_error)
        self.assertTrue(self.store.orders_polled_until)
        self.assertEqual(Job.search_count([('store_id', '=', self.store.id), ('job_type', '=', 'order.import')]), 3,
                         'every order of the sweep was queued, none stepped over')
