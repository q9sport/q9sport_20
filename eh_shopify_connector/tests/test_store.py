# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime
import time
from urllib.parse import parse_qs, urlparse

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from ..lib import crypto, errors, hmac_util, testing
from .common import ShopifyCase


@tagged('post_install', '-at_install', 'eh_shopify')
class TestStoreConnection(ShopifyCase):

    def test_client_credentials_connect_and_token_reuse(self):
        self.store._check_connection()
        self.assertEqual(self.store.state, 'connected')
        self.assertEqual(self.store.shop_name, 'EH Test Store')
        self.assertEqual(self.store.currency_code, 'USD')
        self.assertEqual(self.store.served_api_version, '2026-07')
        self.assertFalse(self.store.missing_required_scopes)
        cred = self.store._credential()
        self.assertTrue(cred.access_token.startswith(crypto.PREFIX))
        self.assertNotIn('cc_1', cred.access_token)
        self.assertEqual(self.fake.requests[-1]['headers']['X-Shopify-Access-Token'], 'cc_1')
        self.assertGreater(cred.token_expires_at, fields.Datetime.now() + datetime.timedelta(hours=23))
        self.store._check_connection()
        self.assertEqual(self.shop.issued, ['cc_1'])

    def test_token_renewed_before_expiry(self):
        self.store._check_connection()
        cred = self.store._credential()
        cred.token_expires_at = fields.Datetime.now() + datetime.timedelta(seconds=30)
        self.store._check_connection()
        self.assertEqual(self.shop.issued, ['cc_1', 'cc_2'])

    def test_other_organisation_explained(self):
        self.shop.same_organisation = False
        action = self.store.action_check_connection()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertIn('organisation', self.store.last_error)
        self.assertEqual(self.store.state, 'draft')

    def test_missing_scopes_need_attention(self):
        self.shop.scopes.remove('read_customers')
        self.store._check_connection()
        self.assertEqual(self.store.state, 'attention')
        self.assertIn('read_customers', self.store.missing_required_scopes)
        self.assertEqual(self.store.health, 'error')

    def test_write_scope_implies_read(self):
        self.shop.scopes.remove('read_orders')
        self.store._check_connection()
        self.assertEqual(self.store.state, 'connected')
        self.assertNotIn('read_orders', self.store.missing_required_scopes)

    def test_credentials_for_another_store_rejected(self):
        self.shop.info['myshopifyDomain'] = 'someone-else.myshopify.com'
        with self.assertRaises(errors.AuthError):
            self.store._check_connection()

    def test_domain_rules(self):
        Store = self.env['eh.shopify.store']
        second = Store.create({'shop_domain': 'https://admin.shopify.com/store/Second-Shop/orders'})
        self.assertEqual(second.shop_domain, 'second-shop.myshopify.com')
        self.assertEqual(second.name, 'second-shop')
        with self.assertRaises(ValidationError):
            Store.create({'shop_domain': 'www.brand.com'})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), self.env.cr.savepoint():
            Store.create({'shop_domain': 'second-shop.myshopify.com'})
            self.env.flush_all()

    def test_health_warns_about_database_key(self):
        self.store._check_connection()
        self.assertEqual(self.store.vault_key_source, 'database_secret')
        self.assertIn('eh_shopify_key', self.store.health_notes)


@tagged('post_install', '-at_install', 'eh_shopify')
class TestLocationsAndWebhooks(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.store._check_connection()

    def test_sync_locations_is_idempotent(self):
        base = self.shop.locations[0]
        self.shop.locations = [dict(base, id='gid://shopify/Location/%d' % i, name='Loc %d' % i) for i in range(3)]
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        self.store.warehouse_id = warehouse
        self.assertEqual(self.store._sync_locations(), {'created': 3, 'updated': 0, 'unchanged': 0, 'removed': 0})
        bindings = self.env['eh.shopify.location'].search([('store_id', '=', self.store.id)])
        self.assertEqual(bindings.mapped('warehouse_id'), warehouse)
        self.assertEqual(bindings[0].location_ids, warehouse.lot_stock_id)
        self.assertEqual(self.store._sync_locations(), {'created': 0, 'updated': 0, 'unchanged': 3, 'removed': 0})
        self.shop.locations[0]['name'] = 'Renamed'
        self.shop.locations.pop()
        self.assertEqual(self.store._sync_locations(), {'created': 0, 'updated': 1, 'unchanged': 1, 'removed': 1})
        self.assertTrue(bindings.filtered(lambda b: b.shopify_gid == 'gid://shopify/Location/2').removed_in_shopify)

    def test_fulfillment_service_location_defaults(self):
        self.shop.locations[0].update({'isFulfillmentService': True,
                                       'fulfillmentService': {'handle': '3pl', 'serviceName': 'Acme 3PL'}})
        self.store._sync_locations()
        binding = self.env['eh.shopify.location'].search([('store_id', '=', self.store.id)])
        self.assertEqual(binding.delivery_mode, 'shopify')
        self.assertFalse(binding.sync_stock)

    def test_register_webhooks_and_self_heal(self):
        topics = self.store._webhook_topics()
        result = self.store._register_webhooks()
        self.assertEqual(result['created'], len(topics))
        url = self.store.webhook_url
        self.assertTrue(url.startswith('https://erp.example.com/eh_shopify/webhook/'))
        self.assertEqual({w['uri'] for w in self.shop.webhooks}, {url})
        self.assertEqual(self.store._register_webhooks()['created'], 0)
        self.shop.webhooks.append({'id': 'gid://shopify/WebhookSubscription/9', 'topic': 'PRODUCTS_CREATE',
                                   'uri': url, 'format': 'JSON', 'filter': None, 'includeFields': [],
                                   'apiVersion': {'handle': '2026-07'}, 'createdAt': None, 'updatedAt': None})
        self.shop.webhooks = [w for w in self.shop.webhooks if w['topic'] != 'SHOP_UPDATE']
        result = self.store._register_webhooks()
        self.assertEqual((result['created'], result['removed']), (1, 1))
        self.assertEqual(sorted(w['topic'] for w in self.shop.webhooks), sorted(self.store._webhook_topics()))
        records = self.env['eh.shopify.webhook'].search([('store_id', '=', self.store.id), ('state', '=', 'active')])
        self.assertEqual(len(records), len(topics))

    def test_webhooks_need_https_and_fall_back_to_polling(self):
        self.env['ir.config_parameter'].sudo().set_str('web.base.url', 'http://localhost:8069')
        result = self.store._register_webhooks()
        self.assertIn('HTTPS', result['skipped'])
        self.assertFalse(self.shop.webhooks)
        self.assertEqual(self.store.health, 'warning')

    def test_legacy_token_needs_signing_secret(self):
        self.store.write({'auth_mode': 'legacy_token'})
        self.store._credential()._set_secrets(access_token='shpat_legacy')
        result = self.store._register_webhooks()
        self.assertIn('API secret key', result['skipped'])
        self.store._credential()._set_secrets(webhook_secret='legacy-signing')
        self.assertEqual(self.store._register_webhooks()['created'], len(self.store._webhook_topics()))
        self.assertEqual(self.fake.requests[-1]['headers']['X-Shopify-Access-Token'], 'shpat_legacy')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestAuthorizationCode(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.oauth_domain = 'eh-oauth.myshopify.com'
        self.oauth_fake = testing.FakeTransport(testing.FakeShop(domain=self.oauth_domain))
        testing.TRANSPORT_OVERRIDES[self.oauth_domain] = self.oauth_fake
        self.addCleanup(testing.TRANSPORT_OVERRIDES.pop, self.oauth_domain, None)
        self.oauth_store = self.env['eh.shopify.store'].create({
            'shop_domain': self.oauth_domain, 'auth_mode': 'authorization_code', 'client_id': 'eh-client-id',
            'webhook_base_url': 'https://odoo.example.com'})
        self.oauth_store._credential()._set_secrets(client_secret='eh-client-secret')

    def _callback(self, state, code='abc'):
        params = {'code': code, 'shop': self.oauth_domain, 'state': state, 'timestamp': str(int(time.time()))}
        params['hmac'] = hmac_util.oauth_query_signature('eh-client-secret', params)
        return params

    def test_the_install_link_comes_back_to_the_address_the_store_was_given(self):
        url = self.oauth_store._oauth_authorize_url()
        redirect = parse_qs(urlparse(url).query)['redirect_uri'][0]
        self.assertEqual(redirect, 'https://odoo.example.com/eh_shopify/oauth/callback',
                         'the public address of the store wins over the address Odoo guesses')
        with self.assertRaises(ValidationError):
            self.oauth_store.webhook_base_url = 'http://odoo.example.com'
        self.oauth_store.webhook_base_url = False
        self.env['ir.config_parameter'].sudo().set_str('web.base.url', 'http://localhost:8069')
        with self.assertRaises(UserError) as caught:
            self.oauth_store._oauth_authorize_url()
        self.assertIn('https', str(caught.exception),
                      'an address Shopify will refuse is refused here, not halfway through the install')

    def test_install_link_flow_with_refresh_rotation(self):
        url = self.oauth_store._oauth_authorize_url()
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query['client_id'], ['eh-client-id'])
        self.assertTrue(query['redirect_uri'][0].endswith('/eh_shopify/oauth/callback'))
        state = query['state'][0]
        self.oauth_fake.shop.codes['abc'] = True
        cred = self.oauth_store._credential()
        cred._consume_oauth(self._callback(state))
        self.assertTrue(cred.refresh_token.startswith(crypto.PREFIX))
        self.assertFalse(cred.oauth_state_digest)
        self.oauth_store._check_connection()
        self.assertEqual(self.oauth_store.state, 'connected')
        first_refresh = cred._secret('refresh_token')
        cred.token_expires_at = fields.Datetime.now() - datetime.timedelta(minutes=1)
        token = cred._access_token()
        self.assertEqual(token, 'ac_2')
        self.assertNotEqual(cred._secret('refresh_token'), first_refresh)

    def test_replayed_or_forged_callback_rejected(self):
        state = parse_qs(urlparse(self.oauth_store._oauth_authorize_url()).query)['state'][0]
        cred = self.oauth_store._credential()
        forged = self._callback(state)
        forged['code'] = 'swapped'
        code = None
        try:
            cred._consume_oauth(forged)
        except errors.AuthError as error:
            code = error.code
        self.assertEqual(code, 'bad_hmac')
        self.assertTrue(cred.oauth_state_digest, 'a forged callback must not spend the real state')
        self.oauth_fake.shop.codes['abc'] = True
        cred._consume_oauth(self._callback(state))
        self.assertFalse(cred.oauth_state_digest)
        with self.assertRaises(errors.AuthError) as ctx:
            cred._consume_oauth(self._callback(state))
        self.assertEqual(ctx.exception.code, 'state_expired')

    def test_expired_link_rejected(self):
        state = parse_qs(urlparse(self.oauth_store._oauth_authorize_url()).query)['state'][0]
        cred = self.oauth_store._credential()
        cred.oauth_state_expires_at = fields.Datetime.now() - datetime.timedelta(minutes=1)
        with self.assertRaises(errors.AuthError) as ctx:
            cred._consume_oauth(self._callback(state))
        self.assertEqual(ctx.exception.code, 'state_expired')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestSecurity(ShopifyCase):

    def test_credentials_unreadable_even_for_managers(self):
        self.store._check_connection()
        manager = self._user('shopify_manager', ['eh_shopify_connector.group_shopify_manager'])
        with self.assertRaises(AccessError):
            self.env['eh.shopify.credential'].with_user(manager).search([])
        values = self.store.with_user(manager).read(['client_secret_input', 'has_client_secret'])[0]
        self.assertFalse(values['client_secret_input'])
        self.assertTrue(values['has_client_secret'])
        self.store.with_user(manager).write({'client_secret_input': 'rotated-secret'})
        self.assertEqual(self.store._credential()._secret('client_secret'), 'rotated-secret')

    def test_plain_user_cannot_see_secret_inputs(self):
        user = self._user('shopify_user', ['eh_shopify_connector.group_shopify_user'])
        self.assertNotIn('client_secret_input', self.env['eh.shopify.store'].with_user(user).fields_get())
        with self.assertRaises(AccessError):
            self.store.with_user(user).write({'name': 'renamed'})

    def test_company_isolation(self):
        other = self.env['res.company'].create({'name': 'Other company'})
        user = self._user('other_company_user', ['eh_shopify_connector.group_shopify_user'], company=other)
        self.assertFalse(self.env['eh.shopify.store'].with_user(user).search([('id', '=', self.store.id)]))

    def test_app_uninstalled_is_confirmed_before_wiping(self):
        self.store._check_connection()
        still_installed, _created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-0', 'X-Shopify-Topic': 'app/uninstalled'},
            {'id': 1, 'myshopify_domain': self.domain})
        still_installed.job_id._run_inline()
        self.assertEqual(still_installed.state, 'ignored')
        self.assertEqual(self.store.state, 'connected')
        forged, _created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-2', 'X-Shopify-Topic': 'app/uninstalled'},
            {'id': 999, 'myshopify_domain': 'another-store.myshopify.com'})
        forged.job_id._run_inline()
        self.assertEqual(forged.state, 'ignored')
        self.assertEqual(self.store.state, 'connected')

    def test_uninstall_older_than_reconnection_is_ignored(self):
        self.store._check_connection()
        event, _created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-late', 'X-Shopify-Topic': 'app/uninstalled',
            'X-Shopify-Triggered-At': '2020-01-01T00:00:00Z'}, {'id': 1, 'myshopify_domain': self.domain})
        self.fake.script((401, {}, {}), (401, {}, {}))
        event.job_id._run_inline()
        self.assertEqual(event.state, 'ignored')
        self.assertEqual(self.store.state, 'connected')

    def test_app_uninstalled_wipes_credentials(self):
        self.store._check_connection()
        event, created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-1', 'X-Shopify-Topic': 'app/uninstalled'},
            {'id': 1, 'myshopify_domain': self.domain})
        self.assertTrue(created)
        self.fake.script((401, {}, {}), (401, {}, {}))
        event.job_id._run_inline()
        self.assertEqual(event.job_id.state, 'done')
        self.assertEqual(self.store.state, 'disconnected')
        self.assertFalse(self.store._credential().access_token)
        self.assertFalse(self.store._credential().client_secret)


@tagged('post_install', '-at_install', 'eh_shopify')
class TestSetupWizard(ShopifyCase):

    def _connected_wizard(self, domain='eh-cutover'):
        wizard = self.env['eh.shopify.setup.wizard'].create({
            'shop_domain': 'https://admin.shopify.com/store/%s' % domain, 'auth_mode': 'client_credentials',
            'client_id': 'eh-client-id', 'client_secret': 'eh-client-secret'})
        fake = testing.FakeTransport(testing.FakeShop(domain='%s.myshopify.com' % domain))
        testing.TRANSPORT_OVERRIDES['%s.myshopify.com' % domain] = fake
        self.addCleanup(testing.TRANSPORT_OVERRIDES.pop, '%s.myshopify.com' % domain, None)
        wizard.action_connect()
        return wizard

    def test_finishing_asks_before_orders_reach_the_books(self):
        wizard = self._connected_wizard()
        self.assertEqual(wizard.step, 'review')
        self.assertTrue(wizard.order_import_from, 'the cutover moment is proposed, not left empty')
        self.assertIn('read_returns', wizard.optional_scope_list,
                      'the permissions shown come from the table the connector checks')
        self.assertIn('read_orders', wizard.required_scope_list)
        wizard.write({'import_orders': False})
        wizard.action_finish()
        store = wizard.store_id
        self.assertFalse(store.orders_enabled, 'nothing is posted until the merchant says so')

    def test_finishing_with_orders_on_sets_the_cutover(self):
        wizard = self._connected_wizard(domain='eh-cutover-on')
        wizard.write({'import_orders': True, 'invoice_policy': 'manual', 'order_confirm': False})
        wizard.action_finish()
        store = wizard.store_id
        self.assertTrue(store.orders_enabled)
        self.assertTrue(store.order_import_from, 'older orders stay in Shopify instead of arriving by surprise')
        self.assertEqual(store.invoice_policy, 'manual')
        self.assertFalse(store.order_confirm)

    def test_typed_secrets_never_stored_in_clear(self):
        wizard = self.env['eh.shopify.setup.wizard'].create({
            'shop_domain': 'https://admin.shopify.com/store/eh-wizard', 'auth_mode': 'client_credentials',
            'client_id': 'eh-client-id', 'client_secret': 'eh-client-secret'})
        self.env.flush_all()
        self.env.cr.execute("SELECT client_secret_sealed FROM eh_shopify_setup_wizard WHERE id = %s", (wizard.id,))
        sealed = self.env.cr.fetchone()[0]
        self.assertTrue(sealed.startswith(crypto.PREFIX))
        self.assertNotIn('eh-client-secret', sealed)
        self.assertEqual(wizard._unsealed('client_secret'), 'eh-client-secret')
        fake = testing.FakeTransport(testing.FakeShop(domain='eh-wizard.myshopify.com'))
        testing.TRANSPORT_OVERRIDES['eh-wizard.myshopify.com'] = fake
        self.addCleanup(testing.TRANSPORT_OVERRIDES.pop, 'eh-wizard.myshopify.com', None)
        wizard.action_connect()
        self.assertEqual(wizard.step, 'review')
        self.assertFalse(wizard.client_secret_sealed)
        self.assertEqual(wizard.store_id._credential()._secret('client_secret'), 'eh-client-secret')
        self.assertEqual(wizard.store_id.state, 'connected')
