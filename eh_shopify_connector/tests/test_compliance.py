# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from odoo import fields
from odoo.tests import tagged

from .common import ShopifyCase


@tagged('post_install', '-at_install', 'eh_shopify')
class TestCompliance(ShopifyCase):
    """The three requests Shopify sends about personal data, and what Odoo keeps."""

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.Customer = self.env['eh.shopify.customer']
        self.Event = self.env['eh.shopify.event']

    def _customer(self, number):
        gid = 'gid://shopify/Customer/%s' % number
        node = {'id': gid, 'displayName': 'Jo Buyer', 'firstName': 'Jo', 'lastName': 'Buyer', 'locale': 'en',
                'taxExempt': False, 'tags': ['vip'], 'note': None, 'state': 'ENABLED',
                'updatedAt': '2026-09-16T10:00:00Z',
                'defaultEmailAddress': {'emailAddress': 'jo%s@example.com' % number, 'marketingState': 'SUBSCRIBED',
                                        'marketingUpdatedAt': None},
                'defaultPhoneNumber': {'phoneNumber': '+6140000000%s' % number, 'marketingState': 'SUBSCRIBED'},
                'defaultAddress': None}
        self.shop.customers[gid] = node
        self.Customer._import_customer(self.store, node)
        binding = self.Customer.search([('store_id', '=', self.store.id), ('shopify_gid', '=', gid)])
        binding._apply_tags(['vip'])
        return gid, binding

    def _event(self, topic, payload, webhook_id, resource_gid=False):
        return self.Event.sudo().create({'store_id': self.store.id, 'topic': topic, 'webhook_id': webhook_id,
                                         'payload': payload, 'resource_gid': resource_gid})

    def test_a_data_request_becomes_a_to_do_and_keeps_no_personal_data(self):
        gid, binding = self._customer(1)
        event = self._event('customers/data_request',
                            {'customer': {'admin_graphql_api_id': gid, 'email': 'jo1@example.com'},
                             'orders_requested': [1001]}, 'w-data-request')
        self.assertEqual(event._on_customer_data_request(), {'data_request': gid})
        activity = binding.partner_id.activity_ids
        self.assertTrue(activity, 'the merchant is asked to send what Odoo holds')
        self.assertIn('1001', activity.note or '', 'the orders named in the request are listed')
        self.assertEqual(event.payload, {'handled': 'customers/data_request'},
                         'the delivery itself keeps no personal data')

    def test_an_erasure_request_clears_what_the_connector_kept(self):
        gid, binding = self._customer(2)
        partner = binding.partner_id
        self.assertTrue(partner.category_id)
        earlier = self._event('customers/update', {'admin_graphql_api_id': gid}, 'w-earlier', resource_gid=gid)
        event = self._event('customers/redact', {'customer': {'admin_graphql_api_id': gid}}, 'w-redact')
        self.assertEqual(event._on_customer_redact(), {'redacted': gid})
        self.assertEqual((binding.email, binding.phone, binding.tags), (False, False, False))
        self.assertTrue(binding.redacted_at)
        self.assertEqual(earlier.payload, {'handled': 'customers/redact'},
                         'deliveries about that customer keep nothing either')
        self.assertFalse(partner.category_id, 'the tags that came from Shopify are gone')
        self.assertTrue(partner.exists(), 'the contact belongs to the merchant and stays')
        self.assertTrue(partner.activity_ids, 'the merchant is asked what else to erase')

    def test_a_shop_erasure_removes_the_credentials_and_the_deliveries(self):
        gid, _binding = self._customer(3)
        earlier = self._event('customers/update', {'admin_graphql_api_id': gid}, 'w-shop-earlier', resource_gid=gid)
        event = self._event('shop/redact', {'shop_domain': self.domain}, 'w-shop-redact')
        self.assertEqual(event._on_shop_redact(), {'redacted': self.domain})
        self.assertEqual(self.store.state, 'disconnected')
        self.assertEqual(earlier.payload, {'handled': 'shop/redact'})
        self.assertFalse(self.env['eh.shopify.credential'].sudo().search_count([('store_id', '=', self.store.id)]),
                         'nothing is left to connect with')

    def test_old_work_keeps_its_reason_but_not_the_customer_data(self):
        Job = self.env['eh.shopify.job']
        job = Job._enqueue(self.store, 'order.import', dedup_key='order:purge',
                           payload={'gid': 'gid://shopify/Order/1', 'email': 'jo@example.com'})
        job.write({'state': 'failed', 'last_error': 'Shopify said no'})
        event = self._event('orders/updated', {'admin_graphql_api_id': 'gid://shopify/Order/1'}, 'w-old')
        event.write({'state': 'failed'})
        # Odoo writes late, so the pending changes go to the database before their date is moved back.
        self.env.flush_all()
        old = fields.Datetime.now() - datetime.timedelta(days=90)
        self.env.cr.execute("UPDATE eh_shopify_job SET write_date = %s WHERE id = %s", (old, job.id))
        self.env.cr.execute("UPDATE eh_shopify_event SET received_at = %s, write_date = %s WHERE id = %s",
                            (old, old, event.id))
        job.invalidate_recordset()
        event.invalidate_recordset()
        counts = Job._cron_purge()
        self.assertEqual(job.payload, {'purged': True}, 'the job payload is cleared')
        self.assertEqual(event.payload, {'purged': True}, 'the delivery payload is cleared')
        self.assertGreaterEqual(counts['payloads'], 2)
        self.assertEqual(job.last_error, 'Shopify said no', 'what went wrong stays readable')
