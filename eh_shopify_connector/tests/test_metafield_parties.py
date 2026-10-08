# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart


@tagged('post_install', '-at_install', 'eh_shopify')
class TestMetafieldParties(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.env['product.product'].create({'name': 'Party item', 'default_code': 'SKU-0'})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Map = self.env['eh.shopify.metafield.map']
        self.Order = self.env['eh.shopify.order']
        self.Customer = self.env['eh.shopify.customer']
        self.stamp = 0

    def _map(self, owner, key, model, field, direction='import'):
        return self.Map.create({'store_id': self.store.id, 'owner_type': owner, 'namespace': 'custom', 'key': key,
                                'metafield_type': 'single_line_text_field', 'direction': direction,
                                'field_id': self.env['ir.model.fields']._get(model, field).id})

    def _customer(self, number):
        gid = 'gid://shopify/Customer/%s' % number
        node = {'id': gid, 'displayName': 'Party %s' % number, 'firstName': 'Party', 'lastName': str(number),
                'locale': 'en', 'taxExempt': False, 'tags': [], 'note': None, 'state': 'ENABLED',
                'updatedAt': '2026-09-16T10:00:00Z',
                'defaultEmailAddress': {'emailAddress': 'party%s@example.com' % number, 'marketingState': 'SUBSCRIBED',
                                        'marketingUpdatedAt': None},
                'defaultPhoneNumber': None, 'defaultAddress': None}
        self.shop.customers[gid] = node
        return node

    def _import(self, order):
        if order['id'] not in self.shop.orders:
            self.shop.add_order(order)
        else:
            self.stamp += 1
            self.shop.orders[order['id']]['updatedAt'] = '2026-09-16T11:%02d:00Z' % self.stamp
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def _reads(self, name):
        return [r for r in self.fake.requests if name in ((r.get('payload') or {}).get('query') or '')]

    def test_order_metafields_follow_updates_and_a_customer_is_read_once(self):
        self._map('order', 'gift_message', 'sale.order', 'reference')
        self._map('customer', 'job_title', 'res.partner', 'function')
        customer = self._customer(31)
        self.shop.set_metafield(customer, 'custom', 'job_title', 'single_line_text_field', 'Head buyer')
        order = make_order(number=31)
        order['customer'] = dict(customer)
        self.shop.add_order(order)
        self.shop.set_metafield(self.shop.orders[order['id']], 'custom', 'gift_message', 'single_line_text_field',
                                'Happy birthday')
        binding = self._import(order)
        sale_order = binding.sale_order_id
        partner = binding.customer_binding_id.partner_id
        self.assertEqual((sale_order.reference, partner.function), ('Happy birthday', 'Head buyer'))
        self.shop.set_metafield(self.shop.orders[order['id']], 'custom', 'gift_message', 'single_line_text_field',
                                'Happy anniversary')
        self.shop.set_metafield(customer, 'custom', 'job_title', 'single_line_text_field', 'Owner')
        self._import(order)
        self.assertEqual(sale_order.reference, 'Happy anniversary')
        self.assertEqual(partner.function, 'Head buyer', 'an order reads its customer only the first time')
        self.assertEqual(len(self._reads('EhCustomerMetafields')), 1)

    def test_customer_updates_read_customer_metafields(self):
        self._map('customer', 'job_title', 'res.partner', 'function')
        customer = self._customer(32)
        self.shop.set_metafield(customer, 'custom', 'job_title', 'single_line_text_field', 'Chef')
        self.Customer._import_customer(self.store, customer)
        event = self.env['eh.shopify.event'].new({'store_id': self.store.id, 'topic': 'customers/update',
                                                  'webhook_id': 'w-32', 'payload': {'admin_graphql_api_id': customer['id']}})
        event._on_customer_change()
        job = self.env['eh.shopify.job'].search([('store_id', '=', self.store.id), ('job_type', '=', 'customer.sync')])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        binding = self.Customer.search([('shopify_gid', '=', customer['id'])])
        self.assertEqual(binding.partner_id.function, 'Chef')
        self.assertTrue(binding.metafields_read_at)

    def test_a_customer_shopify_did_not_return_is_read_again_later(self):
        self._map('customer', 'job_title', 'res.partner', 'function')
        customer = self._customer(34)
        order = make_order(number=34)
        order['customer'] = dict(customer)
        del self.shop.customers[customer['id']]
        binding = self._import(order)
        self.assertTrue(binding.customer_binding_id)
        self.assertFalse(binding.customer_binding_id.metafields_read_at)

    def test_no_mapping_means_no_extra_read(self):
        self._import(make_order(number=33))
        self.assertFalse(self._reads('EhOrderMetafields'))
        self.assertFalse(self._reads('EhCustomerMetafields'))

    def test_customer_and_order_metafields_are_import_only(self):
        with self.assertRaises(ValidationError):
            self._map('order', 'gift_message', 'sale.order', 'reference', direction='both')
        with self.assertRaises(ValidationError):
            self._map('customer', 'job_title', 'res.partner', 'function', direction='export')
