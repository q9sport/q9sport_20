# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart


@tagged('post_install', '-at_install', 'eh_shopify')
class TestOrderCancel(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.env['product.product'].create([{'name': 'Brass gear %s' % i, 'default_code': 'SKU-%s' % i}
                                           for i in range(2)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Job = self.env['eh.shopify.job']
        self.Wizard = self.env['eh.shopify.cancel.wizard']

    def _import(self, order):
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def _dialog(self, sale_order):
        action = sale_order.action_cancel()
        self.assertEqual(action.get('res_model'), 'eh.shopify.cancel.wizard')
        return self.Wizard.browse(action['res_id'])

    def _cancel_job(self):
        return self.Job.search([('job_type', '=', 'order.cancel'), ('store_id', '=', self.store.id)])

    def test_cancel_on_a_shopify_order_opens_the_dialog(self):
        binding = self._import(make_order(number=81))
        wizard = self._dialog(binding.sale_order_id)
        self.assertIn(binding.sale_order_id.state, ('sale', 'done'))
        self.assertFalse(wizard.blocked_reason)
        self.assertEqual((wizard.refund_amount, wizard.refund_currency), (10.0, 'USD'))

    def test_cancel_in_shopify_then_odoo_follows(self):
        order = make_order(number=82)
        binding = self._import(order)
        wizard = self._dialog(binding.sale_order_id)
        wizard.write({'reason': 'INVENTORY', 'refund_method': 'original', 'restock': True,
                      'notify_customer': False, 'staff_note': 'Out of brass'})
        wizard.action_cancel_in_shopify()
        job = self._cancel_job()
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        sent = self.shop.cancel_requests[-1]
        self.assertEqual((sent['reason'], sent['restock'], sent['notifyCustomer'], sent['staffNote']),
                         ('INVENTORY', True, False, 'Out of brass'))
        self.assertEqual(sent['refundMethod'], {'originalPaymentMethodsRefund': True})
        self.assertNotEqual(binding.sale_order_id.state, 'cancel', 'Odoo waits for Shopify to confirm')
        follow = self.Job.search([('job_type', '=', 'order.import'), ('state', '=', 'pending'),
                                  ('dedup_key', '=', 'order:%s' % order['id'])])
        self.assertEqual(len(follow), 1)
        follow._run_inline()
        self.assertEqual(follow.state, 'done', follow.last_error)
        self.assertEqual(binding.sale_order_id.state, 'cancel')

    def test_already_cancelled_in_shopify_is_not_cancelled_twice(self):
        order = make_order(number=83)
        binding = self._import(order)
        wizard = self._dialog(binding.sale_order_id)
        order['cancelledAt'] = '2026-09-16T07:00:00Z'
        wizard.action_cancel_in_shopify()
        job = self._cancel_job()
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertIn('already_cancelled', job.result)
        self.assertFalse(self.shop.cancel_requests)

    def test_shipped_order_is_blocked_with_an_explanation(self):
        order = make_order(number=84)
        binding = self._import(order)
        order['displayFulfillmentStatus'] = 'FULFILLED'
        wizard = self._dialog(binding.sale_order_id)
        self.assertTrue(wizard.blocked_reason)
        with self.assertRaises(UserError):
            wizard.action_cancel_in_shopify()
        self.assertFalse(self._cancel_job())

    def test_cancel_in_odoo_only_leaves_shopify_alone(self):
        binding = self._import(make_order(number=85))
        wizard = self._dialog(binding.sale_order_id)
        wizard.action_cancel_in_odoo_only()
        self.assertEqual(binding.sale_order_id.state, 'cancel')
        self.assertFalse(self.shop.cancel_requests)
        self.assertFalse(self._cancel_job())

    def test_refusal_is_explained_and_never_replayed(self):
        binding = self._import(make_order(number=86))
        wizard = self._dialog(binding.sale_order_id)
        wizard.refund_method = 'store_credit'
        self.shop.cancel_errors = [{'field': ['refundMethod'], 'message': 'Store credit is not available for this order',
                                    'code': 'STORE_CREDIT_REFUND_B2B_NOT_SUPPORTED'}]
        wizard.action_cancel_in_shopify()
        job = self._cancel_job()
        job._run_inline()
        self.assertEqual(job.state, 'failed')
        self.assertIn('Store credit is not available', job.last_error)
        self.assertEqual(len(self.shop.cancel_requests), 1)
        self.assertEqual(self.shop.cancel_requests[0]['refundMethod'],
                         {'storeCreditRefund': {'amount': {'amount': '10.00', 'currencyCode': 'USD'}}})

    def test_only_shopify_managers_cancel_in_shopify(self):
        binding = self._import(make_order(number=87))
        user = self._user('eh_sales_rep', ['sales_team.group_sale_salesman_all_leads'])
        action = binding.sale_order_id.with_user(user).action_cancel()
        wizard = self.Wizard.with_user(user).browse(action['res_id'])
        self.assertFalse(wizard.blocked_reason)
        with self.assertRaises(AccessError):
            wizard.action_cancel_in_shopify()
        self.assertFalse(self._cancel_job())

    def test_several_orders_including_a_shopify_one_are_refused(self):
        binding = self._import(make_order(number=88))
        other = binding.sale_order_id.copy()
        with self.assertRaises(UserError):
            (binding.sale_order_id | other).action_cancel()

    def test_delivery_shipped_in_odoo_blocks_cancel_in_shopify(self):
        binding = self._import(make_order(number=89))
        picking = binding.sale_order_id.picking_ids
        compat.validate_picking(picking)
        wizard = self._dialog(binding.sale_order_id)
        self.assertIn(picking.name, wizard.blocked_reason)
        with self.assertRaises(UserError):
            wizard.action_cancel_in_shopify()
        self.assertFalse(self._cancel_job())

    def test_locked_order_must_be_unlocked_first(self):
        order = self._import(make_order(number=90)).sale_order_id
        if 'locked' in order._fields:
            order.action_lock()
        else:
            order.action_done()
        with self.assertRaises(UserError):
            order.action_cancel()
        self.assertNotEqual(order.state, 'cancel')
