# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .common import ShopifyCase
from .test_fulfillment_import import LOCATION, shopify_fulfillment
from .test_order_import import bag, make_order
from .test_order_sale import ensure_chart

STAMP = '2026-09-15T09:00:00Z'


def cash(number, kind, amount):
    return {'id': 'gid://shopify/OrderTransaction/%s' % number, 'kind': kind, 'status': 'SUCCESS', 'gateway': 'cash',
            'manualPaymentGateway': True, 'test': False, 'processedAt': STAMP, 'createdAt': STAMP,
            'amountSet': bag('%.2f' % amount), 'parentTransaction': None}


@tagged('post_install', '-at_install', 'eh_shopify')
class TestPointOfSaleOrders(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.env['product.product'].create([{'name': 'Till gear %s' % i, 'default_code': 'SKU-%s' % i} for i in range(3)])
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Job = self.env['eh.shopify.job']
        Location = self.env['eh.shopify.location']
        vals = {'store_id': self.store.id, 'name': 'Shop counter', 'shopify_gid': LOCATION, 'active_in_shopify': True,
                'warehouse_id': self.warehouse.id, 'delivery_mode': 'odoo', 'sync_stock': False}
        self.location = Location.search([('store_id', '=', self.store.id), ('shopify_gid', '=', LOCATION)])
        if self.location:
            self.location.write(vals)
        else:
            self.location = Location.create(vals)
        for name in ('shopify_payments', 'cash'):
            self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': name, 'journal_id': self.bank.id})

    def _pos_order(self, number, transactions=None):
        order = make_order(number=number, lines=1, transactions=transactions)
        order.update({'sourceName': 'pos', 'retailLocation': {'id': LOCATION, 'name': 'Shop counter'},
                      'displayFulfillmentStatus': 'FULFILLED', 'email': None})
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1)]
        return order

    def _import(self, order):
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def test_till_sales_without_a_customer_share_one_walk_in_contact(self):
        first = self._import(self._pos_order(95))
        second = self._import(self._pos_order(96))
        self.assertTrue(first.is_pos)
        partner = self.store.pos_walk_in_partner_id
        self.assertTrue(partner)
        self.assertEqual((first.sale_order_id.partner_id, second.sale_order_id.partner_id), (partner, partner))

    def test_goods_sold_at_a_till_leave_stock_at_the_sale(self):
        binding = self._import(self._pos_order(97))
        self.assertEqual(self.location.delivery_mode, 'odoo', 'the till is not set to Shopify ships')
        job = self.Job.search([('job_type', '=', 'fulfillment.import'),
                               ('dedup_key', '=', 'fulfillment-import:%s' % binding.shopify_gid)])
        self.assertTrue(job)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        delivery = binding.sale_order_id.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing')
        self.assertEqual(delivery.state, 'done')
        self.assertFalse(self.Job.search([('job_type', '=', 'fulfillment.push'), ('state', '=', 'pending')]),
                         'nothing is sent back to Shopify')

    def test_cash_given_back_as_change_is_not_a_payment(self):
        self.store.invoice_policy = 'paid'
        binding = self._import(self._pos_order(98, transactions=[cash(981, 'SALE', 50.0), cash(982, 'CHANGE', 40.0)]))
        sale = binding.transaction_ids.filtered(lambda t: t.kind == 'SALE')
        self.assertEqual(sale.state, 'recorded', sale.note)
        self.assertAlmostEqual(sale.payment_id.amount, 10.0, places=2)
        self.assertIn(binding.sale_order_id.invoice_ids.payment_state, ('paid', 'in_payment'))
