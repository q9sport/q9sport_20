# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from odoo import fields
from odoo.tests import tagged

from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart

EXPORT_JOBS = ('bulk.start', 'bulk.poll', 'bulk.read', 'bulk.chunk')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestHistory(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        cls.env['product.product'].create([{'name': 'History gear %s' % i, 'default_code': 'SKU-%s' % i}
                                           for i in range(3)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.order_import_from = fields.Datetime.now()
        self.Order = self.env['eh.shopify.order']
        self.Bulk = self.env['eh.shopify.bulk']
        self.Job = self.env['eh.shopify.job']
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})

    def _drain(self):
        for _attempt in range(100):
            job = self.Job.search([('store_id', '=', self.store.id), ('state', '=', 'pending'),
                                   ('job_type', 'in', EXPORT_JOBS)], order='id', limit=1)
            if not job:
                return
            job._run_inline()
            self.assertEqual(job.state, 'done', '%s: %s' % (job.job_type, job.last_error))
        self.fail('export jobs did not finish')

    def _export(self, kind, rows):
        start = self.Job.search([('dedup_key', '=', 'bulk.start:%s' % kind), ('state', '=', 'pending')])
        start._run_inline()
        self.assertEqual(start.state, 'done', start.last_error)
        bulk = self.Bulk.search([('store_id', '=', self.store.id), ('kind', '=', kind)])
        self.shop.finish_bulk(bulk.operation_gid, rows)
        self._drain()
        return bulk

    def test_order_history_is_imported_without_moving_stock(self):
        old = make_order(number=101, lines=1)
        self.shop.add_order(old)
        self.store.history_orders_from = datetime.date(2026, 1, 1)
        self.store.action_import_order_history()
        bulk = self._export('order_ids', [{'id': old['id']}])
        self.assertIn("created_at:>='2026-01-01'", self.shop.bulk_queries[-1])
        self.assertEqual(bulk.state, 'done', bulk.note)
        imports = self.Job.search([('job_type', '=', 'order.import'), ('state', '=', 'pending')])
        self.assertEqual(len(imports), 1)
        self.assertTrue(imports.payload.get('historical'))
        imports._run_inline()
        self.assertEqual(imports.state, 'done', imports.last_error)
        binding = self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', old['id'])])
        sale_order = binding.sale_order_id
        self.assertEqual(binding.state, 'imported', binding.reason)
        self.assertTrue(sale_order.locked if 'locked' in sale_order._fields else sale_order.state == 'done')
        self.assertFalse(sale_order.picking_ids.filtered(lambda p: p.state != 'cancel'), 'no stock moves')
        self.assertEqual(sale_order.invoice_ids.state, 'posted')
        self.assertFalse(self.Job.search([('job_type', 'in', ('fulfillment.push', 'fulfillment.import'))]))

    def test_orders_already_in_odoo_are_not_imported_again_and_new_ones_keep_the_start_date(self):
        order = make_order(number=102, lines=1)
        self.shop.add_order(order)
        normal = self.Order._enqueue_import(self.store, order['id'])
        normal._run_inline()
        binding = self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])
        self.assertEqual(binding.state, 'skipped', 'a normal import still keeps the start date')
        self.store.order_import_from = False
        normal = self.Order._enqueue_import(self.store, order['id'])
        order['updatedAt'] = '2026-09-16T11:00:00Z'
        normal._run_inline()
        self.assertTrue(binding.sale_order_id)
        self.store.history_orders_from = datetime.date(2026, 1, 1)
        self.store.action_import_order_history()
        self._export('order_ids', [{'id': order['id']}])
        self.assertFalse(self.Job.search([('job_type', '=', 'order.import'), ('state', '=', 'pending')]))

    def test_customers_are_linked_or_created_from_an_export(self):
        known = self.env['res.partner'].create({'name': 'Known buyer', 'email': 'known@example.com'})
        self.store.action_import_customers()
        rows = [
            {'id': 'gid://shopify/Customer/1', 'firstName': 'Known', 'lastName': 'Buyer', 'displayName': 'Known Buyer',
             'locale': 'en', 'taxExempt': False, 'tags': ['vip'],
             'defaultEmailAddress': {'emailAddress': 'Known@Example.com', 'marketingState': 'SUBSCRIBED'},
             'defaultPhoneNumber': None, 'defaultAddress': None},
            {'id': 'gid://shopify/Customer/2', 'firstName': 'New', 'lastName': 'Buyer', 'displayName': 'New Buyer',
             'locale': 'en', 'taxExempt': False, 'tags': [],
             'defaultEmailAddress': {'emailAddress': 'new.buyer@example.com', 'marketingState': 'NOT_SUBSCRIBED'},
             'defaultPhoneNumber': {'phoneNumber': '+61400000000', 'marketingState': 'NOT_SUBSCRIBED'},
             'defaultAddress': {'firstName': 'New', 'lastName': 'Buyer', 'company': None, 'address1': '1 Test St',
                                'address2': None, 'city': 'Melbourne', 'zip': '3000', 'provinceCode': 'VIC',
                                'countryCodeV2': 'AU', 'phone': None}},
        ]
        bulk = self._export('customers', rows)
        self.assertEqual(bulk.state, 'done', bulk.note)
        Customer = self.env['eh.shopify.customer']
        first = Customer.search([('store_id', '=', self.store.id), ('shopify_gid', '=', rows[0]['id'])])
        self.assertEqual((first.partner_id, first.match_method), (known, 'email'))
        second = Customer.search([('store_id', '=', self.store.id), ('shopify_gid', '=', rows[1]['id'])])
        self.assertEqual(second.match_method, 'created')
        self.assertEqual((second.partner_id.city, second.partner_id.country_id.code), ('Melbourne', 'AU'))
        self.assertEqual(second.partner_id.phone, '+61400000000')
