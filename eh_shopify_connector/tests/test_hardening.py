# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""What a person meets: screens, menus, wizards, crons and rights.

The rest of the suite proves the sync. This proves the module can be opened and
lived with: every screen renders, every action and menu resolves, every cron
survives a store that is not connected or not there at all, and the people who
do the work can do it without being a Shopify manager.
"""
from unittest.mock import patch

from odoo.exceptions import AccessError, MissingError, UserError
from odoo.tests import tagged
from odoo.tools import safe_eval

from .. import compat
from ..lib import errors
from .common import ShopifyCase
from .test_order_import import make_order


@tagged('post_install', '-at_install', 'eh_shopify')
class TestHardening(ShopifyCase):

    def _records(self, model):
        data = self.env['ir.model.data'].sudo().search([('module', '=', 'eh_shopify_connector'),
                                                        ('model', '=', model)])
        return self.env[model].sudo().browse(data.mapped('res_id')).exists()

    def _view_type(self, mode):
        types = [value for value, _label in self.env['ir.ui.view']._fields['type'].selection]
        if mode in types:
            return mode
        return 'tree' if mode == 'list' else ('list' if mode == 'tree' else mode)

    def _open_view(self, model, view_id, view_type, user=None):
        target = self.env[model].with_context(lang='en_US')
        if user:
            target = target.with_user(user)
        if hasattr(target, 'get_view'):
            return target.get_view(view_id, view_type)
        return target.fields_view_get(view_id, view_type)

    def test_every_screen_of_the_module_opens(self):
        views = self._records('ir.ui.view')
        opened = 0
        self.assertGreater(len(views), 15, 'the module ships screens')
        failures = []
        for view in views:
            if view.inherit_id or view.type == 'qweb' or not view.model:
                continue
            if view.model not in self.env:
                failures.append('%s: the model %s is gone' % (view.name, view.model))
                continue
            try:
                self._open_view(view.model, view.id, view.type)
                opened += 1
            except Exception as error:
                failures.append('%s (%s): %s' % (view.name, view.type, error))
        self.assertFalse(failures, 'screens that do not open: %s' % failures)
        self.assertGreater(opened, 10, 'the screens were really opened, not skipped')

    def test_every_action_and_menu_of_the_module_resolves(self):
        actions, menus = self._records('ir.actions.act_window'), self._records('ir.ui.menu')
        self.assertGreater(len(actions), 5, 'the module ships actions')
        self.assertGreater(len(menus), 5, 'the module ships menus')
        failures = []
        names = {'uid': self.env.uid, 'user': self.env.user, 'context': dict(self.env.context),
                 'active_id': 1, 'active_ids': [1], 'active_model': 'eh.shopify.store',
                 'allowed_company_ids': self.env.company.ids}
        for action in actions:
            if action.res_model not in self.env:
                failures.append('%s: the model %s is gone' % (action.name, action.res_model))
                continue
            for source, label in ((action.domain, 'domain'), (action.context, 'context')):
                try:
                    safe_eval.safe_eval(source or ('[]' if label == 'domain' else '{}'), dict(names))
                except Exception as error:
                    failures.append('%s: the %s does not read: %s' % (action.name, label, error))
            for mode in (action.view_mode or 'list').split(','):
                try:
                    self._open_view(action.res_model, False, self._view_type(mode.strip()))
                except Exception as error:
                    failures.append('%s (%s): %s' % (action.name, mode.strip(), error))
        for menu in menus:
            action = menu.action
            if action and getattr(action, 'res_model', None) and action.res_model not in self.env:
                failures.append('the menu %s points at a model that is gone' % menu.complete_name)
        self.assertFalse(failures, 'actions that do not resolve: %s' % failures)

    def test_every_wizard_of_the_module_can_be_opened(self):
        failures = []
        wizards = self.env['ir.model'].sudo().search([('model', 'like', 'eh.shopify.%wizard%')])
        self.assertGreater(len(wizards), 4, 'the module ships wizards')
        for record in wizards:
            model = self.env[record.model]
            try:
                model.default_get([name for name in model._fields][:30])
                self._open_view(record.model, False, 'form')
            except Exception as error:
                failures.append('%s: %s' % (record.model, error))
        self.assertFalse(failures, 'wizards that do not open: %s' % failures)

    def test_every_cron_survives_a_store_that_is_not_ready(self):
        crons = self._records('ir.cron')
        self.assertTrue(crons, 'the module ships scheduled work')
        for state in ('draft', 'disconnected'):
            self.store.write({'state': state})
            for cron in crons:
                try:
                    cron.sudo().method_direct_trigger()
                except Exception as error:
                    self.fail('%s crashed with a store that is %s: %s' % (cron.cron_name or cron.name, state, error))
        self.store.unlink()
        for cron in crons:
            try:
                cron.sudo().method_direct_trigger()
            except Exception as error:
                self.fail('%s crashed with no store at all: %s' % (cron.cron_name or cron.name, error))

    def test_a_shopify_user_can_read_what_they_are_shown(self):
        user = self._user('hardening-reader', ['eh_shopify_connector.group_shopify_user'])
        for model in ('eh.shopify.store', 'eh.shopify.order', 'eh.shopify.order.line', 'eh.shopify.job',
                      'eh.shopify.event', 'eh.shopify.product', 'eh.shopify.variant', 'eh.shopify.location',
                      'eh.shopify.customer', 'eh.shopify.refund', 'eh.shopify.return', 'eh.shopify.payout'):
            try:
                self.env[model].with_user(user).search_read([], ['display_name'], limit=1)
            except Exception as error:
                self.fail('a Shopify user cannot read %s: %s' % (model, error))

    def test_a_warehouse_user_can_ship_a_shopify_order(self):
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.env['product.product'].create({'name': 'Hardening gear', 'default_code': 'SKU-0'})
        order = make_order(number=95)
        self.shop.add_order(order)
        job = self.env['eh.shopify.order']._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        binding = self.env['eh.shopify.order'].search([('store_id', '=', self.store.id),
                                                       ('shopify_gid', '=', order['id'])])
        picking = binding.sale_order_id.picking_ids
        user = self._user('hardening-stock', ['stock.group_stock_user'])
        compat.validate_picking(picking.with_user(user))
        self.assertEqual(picking.state, 'done', 'a warehouse user ships without being given Shopify rights')
        self.assertTrue(self.env['eh.shopify.job'].search([('store_id', '=', self.store.id),
                                                           ('job_type', '=', 'fulfillment.push')]),
                        'the shipment is queued for Shopify')

    def test_the_order_form_hides_accounting_fields_from_a_shopify_user(self):
        user = self._user('hardening-no-books', ['eh_shopify_connector.group_shopify_user'])
        form = self._open_view('eh.shopify.order', False, 'form', user=user)
        self.assertNotIn('credit_note_ids', form['arch'],
                         'a Shopify user without accounting rights is not shown credit notes, which would fail to read')
        search = self._open_view('eh.shopify.order', False, 'search', user=user)
        self.assertNotIn('sale_order_id', search['arch'],
                         'searching by sales order would fail to read for a user without sales rights')

    def test_credentials_that_cannot_be_read_say_so_plainly(self):
        self.store._credential().sudo().write({'client_secret': 'this-was-not-encrypted-by-this-database'})
        with self.assertRaises(errors.AuthError) as caught:
            self.store._credential()._webhook_secret()
        self.assertIn('key', caught.exception.message.lower(), 'the message says what went wrong')

    def test_work_whose_payload_was_cleared_refuses_to_run_again(self):
        job = self.env['eh.shopify.job']._enqueue(self.store, 'order.import', dedup_key='cleared:1',
                                                  payload={'gid': 'gid://shopify/Order/1'})
        job.write({'state': 'failed', 'payload': {'purged': True}})
        with self.assertRaises(UserError):
            job.action_retry()

    def test_deleting_a_store_that_holds_links_is_refused(self):
        self.store._check_connection()
        self.env['product.product'].create({'name': 'Hardening keep', 'default_code': 'SKU-0'})
        order = make_order(number=96)
        self.shop.add_order(order)
        self.env['eh.shopify.order']._enqueue_import(self.store, order['id'])._run_inline()
        binding = self.env['eh.shopify.order'].search([('store_id', '=', self.store.id),
                                                       ('shopify_gid', '=', order['id'])])
        self.assertTrue(binding.sale_order_id, 'the order became a sales order')
        with self.assertRaises(UserError) as caught:
            self.store.unlink()
        message = str(caught.exception)
        self.assertIn('second time', message, 'the message says why deleting would hurt')
        self.assertIn('Disconnect', message, 'the message says what to do instead')
        self.assertTrue(binding.exists(), 'nothing was deleted')

    def test_deleting_an_imported_order_is_refused(self):
        self.store._check_connection()
        self.env['product.product'].create({'name': 'Hardening keep two', 'default_code': 'SKU-0'})
        order = make_order(number=97)
        self.shop.add_order(order)
        self.env['eh.shopify.order']._enqueue_import(self.store, order['id'])._run_inline()
        binding = self.env['eh.shopify.order'].search([('store_id', '=', self.store.id),
                                                       ('shopify_gid', '=', order['id'])])
        with self.assertRaises(UserError) as caught:
            binding.unlink()
        self.assertIn('second sales order', str(caught.exception), 'the message explains the duplicate')

    def _failing_job(self, error):
        def explode(self, *args, **kwargs):
            raise error
        job = self.env['eh.shopify.job']._enqueue(self.store, 'locations.sync')
        with patch.object(type(self.env['eh.shopify.store']), '_job_locations_sync', explode):
            job._run_inline()
        return job

    def test_an_odoo_refusal_is_explained_and_not_retried(self):
        job = self._failing_job(UserError('Set a customer on this order first.'))
        self.assertEqual(job.state, 'failed', 'it waits in the exceptions inbox instead of retrying')
        self.assertEqual(job.error_kind, 'mapping')
        self.assertEqual(job.attempts, 1, 'the same refusal is not attempted eight times')
        self.assertIn('Set a customer', job.last_error, 'the merchant reads what Odoo actually said')
        self.assertNotIn('unexpected', job.reason.lower())

    def test_missing_rights_and_missing_records_read_differently(self):
        denied = self._failing_job(AccessError('You are not allowed to modify Sales Order.'))
        self.assertEqual(denied.error_kind, 'permission')
        self.assertEqual(denied.state, 'failed')
        self.assertIn('access rights', denied.reason)
        gone = self._failing_job(MissingError('Record does not exist or has been deleted.'))
        self.assertEqual(gone.error_kind, 'record_gone')
        self.assertEqual(gone.state, 'failed')
        self.assertIn('deleted', gone.reason)

    def test_an_order_left_out_can_be_asked_for_again(self):
        binding = self.env['eh.shopify.order'].create({
            'store_id': self.store.id, 'shopify_gid': 'gid://shopify/Order/9901', 'name': '#9901',
            'state': 'held', 'reason': 'A product was missing.'})
        reader = self._user('hardening-not-manager', ['eh_shopify_connector.group_shopify_user'])
        with self.assertRaises(AccessError):
            binding.with_user(reader).action_import_again()
        self.store.write({'state': 'connected'})
        binding.action_import_again()
        self.assertTrue(self.env['eh.shopify.job'].search([('store_id', '=', self.store.id),
                                                           ('job_type', '=', 'order.import'),
                                                           ('resource_key', '=', binding.shopify_gid)]),
                        'the order is queued for import again')
        self.store.write({'state': 'disconnected'})
        with self.assertRaises(UserError) as caught:
            binding.action_import_again()
        self.assertIn('not connected', str(caught.exception))

    def test_a_store_that_is_not_connected_says_its_work_is_waiting(self):
        self.env['eh.shopify.job']._enqueue(self.store, 'locations.sync', dedup_key='waiting:1')
        self.store.write({'state': 'disconnected'})
        self.store.invalidate_recordset(['health', 'health_notes'])
        self.assertEqual(self.store.health, 'off')
        self.assertIn('waiting', self.store.health_notes,
                      'the merchant is told the queue is not moving, instead of finding it stuck')

    def test_a_store_says_which_features_are_off(self):
        self.store._check_connection()
        self.store.invalidate_recordset(['health', 'health_notes'])
        self.assertTrue(self.store.missing_optional_scopes, 'the fake shop grants only what it was asked for')
        notes = self.store.health_notes
        self.assertNotEqual(notes, 'Everything is working.',
                            'a store with features switched off does not report that all is well')
        self.assertIn('permission', notes)

    def test_a_system_administrator_sees_the_app(self):
        admin = self._user('hardening-sysadmin', ['base.group_system'])
        self.assertTrue(admin.has_group('eh_shopify_connector.group_shopify_manager'),
                        'whoever installed the app finds it, not only the user the database was made with')
        menu = self.env.ref('eh_shopify_connector.menu_eh_shopify_root')
        self.assertTrue(menu.with_user(admin).read(['name']), 'the Shopify menu is visible to them')
        self.assertTrue(menu.web_icon, 'the app has its own tile in the Apps drawer')

    def test_work_is_closed_when_a_store_stops_being_connected(self):
        self.store.write({'state': 'connected'})
        job = self.env['eh.shopify.job']._enqueue(self.store, 'locations.sync', dedup_key='stranded:1')
        self.store.write({'active': False})
        self.assertEqual(job.state, 'cancelled',
                         'work that can never run again is closed, not left waiting for ever')
        self.store.write({'active': True, 'state': 'connected'})
        other = self.env['eh.shopify.job']._enqueue(self.store, 'locations.sync', dedup_key='stranded:2')
        self.store._on_app_uninstalled()
        self.assertEqual(other.state, 'cancelled', 'uninstalling the app in Shopify closes the work too')

    def test_a_delivery_whose_work_gave_up_does_not_stay_queued(self):
        job = self.env['eh.shopify.job']._enqueue(self.store, 'event.process', dedup_key='stuck:1',
                                                  payload={'event_id': 0})
        event = self.env['eh.shopify.event'].create({
            'store_id': self.store.id, 'topic': 'orders/create', 'webhook_id': 'eh-stuck-1',
            'payload': {}, 'job_id': job.id})
        job._mark_failed(errors.MappingError('No product matches this line.'), 0.0)
        self.assertIn(job.state, ('failed', 'dead'))
        self.assertEqual(event.state, 'failed', 'the delivery says what happened instead of reading Queued for ever')
        self.assertIn('product', event.note)

    def test_the_install_link_leaves_out_permissions_the_app_does_not_have(self):
        asked = self.store._requested_scopes()
        self.assertIn('read_orders', asked)
        self.assertNotIn('read_shopify_payments_payouts', asked,
                         'asking for a permission the app was not approved for makes Shopify refuse the whole link')
        self.store.request_protected_scopes = True
        self.assertIn('read_shopify_payments_payouts', self.store._requested_scopes())
