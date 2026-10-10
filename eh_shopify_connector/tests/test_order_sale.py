# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import copy

from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import bag, make_order


def ensure_chart(env, company):
    if 'chart_template' in company._fields:  # 17+
        if not company.chart_template:
            env['account.chart.template'].try_loading('generic_coa', company=company, install_demo=False)
    elif not company.chart_template_id:  # 16
        template = env.ref('l10n_generic_coa.configurable_chart_template')
        template.try_loading(company=company, install_demo=False)


def tax_line(rate, amount):
    return {'title': 'Sales tax', 'rate': rate, 'ratePercentage': rate * 100, 'channelLiable': False,
            'source': 'Shopify', 'priceSet': bag(amount)}


@tagged('post_install', '-at_install', 'eh_shopify')
class TestOrderToSale(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)],
                                                     limit=1)
        cls.products = cls.env['product.product'].create([
            {'name': 'Brass gear %s' % i, 'default_code': 'SKU-%s' % i} for i in range(3)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.Order = self.env['eh.shopify.order']
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})

    def _import(self, order):
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        return job, self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def test_paid_order_is_confirmed_invoiced_and_paid(self):
        job, binding = self._import(make_order(number=21, lines=2))
        self.assertEqual(job.state, 'done', job.last_error)
        so = binding.sale_order_id
        self.assertEqual(so.state, 'sale')
        self.assertEqual(so.amount_total, 20.0)
        self.assertEqual(binding.parity_state, 'ok')
        self.assertEqual(so.client_order_ref, '#1021')
        self.assertEqual(str(so.date_order), '2026-09-15 09:00:00')
        self.assertEqual(len(binding.line_ids.filtered(lambda l: l.kind == 'product')), 2)
        invoice = so.invoice_ids
        self.assertEqual(invoice.state, 'posted')
        transaction = binding.transaction_ids
        self.assertEqual(transaction.state, 'recorded', transaction.note)
        self.assertTrue(compat.payment_is_confirmed(transaction.payment_id))
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))

    def test_taxes_shipping_and_discount_match_shopify(self):
        order = make_order(number=22, lines=1)
        item = order['_all']['lineItems'][0]
        item.update({'quantity': 2, 'currentQuantity': 2,
                     'discountAllocations': [{'allocatedAmountSet': bag('2.00'), 'discountApplication': {'index': 0}}],
                     'taxLines': [tax_line(0.10, '1.80')]})
        order['_all']['shippingLines'] = [{'id': 'gid://shopify/ShippingLine/1', 'title': 'Express', 'code': 'EXP',
                                           'source': 'shopify', 'isRemoved': False, 'originalPriceSet': bag('5.00'),
                                           'discountedPriceSet': bag('5.00'), 'discountAllocations': [],
                                           'taxLines': [tax_line(0.10, '0.50')]}]
        order['currentTotalPriceSet'] = order['totalPriceSet'] = bag('25.30')
        order['transactions'][0]['amountSet'] = bag('25.30')
        job, binding = self._import(order)
        self.assertEqual(job.state, 'done', job.last_error)
        so = binding.sale_order_id
        self.assertAlmostEqual(so.amount_total, 25.30, places=2)
        self.assertEqual(binding.parity_state, 'ok')
        product_line = so.order_line.filtered(lambda l: l.product_id == self.products[0])
        self.assertEqual(product_line.discount, 10.0)
        shipping = so.order_line.filtered(lambda l: l.name == 'Express')
        self.assertEqual(shipping.price_unit, 5.0)
        _uom_field, tax_field = compat.sale_line_names(self.env)
        self.assertEqual(shipping[tax_field].amount, 10.0)

    def test_tax_included_prices(self):
        order = make_order(number=23, lines=1)
        order['taxesIncluded'] = True
        item = order['_all']['lineItems'][0]
        item.update({'originalUnitPriceSet': bag('11.00'), 'taxLines': [tax_line(0.10, '1.00')]})
        order['currentTotalPriceSet'] = order['totalPriceSet'] = bag('11.00')
        order['transactions'][0]['amountSet'] = bag('11.00')
        job, binding = self._import(order)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertAlmostEqual(binding.sale_order_id.amount_total, 11.0, places=2)
        _uom_field, tax_field = compat.sale_line_names(self.env)
        self.assertTrue(binding.sale_order_id.order_line[:1][tax_field].price_include)

    def test_unmatched_product_stops_then_is_created_on_request(self):
        order = make_order(number=24, lines=1)
        item = order['_all']['lineItems'][0]
        item['sku'] = 'NEW-SKU'
        item['variant'].update({'id': 'gid://shopify/ProductVariant/999', 'sku': 'NEW-SKU'})
        job, binding = self._import(order)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.error_kind, 'mapping')
        self.assertIn('NEW-SKU', job.last_error)
        self.store.unmatched_product = 'create'
        job.action_retry()
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertTrue(self.env['product.product'].search([('default_code', '=', 'NEW-SKU')]))

    def test_total_mismatch_keeps_a_quotation_with_the_reason(self):
        order = make_order(number=25, lines=1)
        order['currentTotalPriceSet'] = order['totalPriceSet'] = bag('15.00')
        job, binding = self._import(order)
        self.assertEqual(job.state, 'done')
        self.assertEqual(binding.state, 'error')
        self.assertEqual(binding.parity_state, 'mismatch')
        self.assertEqual(binding.sale_order_id.state, 'draft')
        self.assertFalse(binding.sale_order_id.invoice_ids)
        self.assertIn('differs', binding.reason)

    def test_discount_rounding_is_absorbed_by_a_visible_line(self):
        order = make_order(number=26, lines=1)
        item = order['_all']['lineItems'][0]
        item.update({'originalUnitPriceSet': bag('1000.00'),
                     'discountAllocations': [{'allocatedAmountSet': bag('333.33'),
                                              'discountApplication': {'index': 0}}]})
        order['currentTotalPriceSet'] = order['totalPriceSet'] = bag('666.67')
        order['transactions'][0]['amountSet'] = bag('666.67')
        job, binding = self._import(order)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(binding.parity_state, 'adjusted')
        self.assertAlmostEqual(binding.sale_order_id.amount_total, 666.67, places=2)
        self.assertTrue(binding.sale_order_id.order_line.filtered(lambda l: l.name == 'Rounding to match Shopify'))

    def test_pending_payment_is_invoiced_when_paid(self):
        order = make_order(number=27, lines=1, status='PENDING', transactions=[])
        job, binding = self._import(order)
        self.assertEqual(binding.sale_order_id.state, 'sale')
        self.assertFalse(binding.sale_order_id.invoice_ids)
        paid = copy.deepcopy(order)
        paid.update({'displayFinancialStatus': 'PAID', 'updatedAt': '2026-09-15T11:00:00Z',
                     'transactionsCount': {'count': 1},
                     'transactions': [{'id': 'gid://shopify/OrderTransaction/271', 'kind': 'SALE', 'status': 'SUCCESS',
                                       'gateway': 'shopify_payments', 'manualPaymentGateway': False, 'test': False,
                                       'processedAt': '2026-09-15T10:59:00Z', 'createdAt': '2026-09-15T10:59:00Z',
                                       'amountSet': bag('10.00'), 'parentTransaction': None}]})
        job, binding = self._import(paid)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(binding.sale_order_id.invoice_ids.state, 'posted')
        self.assertEqual(binding.transaction_ids.state, 'recorded')

    def test_payment_method_without_journal_does_not_block_the_order(self):
        order = make_order(number=28, lines=1)
        order['transactions'][0]['gateway'] = 'paypal'
        job, binding = self._import(order)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(binding.sale_order_id.state, 'sale')
        self.assertEqual(binding.transaction_ids.state, 'error')
        self.assertIn('paypal', binding.transaction_ids.note)

    def test_cancelled_in_shopify_cancels_the_open_order(self):
        self.store.invoice_policy = 'manual'
        order = make_order(number=29, lines=1)
        job, binding = self._import(order)
        self.assertEqual(binding.sale_order_id.state, 'sale')
        cancelled = copy.deepcopy(order)
        cancelled.update({'cancelledAt': '2026-09-15T12:00:00Z', 'updatedAt': '2026-09-15T12:00:00Z'})
        job, binding = self._import(cancelled)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(binding.state, 'cancelled')
        self.assertEqual(binding.sale_order_id.state, 'cancel')

    def test_tip_and_gift_card_use_service_products(self):
        order = make_order(number=30, lines=1)
        order['_all']['lineItems'].append({
            'id': 'gid://shopify/LineItem/30999', 'title': 'Tip', 'name': 'Tip', 'sku': None, 'vendor': None,
            'product': None, 'variant': None, 'requiresShipping': False, 'quantity': 1, 'currentQuantity': 1,
            'isGiftCard': False, 'taxable': False, 'originalUnitPriceSet': bag('3.00'), 'discountAllocations': [],
            'taxLines': [], 'duties': []})
        order['_all']['lineItems'].append({
            'id': 'gid://shopify/LineItem/30998', 'title': 'Gift card', 'name': 'Gift card 50', 'sku': None,
            'vendor': 'Store', 'product': {'id': 'gid://shopify/Product/77'}, 'variant': None,
            'requiresShipping': False, 'quantity': 1, 'currentQuantity': 1, 'isGiftCard': True, 'taxable': False,
            'originalUnitPriceSet': bag('50.00'), 'discountAllocations': [], 'taxLines': [], 'duties': []})
        order['currentTotalPriceSet'] = order['totalPriceSet'] = bag('63.00')
        order['transactions'][0]['amountSet'] = bag('63.00')
        job, binding = self._import(order)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(binding.parity_state, 'ok')
        self.assertEqual(set(binding.line_ids.mapped('kind')), {'product', 'tip', 'gift_card'})
        self.assertEqual(self.store.tip_product_id.type, 'service')
