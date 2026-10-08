# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import importlib
from unittest.mock import patch

from odoo.tests import tagged

from ..lib import documents
from .common import ShopifyCase

SYNC_JOBS = ('customer.sync', 'company.sync')
_partner_module = importlib.import_module('odoo.addons.base.models.res_partner')
BASE_PARTNER = getattr(_partner_module, 'ResPartner', None) or _partner_module.Partner
PROTECTED = 'This app is not approved to access the Customer object.'


@tagged('post_install', '-at_install', 'eh_shopify')
class TestCustomerSync(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.Customer = self.env['eh.shopify.customer']
        self.Job = self.env['eh.shopify.job']
        self.Blacklist = self.env['mail.blacklist'].with_context(active_test=False)

    def _customer(self, number, **values):
        gid = 'gid://shopify/Customer/%s' % number
        node = {'id': gid, 'displayName': 'Customer %s' % number, 'firstName': 'Customer', 'lastName': str(number),
                'locale': 'en', 'taxExempt': False, 'tags': [], 'note': None, 'state': 'ENABLED',
                'updatedAt': '2026-09-16T10:00:00Z',
                'defaultEmailAddress': {'emailAddress': 'customer%s@example.com' % number, 'marketingState': 'SUBSCRIBED',
                                        'marketingUpdatedAt': None},
                'defaultPhoneNumber': None, 'defaultAddress': None}
        node.update(values)
        self.shop.customers[gid] = node
        return node

    def _link(self, node):
        self.Customer._import_customer(self.store, node)
        return self.Customer.search([('store_id', '=', self.store.id), ('shopify_gid', '=', node['id'])])

    def _event(self, topic, payload):
        return self.env['eh.shopify.event'].new({'store_id': self.store.id, 'topic': topic,
                                                 'webhook_id': 'w-%s' % topic, 'payload': payload})

    def _run(self):
        jobs = self.Job.search([('store_id', '=', self.store.id), ('job_type', 'in', SYNC_JOBS),
                                ('state', '=', 'pending')], order='id')
        for job in jobs:
            job._run_inline()
            self.assertEqual(job.state, 'done', job.last_error)
        return jobs

    def _update(self, node):
        self.assertEqual(self._event('customers/update', {'admin_graphql_api_id': node['id']})._on_customer_change(),
                         {'queued': node['id']})
        return self._run()

    def _entry(self, email):
        return self.Blacklist.search([('email', '=', email)])

    def _consent(self, node, state):
        node['defaultEmailAddress']['marketingState'] = state
        return self._update(node)

    # ------------------------------------------------------------------ tags
    def test_tags_come_from_shopify_under_their_own_parent_and_leave_odoo_tags_alone(self):
        node = self._customer(1, tags=['vip', 'wholesale'])
        partner = self._link(node).partner_id
        self._update(node)
        self.assertEqual(set(partner.category_id.mapped('name')), {'vip', 'wholesale'})
        self.assertEqual(set(partner.category_id.mapped('parent_id.name')), {'Shopify'})
        manual = self.env['res.partner.category'].create({'name': 'Key account'})
        partner.category_id = [(4, manual.id)]
        node['tags'] = ['vip']
        self._update(node)
        self.assertEqual(set(partner.category_id.mapped('name')), {'vip', 'Key account'},
                         'only tags that came from Shopify are removed')

    def test_an_unchanged_update_does_not_write_the_contact(self):
        node = self._customer(2, tags=['vip'])
        self._link(node)
        self._update(node)
        original = BASE_PARTNER.write
        writes = []

        def counting(records, vals):
            writes.append(vals)
            return original(records, vals)

        with patch.object(BASE_PARTNER, 'write', counting):
            self._update(node)
        self.assertEqual(writes, [], 'a contact written on every update loses its first B2B company link')

    # ------------------------------------------------------------------ consent
    def test_consent_is_mirrored_on_changes_so_an_odoo_opt_in_sticks(self):
        self.store.mirror_consent = True
        node = self._customer(3)
        self._link(node)
        email = 'customer3@example.com'
        self._consent(node, 'UNSUBSCRIBED')
        self.assertTrue(self._entry(email).active)
        self.env['mail.blacklist']._remove(email)
        self._update(node)
        self.assertFalse(self._entry(email).active, 'an unchanged Shopify state does not undo an opt in made in Odoo')
        self._consent(node, 'SUBSCRIBED')
        self._consent(node, 'UNSUBSCRIBED')
        self.assertTrue(self._entry(email).active, 'a new unsubscribe in Shopify blacklists the address again')
        self._consent(node, 'SUBSCRIBED')
        self.assertFalse(self._entry(email).active)

    def test_a_blacklist_entry_made_in_odoo_is_never_lifted(self):
        self.store.mirror_consent = True
        self.env['mail.blacklist']._add('customer4@example.com')
        node = self._customer(4)
        self._link(node)
        self._consent(node, 'UNSUBSCRIBED')
        self._consent(node, 'SUBSCRIBED')
        self.assertTrue(self._entry('customer4@example.com').active)

    def test_an_email_change_moves_the_blacklist_entry(self):
        self.store.mirror_consent = True
        node = self._customer(5, defaultEmailAddress={'emailAddress': 'old5@example.com',
                                                      'marketingState': 'UNSUBSCRIBED', 'marketingUpdatedAt': None})
        self._link(node)
        self._update(node)
        self.assertTrue(self._entry('old5@example.com').active)
        node['defaultEmailAddress']['emailAddress'] = 'new5@example.com'
        self._update(node)
        self.assertFalse(self._entry('old5@example.com').active)
        self.assertTrue(self._entry('new5@example.com').active)
        self._consent(node, 'SUBSCRIBED')
        self.assertFalse(self._entry('new5@example.com').active)

    def test_another_store_still_unsubscribed_keeps_the_address_blacklisted(self):
        self.store.mirror_consent = True
        node = self._customer(6)
        binding = self._link(node)
        self._consent(node, 'UNSUBSCRIBED')
        second = self.env['eh.shopify.store'].create({'name': 'Second store', 'shop_domain': 'eh-second.myshopify.com',
                                                      'auth_mode': 'client_credentials', 'client_id': 'eh-second',
                                                      'mirror_consent': True})
        other = self.Customer.create({'store_id': second.id, 'shopify_gid': node['id'], 'partner_id': binding.partner_id.id,
                                      'email': 'customer6@example.com', 'email_marketing_state': 'UNSUBSCRIBED'})
        self._consent(node, 'SUBSCRIBED')
        self.assertTrue(self._entry('customer6@example.com').active)
        self.assertEqual((binding.blacklisted_email, other.blacklisted_email), (False, 'customer6@example.com'))

    def test_sms_consent_is_read_and_kept(self):
        self.assertIn('defaultPhoneNumber { phoneNumber marketingState }',
                      documents.CUSTOMER.text_for(self.store.api_version))
        node = self._customer(7, defaultPhoneNumber={'phoneNumber': '+61400000007', 'marketingState': 'SUBSCRIBED'})
        binding = self._link(node)
        self._update(node)
        self.assertEqual(binding.sms_marketing_state, 'SUBSCRIBED')

    def test_withheld_customer_data_keeps_what_odoo_knows_and_creates_nothing(self):
        node = self._customer(8)
        binding = self._link(node)
        withheld = dict(node, displayName=None, firstName=None, lastName=None, defaultEmailAddress=None,
                        defaultPhoneNumber=None, defaultAddress=None, tags=['vip'])
        self.fake.script((200, {}, {'data': {'customer': withheld},
                                    'errors': [{'message': PROTECTED, 'path': ['customer', 'defaultEmailAddress']}]}))
        self._update(node)
        self.assertEqual((binding.email, binding.display_name_shopify), ('customer8@example.com', 'Customer 8'))
        self.assertEqual(binding.partner_id.category_id.mapped('name'), ['vip'])
        self.store.customers_sync_all = True
        stranger = self._customer(9)
        self.fake.script((200, {}, {'data': {'customer': dict(stranger, displayName=None, defaultEmailAddress=None)},
                                    'errors': [{'message': PROTECTED}]}))
        jobs = self._update(stranger)
        self.assertEqual(jobs.result, {'skipped': 'Shopify withheld protected customer data'})
        self.assertFalse(self.Customer.search([('shopify_gid', '=', stranger['id'])]))

    # ------------------------------------------------------------------ lifecycle
    def test_customers_not_linked_are_created_only_when_the_store_asks(self):
        node = self._customer(10)
        self.assertEqual(self._event('customers/create', {'admin_graphql_api_id': node['id']})._on_customer_change(),
                         {'skipped': 'the customer is not linked in Odoo'})
        self.assertFalse(self.Job.search([('store_id', '=', self.store.id), ('job_type', '=', 'customer.sync')]))
        self.store.customers_sync_all = True
        self._update(node)
        self.assertTrue(self.Customer.search([('shopify_gid', '=', node['id'])]).partner_id)

    def test_a_deleted_customer_keeps_its_contact_and_loses_its_shopify_tags(self):
        node = self._customer(11, tags=['vip'])
        binding = self._link(node)
        partner = binding.partner_id
        self._update(node)
        self.assertEqual(self._event('customers/delete', {'id': 11})._on_customer_delete(), {'removed': node['id']})
        self.assertTrue(binding.removed_in_shopify)
        self.assertTrue(partner.exists())
        self.assertFalse(partner.category_id)

    def test_a_merge_queues_behind_the_kept_customer_and_moves_the_link(self):
        node = self._customer(12)
        binding = self._link(node)
        kept = self._customer(13)
        payload = {'admin_graphql_api_customer_kept_id': kept['id'],
                   'admin_graphql_api_customer_deleted_id': node['id'], 'status': 'COMPLETED'}
        event, created = self.env['eh.shopify.event']._receive(
            self.store, {'X-Shopify-Webhook-Id': 'w-merge-12', 'X-Shopify-Topic': 'customers/merge'}, payload)
        self.assertTrue(created)
        self.assertEqual(event.job_id.resource_key, kept['id'])
        # The delivered event runs first; the customer update it queues waits behind it on the same resource.
        event.job_id._run_inline()
        self.assertEqual(event.job_id.state, 'done', event.job_id.last_error)
        self.assertEqual((binding.shopify_gid, binding.removed_in_shopify), (kept['id'], False))
        self._run()
        self.assertEqual(self.Customer.search([('store_id', '=', self.store.id), ('shopify_gid', '=', kept['id'])]),
                         binding)

    def test_a_merge_into_a_linked_customer_retires_the_old_link(self):
        old_node = self._customer(14, tags=['vip'])
        old = self._link(old_node)
        self._update(old_node)
        kept = self._customer(15)
        new = self._link(kept)
        self._event('customers/merge', {'admin_graphql_api_customer_kept_id': kept['id'],
                                        'admin_graphql_api_customer_deleted_id': old_node['id'],
                                        'status': 'COMPLETED'})._on_customer_merge()
        self.assertEqual((old.shopify_gid, old.removed_in_shopify), (old_node['id'], True))
        self.assertEqual((new.shopify_gid, new.removed_in_shopify), (kept['id'], False))
        self.assertFalse(old.partner_id.category_id)

    # ------------------------------------------------------------------ companies
    def _company_update(self, gid):
        self.assertEqual(self._event('companies/update', {'admin_graphql_api_id': gid})._on_company_change(),
                         {'queued': gid})
        return self._run()

    def test_company_updates_refresh_the_company_the_connector_created(self):
        partner = self.env['res.partner'].create({'name': 'Old Engines', 'is_company': True})
        gid = 'gid://shopify/Company/9'
        binding = self.Customer.create({'store_id': self.store.id, 'kind': 'company', 'shopify_gid': gid,
                                        'partner_id': partner.id, 'created_by_connector': True})
        self.shop.companies[gid] = {'id': gid, 'name': 'New Engines', 'externalId': 'NE-9', 'note': None}
        self._company_update(gid)
        self.assertEqual((partner.name, partner.ref, binding.display_name_shopify), ('New Engines', 'NE-9', 'New Engines'))
        del self.shop.companies[gid]
        self._company_update(gid)
        self.assertTrue(binding.removed_in_shopify)

    def test_a_company_found_in_odoo_keeps_its_name_and_reference(self):
        partner = self.env['res.partner'].create({'name': 'Harbour Mills', 'is_company': True, 'ref': 'C-42'})
        gid = 'gid://shopify/Company/10'
        self.Customer.create({'store_id': self.store.id, 'kind': 'company', 'shopify_gid': gid, 'partner_id': partner.id})
        self.shop.companies[gid] = {'id': gid, 'name': 'Harbour Mills Pty', 'externalId': 'HM-10', 'note': None}
        self._company_update(gid)
        self.assertEqual((partner.name, partner.ref), ('Harbour Mills', 'C-42'))

    def test_customer_topics_follow_orders_and_the_customer_permission(self):
        self.store.orders_enabled = True
        self.store.granted_scopes = 'read_orders,read_customers'
        topics = self.store._webhook_topics()
        self.assertIn('CUSTOMERS_UPDATE', topics)
        self.assertEqual(topics[-1], 'COMPANIES_DELETE', 'customer topics come last')
        self.store.granted_scopes = 'read_orders'
        self.assertNotIn('CUSTOMERS_UPDATE', self.store._webhook_topics())
        self.store.granted_scopes = 'read_orders,read_customers'
        self.store.orders_enabled = False
        self.assertNotIn('CUSTOMERS_UPDATE', self.store._webhook_topics())
