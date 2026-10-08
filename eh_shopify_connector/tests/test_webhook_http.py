# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..controllers import main as controller
from ..lib import hmac_util, testing


@tagged('post_install', '-at_install', 'eh_shopify')
class TestWebhookEndpoint(HttpCase):

    def setUp(self):
        super().setUp()
        self.domain = 'eh-hook.myshopify.com'
        testing.TRANSPORT_OVERRIDES[self.domain] = testing.FakeTransport(testing.FakeShop(domain=self.domain))
        self.addCleanup(testing.TRANSPORT_OVERRIDES.pop, self.domain, None)
        self.store = self.env['eh.shopify.store'].create({
            'shop_domain': self.domain, 'auth_mode': 'client_credentials', 'client_id': 'id', 'state': 'connected'})
        self.store._credential()._set_secrets(client_secret='hook-secret')
        self.url = '/eh_shopify/webhook/%s' % self.store.store_uuid
        self.body = json.dumps({'id': 1, 'admin_graphql_api_id': 'gid://shopify/Location/1'}).encode()

    def _post(self, body=None, secret='hook-secret', url=None, **headers):
        body = self.body if body is None else body
        values = {
            'Content-Type': 'application/json',
            'X-Shopify-Hmac-Sha256': hmac_util.webhook_signature(secret, body),
            'X-Shopify-Shop-Domain': self.domain,
            'X-Shopify-Topic': 'locations/update',
            'X-Shopify-Webhook-Id': 'delivery-1',
            'X-Shopify-Event-Id': 'event-1',
            'X-Shopify-API-Version': '2026-07',
            'X-Shopify-Triggered-At': '2026-09-15T01:02:03.123456789Z',
        }
        values.update(headers)
        return self.url_open(url or self.url, data=body, headers=values)

    def _events(self):
        return self.env['eh.shopify.event'].search([('store_id', '=', self.store.id)])

    def test_signed_delivery_is_stored_and_queued(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        event = self._events()
        self.assertEqual(len(event), 1)
        self.assertEqual(event.topic, 'locations/update')
        self.assertEqual(event.resource_gid, 'gid://shopify/Location/1')
        self.assertEqual(str(event.triggered_at), '2026-09-15 01:02:03')
        self.assertEqual(event.job_id.job_type, 'event.process')
        self.assertEqual(event.job_id.state, 'pending')

    def test_duplicate_delivery_is_acknowledged_once(self):
        self.assertEqual(self._post().status_code, 200)
        second = self._post()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.text, 'duplicate')
        self.assertEqual(len(self._events()), 1)

    @mute_logger('odoo.addons.eh_shopify_connector.controllers.main')
    def test_bad_signature_rejected(self):
        self.assertEqual(self._post(secret='wrong').status_code, 401)
        self.assertFalse(self._events())

    @mute_logger('odoo.addons.eh_shopify_connector.controllers.main')
    def test_tampered_body_rejected(self):
        signature = hmac_util.webhook_signature('hook-secret', self.body)
        response = self._post(body=b'{"id": 2}', **{'X-Shopify-Hmac-Sha256': signature})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self._events())

    @mute_logger('odoo.addons.eh_shopify_connector.controllers.main')
    def test_other_shop_rejected(self):
        self.assertEqual(self._post(**{'X-Shopify-Shop-Domain': 'attacker.myshopify.com'}).status_code, 401)
        self.assertFalse(self._events())

    def test_oversized_and_non_json_bodies_are_refused(self):
        with patch.object(controller, 'MAX_BODY_BYTES', 16):
            self.assertEqual(self._post().status_code, 413)
        self.assertEqual(self._post(**{'Content-Type': 'text/plain'}).status_code, 415)
        self.assertFalse(self._events())

    def test_unknown_store_is_not_found(self):
        self.assertEqual(self._post(url='/eh_shopify/webhook/%s' % ('f' * 32)).status_code, 404)
        self.assertEqual(self._post(url='/eh_shopify/webhook/not-a-uuid').status_code, 404)

    @mute_logger('odoo.addons.eh_shopify_connector.controllers.main')
    def test_unreadable_credentials_are_refused_not_a_server_error(self):
        self.store._credential().sudo().write({'client_secret': 'this-was-not-encrypted-by-this-database'})
        response = self._post()
        self.assertEqual(response.status_code, 401, 'a delivery Odoo cannot check is refused, not crashed on')
        self.assertFalse(self._events(), 'nothing is stored from a delivery that was never checked')
        self.assertIn('key', (self.store.webhook_note or '').lower(), 'the store says what to do about it')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestInstallLinkEndpoint(HttpCase):
    """The install link comes back to /eh_shopify/oauth/callback in the browser that started it."""

    def setUp(self):
        super().setUp()
        self.domain = 'eh-install.myshopify.com'
        self.shop = testing.FakeShop(domain=self.domain)
        testing.TRANSPORT_OVERRIDES[self.domain] = testing.FakeTransport(self.shop)
        self.addCleanup(testing.TRANSPORT_OVERRIDES.pop, self.domain, None)
        self.store = self.env['eh.shopify.store'].create({
            'shop_domain': self.domain, 'auth_mode': 'authorization_code', 'client_id': 'eh-client-id',
            'webhook_base_url': 'https://odoo.example.com'})
        self.store._credential()._set_secrets(client_secret='eh-client-secret')

    def _start(self):
        action = self.make_jsonrpc_request('/web/dataset/call_button/eh.shopify.store/action_start_oauth', {
            'model': 'eh.shopify.store', 'method': 'action_start_oauth', 'args': [[self.store.id]], 'kwargs': {}})
        return parse_qs(urlparse(action['url']).query)['state'][0]

    def _callback(self, state, code='abc'):
        params = {'code': code, 'shop': self.domain, 'state': state, 'timestamp': str(int(time.time()))}
        params['hmac'] = hmac_util.oauth_query_signature('eh-client-secret', params)
        return self.url_open('/eh_shopify/oauth/callback', params=params, allow_redirects=False)

    def test_install_link_connects_the_store(self):
        self.authenticate('admin', 'admin')
        state = self._start()
        self.shop.codes['abc'] = True
        response = self._callback(state)
        self.assertEqual(response.status_code, 303, response.text)
        self.assertIn('eh.shopify.store', response.headers['Location'])
        self.env.invalidate_all()
        self.assertTrue(self.store._credential()._has('refresh_token'))
        self.assertFalse(self.store._credential().oauth_state_digest, 'the link is spent')
        self.assertEqual(self.store.state, 'connected')

    def test_a_link_started_in_another_browser_is_refused(self):
        self.authenticate('admin', 'admin')
        state = self._start()
        self.authenticate('admin', 'admin')
        self.shop.codes['abc'] = True
        response = self._callback(state)
        self.assertEqual(response.status_code, 400)
        self.env.invalidate_all()
        self.assertFalse(self.store._credential()._has('refresh_token'))
        self.assertTrue(self.store._credential().oauth_state_digest, 'a refused callback does not spend the link')

    def test_only_a_shopify_manager_finishes_the_connection(self):
        new_test_user(self.env, 'eh_install_clerk', groups='base.group_user,eh_shopify_connector.group_shopify_user')
        self.authenticate('eh_install_clerk', 'eh_install_clerk')
        response = self._callback('any-state')
        self.assertEqual(response.status_code, 403)
        self.assertIn('Shopify manager', response.text)
