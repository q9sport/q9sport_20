# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime
import unittest

try:
    import websocket
except ImportError:
    websocket = None

from odoo import fields
from odoo.tests import HttpCase, tagged

from .. import compat
from ..lib import testing
from .common import ShopifyCase
from .test_customers import order as customer_order
from .test_fulfillment import fulfillment_order
from .test_order_import import make_order
from .test_order_sale import ensure_chart
from .test_refunds import add_refund

FORM_OPENED = """
(async () => {
    const deadline = Date.now() + 30000;
    while (!document.querySelector('.o_form_renderer')) {
        if (Date.now() > deadline) {
            throw new Error('the form view did not open');
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    console.log('test successful');
})().catch((error) => console.error(error.message));
"""

LIST_SHOWS_ROWS = """
(async () => {
    const deadline = Date.now() + 30000;
    while (!document.querySelector('.o_list_renderer .o_data_row')) {
        if (Date.now() > deadline) {
            throw new Error('the list view shows no record');
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    console.log('test successful');
})().catch((error) => console.error(error.message));
"""


@tagged('post_install', '-at_install', 'eh_shopify')
class TestScreens(HttpCase, ShopifyCase):
    """Every screen of the app renders in the web client with real records in it."""

    @classmethod
    def setUpClass(cls):
        if websocket is None:
            # The Odoo browser helper needs it too; checked first so the class is reported as skipped once.
            raise unittest.SkipTest('the browser checks need the websocket-client Python package')
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)],
                                                     limit=1)
        cls.env['product.product'].create([{'name': 'Brass gear %s' % i, 'default_code': 'SKU-%s' % i}
                                           for i in range(2)])
        if 'tour_enabled' in cls.env['res.users']._fields:
            cls.env.ref('base.user_admin').tour_enabled = False

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        # The browser talks to the local test server, so Shopify is given the public address explicitly.
        self.store.webhook_base_url = 'https://erp.example.com'
        self.store._register_webhooks()
        self.store.write({'payouts_enabled': True, 'currency_code': self.env.company.currency_id.name})
        self.shop.payments_account['defaultCurrency'] = self.env.company.currency_id.name
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})
        Job = self.env['eh.shopify.job']
        order = make_order(number=81, lines=2)
        order.update({key: value for key, value in customer_order().items() if key != 'name'})
        order['_all']['fulfillmentOrders'] = [fulfillment_order(order, 81)]
        self.shop.add_order(order)
        Order = self.env['eh.shopify.order']
        job = Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.binding = Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])
        compat.validate_picking(self.binding.sale_order_id.picking_ids)
        Job.search([('state', '=', 'pending')])._run_inline()
        add_refund(order, 811, [(1, 1)])
        Order._enqueue_import(self.store, order['id'])._run_inline()
        Job.search([('state', '=', 'pending')])._run_inline()
        returned = 'gid://shopify/Return/812'
        self.shop.returns[returned] = {
            'id': returned, 'name': '#R812', 'status': 'REQUESTED', 'orderId': order['id'], 'exchanges': [],
            'lines': [{'id': '%s-line-0' % returned, 'quantity': 1, 'lineItemId': order['_all']['lineItems'][0]['id'],
                       'dispositions': []}]}
        self.env['eh.shopify.return']._enqueue_return_sync(self.store, returned, order['id'])._run_inline()
        issued = (fields.Datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.shop.add_payout(813, 'PAID', issued, [('CHARGE', 20.0, 0.59, '811', self.binding.shopify_gid)],
                             currency=self.env.company.currency_id.name)
        Job._enqueue(self.store, 'payouts.poll', dedup_key='payouts.poll')._run_inline()
        Job.search([('state', '=', 'pending')])._run_inline()
        due = fields.Date.context_today(self.binding) + datetime.timedelta(days=20)
        dispute = self.shop.add_dispute(814, self.binding.shopify_gid, 'NEEDS_RESPONSE', due.strftime('%Y-%m-%d'),
                                        issued)
        self.env['eh.shopify.dispute']._enqueue_sync(self.store, dispute['id'])._run_inline()
        self.env['eh.shopify.event']._receive(self.store, {'X-Shopify-Webhook-Id': 'w-81',
                                                           'X-Shopify-Topic': 'orders/updated'},
                                              {'id': 81, 'admin_graphql_api_id': order['id']})
        variant = self.env['eh.shopify.variant'].search([('store_id', '=', self.store.id)], limit=1)
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        location = self.env['eh.shopify.location'].create({
            'store_id': self.store.id, 'name': 'Main', 'shopify_gid': 'gid://shopify/Location/1',
            'active_in_shopify': True, 'location_ids': [(6, 0, warehouse.lot_stock_id.ids)]})
        self.env['eh.shopify.inventory.level'].create({
            'store_id': self.store.id, 'variant_binding_id': variant.id, 'location_binding_id': location.id,
            'state': 'synced'})
        for model in ('eh.shopify.customer', 'eh.shopify.refund', 'eh.shopify.return', 'eh.shopify.payout',
                      'eh.shopify.dispute', 'eh.shopify.fulfillment', 'eh.shopify.event', 'eh.shopify.webhook',
                      'eh.shopify.inventory.level'):
            self.assertTrue(self.env[model].search_count([]), 'the screens show a %s' % model)

    def _open_form(self, action, record):
        self.assertTrue(record, 'a %s record to open' % action)
        self.browser_js('/odoo/action-eh_shopify_connector.%s/%s' % (action, record[:1].id), FORM_OPENED,
                        ready='odoo.isReady === true', login='admin')

    def test_record_forms_open(self):
        self._open_form('action_eh_shopify_store', self.store)
        self._open_form('action_eh_shopify_order', self.binding)
        for action, model in (('action_eh_shopify_refund', 'eh.shopify.refund'),
                              ('action_eh_shopify_return', 'eh.shopify.return'),
                              ('action_eh_shopify_payout', 'eh.shopify.payout'),
                              ('action_eh_shopify_dispute', 'eh.shopify.dispute'),
                              ('action_eh_shopify_customer', 'eh.shopify.customer'),
                              ('action_eh_shopify_job', 'eh.shopify.job'),
                              ('action_eh_shopify_event', 'eh.shopify.event'),
                              ('action_eh_shopify_location', 'eh.shopify.location'),
                              ('action_eh_shopify_webhook', 'eh.shopify.webhook'),
                              ('action_eh_shopify_inventory_level', 'eh.shopify.inventory.level')):
            self._open_form(action, self.env[model].search([], limit=1))

    def test_business_documents_open_with_the_shopify_additions(self):
        sale_order = self.binding.sale_order_id
        self.browser_js('/odoo/sale.order/%s' % sale_order.id, FORM_OPENED, ready='odoo.isReady === true',
                        login='admin')
        self.browser_js('/odoo/stock.picking/%s' % sale_order.picking_ids[:1].id, FORM_OPENED,
                        ready='odoo.isReady === true', login='admin')
        self.browser_js('/odoo/account.move/%s' % sale_order.invoice_ids[:1].id, FORM_OPENED,
                        ready='odoo.isReady === true', login='admin')

    def test_record_lists_show_rows(self):
        """Every list of the app renders its records, not only an empty view."""
        for action in ('action_eh_shopify_order', 'action_eh_shopify_refund', 'action_eh_shopify_return',
                       'action_eh_shopify_payout', 'action_eh_shopify_dispute', 'action_eh_shopify_customer',
                       'action_eh_shopify_job', 'action_eh_shopify_event', 'action_eh_shopify_location',
                       'action_eh_shopify_webhook', 'action_eh_shopify_inventory_level',
                       'action_eh_shopify_variant', 'action_eh_shopify_gateway'):
            with self.subTest(action=action):
                self.browser_js('/odoo/action-eh_shopify_connector.%s?view_type=list' % action, LIST_SHOWS_ROWS,
                                ready='odoo.isReady === true', login='admin')

    def test_click_everywhere_in_the_app(self):
        self.browser_js(
            '/odoo',
            "odoo.loader.modules.get('@web/webclient/clickbot/clickbot_loader').startClickEverywhere("
            "{ xmlId: 'eh_shopify_connector.menu_eh_shopify_root', logger: true });",
            ready='odoo.isReady === true', login='admin', timeout=600, success_signal='clickbot test succeeded')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestSetupWizardScreen(HttpCase, ShopifyCase):
    """The Connect a Store dialog, driven in the browser against the offline store."""

    domain_ui = 'eh-wizard-ui.myshopify.com'

    @classmethod
    def setUpClass(cls):
        if websocket is None:
            raise unittest.SkipTest('the browser checks need the websocket-client Python package')
        super().setUpClass()
        if 'tour_enabled' in cls.env['res.users']._fields:
            cls.env.ref('base.user_admin').tour_enabled = False

    def setUp(self):
        super().setUp()
        # Pin the system address to the local test server, as a fresh install
        # behind no public HTTPS name would have it, so the finish step takes
        # the scheduled checks path whatever the login did to web.base.url.
        params = self.env['ir.config_parameter'].sudo()
        params.set_str('web.base.url', self.base_url())
        params.set_bool('web.base.url.freeze', True)
        self.fake_ui = testing.FakeTransport(testing.FakeShop(domain=self.domain_ui))
        testing.TRANSPORT_OVERRIDES[self.domain_ui] = self.fake_ui
        self.addCleanup(testing.TRANSPORT_OVERRIDES.pop, self.domain_ui, None)

    def test_connect_a_store_from_the_dialog(self):
        self.start_tour('/odoo/action-eh_shopify_connector.action_eh_shopify_setup_wizard',
                        'eh_shopify_setup_wizard_tour', login='admin', timeout=120)
        store = self.env['eh.shopify.store'].search([('shop_domain', '=', self.domain_ui)])
        self.assertEqual(len(store), 1, 'going back and connecting again reuses the same store')
        self.assertEqual(store.name, 'eh-wizard-ui')
        self.assertEqual(store.auth_mode, 'client_credentials')
        self.assertEqual(store._credential()._secret('client_secret'), 'eh-client-secret')
        self.assertEqual(store.shop_name, 'EH Test Store')
        self.assertTrue(store.orders_enabled)
        self.assertTrue(store.order_import_from)
        self.assertFalse(store.order_confirm)
        self.assertEqual(store.invoice_policy, 'manual')
        self.assertTrue(store.webhooks_enabled)
        self.assertEqual(store.location_count, 1)
        # Odoo answers on a plain http local address, so finishing keeps the
        # store on scheduled checks and says why, on the store and in its chatter.
        self.assertFalse(self.fake_ui.shop.webhooks, 'no subscription points Shopify at a private address')
        self.assertIn('public HTTPS address', store.webhook_note)
        self.assertTrue(store.message_ids.filtered(lambda message: 'public HTTPS address' in (message.body or '')))
        self.env.cr.execute('SELECT client_secret_sealed FROM eh_shopify_setup_wizard')
        self.assertFalse([row for row in self.env.cr.fetchall() if row[0]],
                         'no typed secret is left in the dialog records')
