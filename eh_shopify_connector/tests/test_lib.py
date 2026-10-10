# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime
import re

from odoo.tests import BaseCase, tagged

from ..lib import client, crypto, documents, errors, grants, hmac_util, testing, throttle, util, versions


def _client(transport, **kw):
    return client.GraphQLClient('eh-test.myshopify.com', lambda force_refresh=False: 'tok', transport=transport,
                                sleep=transport.fake_sleep, rng=lambda: 0.5, bucket=throttle.CostBucket(), **kw)


@tagged('post_install', '-at_install', 'eh_shopify')
class TestVersions(BaseCase):

    def test_floor_and_features(self):
        today = datetime.date(2026, 9, 15)
        self.assertEqual(versions.status('2025-10', today)[0], 'below_floor')
        self.assertEqual(versions.status('2026-07', today)[0], 'ok')
        self.assertEqual(versions.status('2026-10', today)[0], 'unreleased')
        self.assertEqual(versions.status('2026-01', datetime.date(2026, 11, 1))[0], 'warning')
        self.assertEqual(versions.status('2026-01', datetime.date(2027, 2, 1))[0], 'expired')
        self.assertEqual(versions.selectable(today), ['2026-01', '2026-04', '2026-07'])
        self.assertTrue(versions.supports('2026-01', 'change_from_quantity'))
        self.assertFalse(versions.supports('2026-04', 'order_attribution'))
        self.assertTrue(versions.supports('2026-10', 'variant_barcodes_list'))


@tagged('post_install', '-at_install', 'eh_shopify')
class TestReadableValues(BaseCase):

    def test_shopify_values_read_as_a_person_says_them(self):
        self.assertEqual(util.readable_enum('PARTIALLY_REFUNDED'), 'Partially refunded')
        self.assertEqual(util.readable_enum('DEPOSIT'), 'Deposit')
        self.assertEqual(util.readable_enum('NO_RETURN'), 'No return')
        self.assertFalse(util.readable_enum(False), 'nothing stays nothing')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestSignatures(BaseCase):

    def test_webhook_hmac(self):
        body = b'{"id": 1}'
        good = hmac_util.webhook_signature('secret', body)
        self.assertTrue(hmac_util.verify_webhook('secret', body, good))
        self.assertFalse(hmac_util.verify_webhook('secret', b'{"id": 2}', good))
        self.assertFalse(hmac_util.verify_webhook('other', body, good))
        self.assertFalse(hmac_util.verify_webhook('', body, good))
        self.assertFalse(hmac_util.verify_webhook('secret', body, None))

    def test_oauth_query_hmac(self):
        params = {'code': 'c', 'shop': 'a.myshopify.com', 'state': 's', 'timestamp': '1'}
        params['hmac'] = hmac_util.oauth_query_signature('secret', params)
        self.assertTrue(hmac_util.verify_oauth_query('secret', params))
        self.assertFalse(hmac_util.verify_oauth_query('secret', dict(params, shop='b.myshopify.com')))

    def test_domain_normalisation(self):
        cases = {
            'mystore': 'mystore.myshopify.com',
            'MyStore.myshopify.com': 'mystore.myshopify.com',
            'https://mystore.myshopify.com/admin': 'mystore.myshopify.com',
            'https://admin.shopify.com/store/my-store/orders': 'my-store.myshopify.com',
            'admin.shopify.com/store/my-store': 'my-store.myshopify.com',
            'www.brand.com': None,
            'https://admin.shopify.com/settings': None,
            '': None,
        }
        for value, expected in cases.items():
            self.assertEqual(hmac_util.normalize_shop_domain(value), expected, value)


@tagged('post_install', '-at_install', 'eh_shopify')
class TestVault(BaseCase):

    def test_round_trip_and_wrong_key(self):
        vault = crypto.Vault('material')
        stored = vault.encrypt('shpat_secret')
        self.assertTrue(stored.startswith(crypto.PREFIX))
        self.assertNotIn('shpat_secret', stored)
        self.assertEqual(vault.decrypt(stored), 'shpat_secret')
        with self.assertRaises(crypto.VaultError):
            crypto.Vault('another').decrypt(stored)
        self.assertEqual(crypto.mask('abcdefgh'), '********efgh')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestClient(BaseCase):

    def setUp(self):
        super().setUp()
        self.fake = testing.FakeTransport()
        self.client = _client(self.fake)

    def test_query_returns_data_and_records_version(self):
        data = self.client.execute(documents.SHOP_INFO.text).data
        self.assertEqual(data['shop']['currencyCode'], 'USD')
        self.assertEqual(self.client.served_version, '2026-07')
        headers = self.fake.requests[-1]['headers']
        self.assertEqual(headers['X-Shopify-Access-Token'], 'tok')
        self.assertEqual(self.fake.requests[-1]['timeout'], (10, 60))

    def test_http_429_waits_retry_after(self):
        self.fake.script((429, {'Retry-After': '2'}, {}))
        self.client.execute(documents.SHOP_INFO.text)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertTrue(any(w >= 2 for w in self.fake.waits))

    def test_throttled_graphql_error_waits_then_succeeds(self):
        self.fake.script('throttled')
        self.client.execute(documents.SHOP_INFO.text)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertTrue(self.fake.waits)

    def test_server_errors_retry_then_give_up(self):
        self.fake.script(*[(503, {}, {})] * 6)
        with self.assertRaises(errors.TransientError):
            self.client.execute(documents.SHOP_INFO.text)
        self.assertEqual(len(self.fake.requests), 6)

    def test_connect_error_is_retried(self):
        self.fake.script(client.TransportConnectError('dns'))
        self.client.execute(documents.SHOP_INFO.text)
        self.assertEqual(len(self.fake.requests), 2)

    def test_read_timeout_mutation_is_not_replayed(self):
        self.fake.script(client.TransportReadTimeout('late'))
        with self.assertRaises(errors.TransientError) as ctx:
            self.client.execute(documents.WEBHOOK_DELETE.text, {'id': 'gid://shopify/WebhookSubscription/1'})
        self.assertTrue(ctx.exception.ambiguous)
        self.assertEqual(len(self.fake.requests), 1)

    def test_read_timeout_mutation_with_idempotency_key_is_replayed(self):
        self.fake.script(client.TransportReadTimeout('late'))
        self.client.execute(documents.WEBHOOK_DELETE.text, {'id': 'gid://shopify/WebhookSubscription/1'},
                            idempotency_key='key-1')
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.fake.requests[-1]['payload']['variables']['idempotencyKey'], 'key-1')

    def test_read_timeout_query_is_replayed(self):
        self.fake.script(client.TransportReadTimeout('late'))
        self.client.execute(documents.SHOP_INFO.text)
        self.assertEqual(len(self.fake.requests), 2)

    def test_401_refreshes_token_once(self):
        calls = []

        def provider(force_refresh=False):
            calls.append(force_refresh)
            return 'fresh' if force_refresh else 'stale'

        c = client.GraphQLClient('eh-test.myshopify.com', provider, transport=self.fake,
                                 sleep=self.fake.fake_sleep, bucket=throttle.CostBucket())
        self.fake.script((401, {}, {'errors': 'Invalid API key or access token'}))
        c.execute(documents.SHOP_INFO.text)
        self.assertIn(True, calls)
        self.assertEqual(self.fake.requests[-1]['headers']['X-Shopify-Access-Token'], 'fresh')
        self.fake.script((401, {}, {}), (401, {}, {}))
        with self.assertRaises(errors.AuthError):
            c.execute(documents.SHOP_INFO.text)

    def test_job_deadline_stops_before_sending_and_before_long_waits(self):
        now = [100.0]
        c = client.GraphQLClient('eh-test.myshopify.com', lambda force_refresh=False: 'tok', transport=self.fake,
                                 sleep=self.fake.fake_sleep, clock=lambda: now[0], bucket=throttle.CostBucket(),
                                 deadline=100.5)
        with self.assertRaises(errors.TransientError) as ctx:
            c.execute(documents.SHOP_INFO.text)
        self.assertEqual(ctx.exception.code, 'deadline')
        self.assertFalse(self.fake.requests)
        c.deadline = 110.0
        self.fake.script((429, {'Retry-After': '30'}, {}))
        with self.assertRaises(errors.TransientError) as ctx:
            c.execute(documents.SHOP_INFO.text)
        self.assertEqual(ctx.exception.code, 'deadline')
        self.assertTrue(self.fake.requests[-1]['timeout'][1] <= 9.0)

    def test_access_denied_lists_missing_scope(self):
        self.fake.script((200, {}, {'errors': [{'message': 'Access denied for orders field.', 'extensions': {
            'code': 'ACCESS_DENIED', 'requiredAccess': '`read_orders` access scope.'}}]}))
        with self.assertRaises(errors.ScopeError) as ctx:
            self.client.execute(documents.SHOP_INFO.text)
        self.assertEqual(ctx.exception.missing_scopes, ['read_orders'])

    def test_schema_error_is_query_error(self):
        self.fake.script((200, {}, [{'message': "Field 'x' doesn't exist on type 'Shop'",
                                     'extensions': {'code': 'undefinedField'}}]))
        with self.assertRaises(errors.QueryError):
            self.client.execute(documents.SHOP_INFO.text)

    def test_frozen_shop(self):
        self.fake.script((402, {}, {}))
        with self.assertRaises(errors.ShopUnavailable):
            self.client.execute(documents.SHOP_INFO.text)

    def test_user_errors_raised(self):
        self.shop_sub = {'uri': 'https://erp.example.com/eh_shopify/webhook/x', 'format': 'JSON'}
        self.client.mutate(documents.WEBHOOK_CREATE.text, {'topic': 'SHOP_UPDATE', 'subscription': self.shop_sub},
                           'webhookSubscriptionCreate')
        with self.assertRaises(errors.UserErrors) as ctx:
            self.client.mutate(documents.WEBHOOK_CREATE.text, {'topic': 'SHOP_UPDATE', 'subscription': self.shop_sub},
                               'webhookSubscriptionCreate')
        self.assertIn('already been taken', ctx.exception.message)

    def test_typed_user_errors_are_not_missed(self):
        self.fake.script((200, {}, {'data': {'orderCancel': {'job': None, 'orderCancelUserErrors': [
            {'field': ['orderId'], 'message': 'Order has already been cancelled', 'code': 'ORDER_ALREADY_CANCELLED'}]}}}))
        with self.assertRaises(errors.UserErrors) as ctx:
            self.client.mutate(documents.ORDER_CANCEL.text, {'orderId': 'gid://shopify/Order/1', 'reason': 'CUSTOMER',
                                                             'restock': True}, 'orderCancel')
        self.assertEqual(ctx.exception.codes(), {'ORDER_ALREADY_CANCELLED'})

    def test_paginate_all_pages_and_halve_on_cost(self):
        base = self.fake.shop.locations[0]
        self.fake.shop.locations = [dict(base, id='gid://shopify/Location/%d' % i) for i in range(7)]
        nodes = list(self.client.paginate(documents.LOCATIONS.text, {}, ('locations',), page_size=3))
        self.assertEqual(len(nodes), 7)
        self.fake.script((200, {}, {'errors': [{'message': 'cost', 'extensions': {'code': 'MAX_COST_EXCEEDED'}}]}))
        nodes = list(self.client.paginate(documents.LOCATIONS.text, {}, ('locations',), page_size=4))
        self.assertEqual(len(nodes), 7)
        self.assertEqual(self.fake.requests[-1]['payload']['variables']['first'], 2)

    def test_paginate_refuses_missing_cursor(self):
        self.fake.script((200, {}, {'data': {'locations': {'pageInfo': {'hasNextPage': True, 'endCursor': None},
                                                           'nodes': []}}}))
        with self.assertRaises(errors.IncompleteData):
            list(self.client.paginate(documents.LOCATIONS.text, {}, ('locations',)))

    def test_complete_nested_connection(self):
        parent = {'lineItems': {'nodes': [{'id': 1}], 'pageInfo': {'hasNextPage': True, 'endCursor': 'c1'}}}
        self.fake.script((200, {}, {'data': {'node': {'lineItems': {
            'nodes': [{'id': 2}], 'pageInfo': {'hasNextPage': False, 'endCursor': 'c2'}}}}}))
        nodes = self.client.complete_connection(parent, 'lineItems', 'query EhNested { node }', 'gid://x/1',
                                                ('node', 'lineItems'))
        self.assertEqual([n['id'] for n in nodes], [1, 2])

    def test_bulk_assembly(self):
        rows = [{'id': 'p1'}, {'id': 'v1', '__parentId': 'p1'}, {'id': 'p2'}, {'id': 'v2', '__parentId': 'p1'},
                {'id': 'm1', '__parentId': 'v1'}, {'id': 'v3', '__parentId': 'p2'}]
        grouped = client.assemble_bulk(rows)
        self.assertEqual([g['node']['id'] for g in grouped], ['p1', 'p2'])
        self.assertEqual([c['node']['id'] for c in grouped[0]['children']], ['v1', 'v2'])
        self.assertEqual(grouped[0]['children'][0]['children'][0]['node']['id'], 'm1')
        with self.assertRaises(errors.IncompleteData):
            client.assemble_bulk([{'id': 'x', '__parentId': 'missing'}])

    def test_server_error_on_unkeyed_mutation_is_not_replayed(self):
        self.fake.script((504, {}, {}))
        with self.assertRaises(errors.TransientError) as ctx:
            self.client.execute(documents.WEBHOOK_DELETE.text, {'id': 'gid://shopify/WebhookSubscription/1'})
        self.assertTrue(ctx.exception.ambiguous)
        self.assertEqual(len(self.fake.requests), 1)
        self.fake.script((200, {}, b'{not json'))
        with self.assertRaises(errors.TransientError) as ctx:
            self.client.execute(documents.WEBHOOK_DELETE.text, {'id': 'gid://shopify/WebhookSubscription/1'})
        self.assertTrue(ctx.exception.ambiguous)

    def test_idempotent_document_requires_a_key(self):
        with self.assertRaises(errors.QueryError):
            self.client.execute(documents.INVENTORY_SET.text, {'input': {}})
        self.assertFalse(self.fake.requests)

    def test_idempotency_concurrent_request_is_retried_with_same_key(self):
        busy = {'data': {'inventorySetQuantities': {'inventoryAdjustmentGroup': None, 'userErrors': [
            {'field': ['input'], 'message': 'busy', 'code': 'IDEMPOTENCY_CONCURRENT_REQUEST'}]}}}
        self.fake.shop.inventory[('I1', 'L1')] = 1
        self.fake.script((200, {}, busy))
        payload = self.client.mutate(documents.INVENTORY_SET.text, {'input': {
            'name': 'available', 'reason': 'correction', 'quantities': [
                {'inventoryItemId': 'I1', 'locationId': 'L1', 'quantity': 3, 'changeFromQuantity': 1}]}},
            'inventorySetQuantities', idempotency_key='k-1')
        self.assertFalse(payload['userErrors'])
        self.assertEqual({r['payload']['variables']['idempotencyKey'] for r in self.fake.requests}, {'k-1'})
        mismatch = {'data': {'inventorySetQuantities': {'inventoryAdjustmentGroup': None, 'userErrors': [
            {'field': ['input'], 'message': 'mismatch', 'code': 'IDEMPOTENCY_KEY_PARAMETER_MISMATCH'}]}}}
        self.fake.script((200, {}, mismatch))
        with self.assertRaises(errors.QueryError):
            self.client.mutate(documents.INVENTORY_SET.text, {'input': {'quantities': []}},
                               'inventorySetQuantities', idempotency_key='k-1')

    def test_nested_pages_halve_on_cost(self):
        parent = {'lineItems': {'nodes': [{'id': 1}], 'pageInfo': {'hasNextPage': True, 'endCursor': 'c1'}}}
        self.fake.script((200, {}, {'errors': [{'message': 'cost', 'extensions': {'code': 'MAX_COST_EXCEEDED'}}]}),
                         (200, {}, {'data': {'node': {'lineItems': {
                             'nodes': [{'id': 2}], 'pageInfo': {'hasNextPage': False, 'endCursor': 'c2'}}}}}))
        nodes = self.client.complete_connection(parent, 'lineItems', 'query EhNested { node }', 'gid://x/1',
                                                ('node', 'lineItems'), page_size=40)
        self.assertEqual([n['id'] for n in nodes], [1, 2])
        self.assertEqual(self.fake.requests[-1]['payload']['variables']['first'], 20)

    def test_protected_customer_data_is_partial_not_fatal(self):
        self.fake.script((200, {}, {'data': {'shop': {'name': 'x', 'email': None}}, 'errors': [
            {'message': 'This app is not approved to access the Customer object.', 'path': ['shop', 'email']}]}))
        response = self.client.execute(documents.SHOP_INFO.text, tolerate=errors.is_protected_data_error)
        self.assertEqual(response.data['shop']['name'], 'x')
        self.assertEqual(len(response.warnings), 1)
        self.fake.script((200, {}, {'data': {'shop': None}, 'errors': [
            {'message': 'This app is not approved to access the Customer object.'}]}))
        with self.assertRaises(errors.QueryError):
            self.client.execute(documents.SHOP_INFO.text)

    def test_cost_bucket_projection(self):
        now = [100.0]
        bucket = throttle.CostBucket(clock=lambda: now[0])
        bucket.update({'requestedQueryCost': 100, 'throttleStatus': {
            'maximumAvailable': 2000.0, 'currentlyAvailable': 50.0, 'restoreRate': 100.0}})
        self.assertAlmostEqual(bucket.wait_for(250), 2.0)
        now[0] += 1.0
        self.assertAlmostEqual(bucket.wait_for(250), 1.0)
        now[0] += 5.0
        self.assertEqual(bucket.wait_for(250), 0.0)


@tagged('post_install', '-at_install', 'eh_shopify')
class TestGrants(BaseCase):

    def setUp(self):
        super().setUp()
        self.fake = testing.FakeTransport()
        self.shop = self.fake.shop

    def test_client_credentials(self):
        token = grants.client_credentials(self.shop.domain, 'eh-client-id', 'eh-client-secret',
                                          post_form=self.fake.post_form, clock=lambda: 1000)
        self.assertEqual(token.expires_in, 86399)
        self.assertEqual(token.expires_at, 1000 + 86399)
        self.assertIsNone(token.refresh_token)
        form = self.fake.requests[-1]['form']
        self.assertEqual(form['grant_type'], 'client_credentials')
        self.assertTrue(self.fake.requests[-1]['url'].endswith('/admin/oauth/access_token'))

    def test_client_credentials_other_organisation(self):
        self.shop.same_organisation = False
        with self.assertRaises(errors.AuthError) as ctx:
            grants.client_credentials(self.shop.domain, 'eh-client-id', 'eh-client-secret',
                                      post_form=self.fake.post_form)
        self.assertEqual(ctx.exception.code, 'shop_not_permitted')

    def test_bad_client(self):
        with self.assertRaises(errors.AuthError) as ctx:
            grants.client_credentials(self.shop.domain, 'eh-client-id', 'wrong', post_form=self.fake.post_form)
        self.assertEqual(ctx.exception.code, 'invalid_client')

    def test_code_exchange_and_rotation(self):
        self.shop.codes['abc'] = True
        token = grants.exchange_code(self.shop.domain, 'eh-client-id', 'eh-client-secret', 'abc',
                                     post_form=self.fake.post_form)
        self.assertEqual(self.fake.requests[-1]['form']['expiring'], '1')
        self.assertEqual(token.expires_in, 3600)
        renewed = grants.refresh(self.shop.domain, 'eh-client-id', 'eh-client-secret', token.refresh_token,
                                 post_form=self.fake.post_form)
        self.assertNotEqual(renewed.refresh_token, token.refresh_token)
        with self.assertRaises(errors.AuthError) as ctx:
            grants.refresh(self.shop.domain, 'eh-client-id', 'eh-client-secret', token.refresh_token,
                           post_form=self.fake.post_form)
        self.assertEqual(ctx.exception.code, 'invalid_grant')

    def test_authorize_url_and_callback(self):
        url = grants.authorize_url(self.shop.domain, 'eh-client-id', ['read_orders', 'read_products'],
                                   'https://erp.example.com/eh_shopify/oauth/callback', 'st8')
        self.assertIn('scope=read_orders%2Cread_products', url)
        self.assertIn('state=st8', url)
        params = {'code': 'c', 'shop': self.shop.domain, 'state': 'st8', 'timestamp': '100'}
        params['hmac'] = hmac_util.oauth_query_signature('eh-client-secret', params)
        digest = grants.state_digest('st8')
        self.assertEqual(grants.verify_callback('eh-client-secret', params, self.shop.domain, digest,
                                                clock=lambda: 100), 'c')
        for broken, code in ((dict(params, state='other'), 'bad_state'),
                             (dict(params, shop='x.myshopify.com'), 'shop_mismatch'),
                             (dict(params, code='tampered'), 'bad_hmac')):
            if code == 'bad_state':
                broken['hmac'] = hmac_util.oauth_query_signature('eh-client-secret', broken)
            with self.assertRaises(errors.AuthError) as ctx:
                grants.verify_callback('eh-client-secret', broken, self.shop.domain, digest, clock=lambda: 100)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(errors.AuthError) as ctx:
            grants.verify_callback('eh-client-secret', params, self.shop.domain, digest, clock=lambda: 100000)
        self.assertEqual(ctx.exception.code, 'stale_callback')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestDocuments(BaseCase):

    def test_registry_is_consistent(self):
        for name, doc in documents.REGISTRY.items():
            kind, operation = client.operation_of(doc.text)
            if doc.bulk:
                # Bulk queries travel as a string inside bulkOperationRunQuery and
                # stay anonymous, as in Shopify's own examples.
                self.assertEqual((kind, operation), ('query', None), name)
                self.assertNotIn('first:', doc.text, name)
                continue
            self.assertEqual(operation, name)
            self.assertTrue(name.startswith('Eh'))
            self.assertEqual(kind, doc.kind)
            if doc.kind == 'mutation':
                self.assertTrue(doc.payload_key, name)
                self.assertTrue(re.search(r'\b\w*[uU]serErrors\s*\{', doc.text), name)
            if doc.idempotent:
                self.assertIn('@idempotent(key: $idempotencyKey)', doc.text, name)
            if doc.connection:
                self.assertTrue(re.search(r'pageInfo\s*\{\s*hasNextPage\s+endCursor\s*\}', doc.text), name)
                self.assertLessEqual(doc.page_size, 250, name)
            if 'inventoryActivate' in doc.text or 'inventorySetQuantities' in doc.text or 'refundCreate' in doc.text:
                self.assertTrue(doc.idempotent, '%s must carry @idempotent from 2026-04' % name)

    def test_fingerprint_is_order_independent(self):
        self.assertEqual(util.fingerprint({'a': 1, 'b': [1, 2]}), util.fingerprint({'b': [1, 2], 'a': 1}))
        self.assertEqual(util.gid_parts('gid://shopify/Order/123'), ('Order', '123'))
        self.assertEqual(util.redact('token shpat_abcdef here', ['shpat_abcdef']), 'token [redacted] here')
