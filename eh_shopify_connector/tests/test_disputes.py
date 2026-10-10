# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from odoo import fields
from odoo.tests import tagged

from .common import ShopifyCase
from .test_order_import import make_order

STAMP = '%Y-%m-%dT%H:%M:%SZ'


@tagged('post_install', '-at_install', 'eh_shopify')
class TestDisputes(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['product.product'].create([{'name': 'Dispute gear %s' % i, 'default_code': 'SKU-%s' % i} for i in range(2)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Dispute = self.env['eh.shopify.dispute']
        self.Job = self.env['eh.shopify.job']
        self.today = fields.Date.context_today(self.Dispute)
        self.opened = (fields.Datetime.now() - datetime.timedelta(days=2)).strftime(STAMP)

    def _import(self, number):
        order = make_order(number=number, lines=1)
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def _sync(self, gid):
        job = self.Dispute._enqueue_sync(self.store, gid)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Dispute.search([('store_id', '=', self.store.id), ('shopify_gid', '=', gid)])

    def test_a_chargeback_needing_an_answer_puts_a_todo_on_the_order(self):
        binding = self._import(61)
        due = self.today + datetime.timedelta(days=20)
        record = self.shop.add_dispute(611, binding.shopify_gid, 'NEEDS_RESPONSE', due.strftime('%Y-%m-%d'), self.opened)
        event = self.env['eh.shopify.event'].new({'store_id': self.store.id, 'topic': 'disputes/create',
                                                  'webhook_id': 'w-d611', 'payload': {'id': 611, 'order_id': 61}})
        self.assertEqual(event._on_dispute_change(), {'queued': record['id']})
        job = self.Job.search([('job_type', '=', 'dispute.sync'), ('state', '=', 'pending')])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        dispute = self.Dispute.search([('shopify_gid', '=', record['id'])])
        self.assertEqual((dispute.state, dispute.sale_order_id), ('open', binding.sale_order_id))
        self.assertEqual(dispute.activity_id.res_id, binding.sale_order_id.id)
        self.assertEqual(dispute.activity_id.date_deadline, due - datetime.timedelta(days=2))
        again = self._sync(record['id'])
        self.assertEqual(len(binding.sale_order_id.activity_ids.filtered(
            lambda activity: activity.summary == 'Answer the Shopify chargeback')), 1, 'one to-do per dispute')
        self.assertEqual(again, dispute)

    def test_a_decided_dispute_closes_the_todo(self):
        binding = self._import(62)
        due = (self.today + datetime.timedelta(days=10)).strftime('%Y-%m-%d')
        record = self.shop.add_dispute(621, binding.shopify_gid, 'NEEDS_RESPONSE', due, self.opened)
        dispute = self._sync(record['id'])
        self.assertTrue(dispute.activity_id)
        record['status'] = 'WON'
        self._sync(record['id'])
        self.assertEqual(dispute.state, 'won')
        self.assertFalse(dispute.activity_id)
        self.assertFalse(binding.sale_order_id.activity_ids.filtered(
            lambda activity: activity.summary == 'Answer the Shopify chargeback'))

    def test_disputes_are_read_on_a_schedule_with_either_date_format(self):
        binding = self._import(63)
        self.store.granted_scopes = (self.store.granted_scopes or '') + ',read_shopify_payments_disputes'
        due = self.today + datetime.timedelta(days=15)
        self.shop.add_dispute(631, binding.shopify_gid, 'UNDER_REVIEW', due.strftime('%Y-%m-%dT00:00:00Z'), self.opened)
        poll = self.Job._enqueue(self.store, 'disputes.poll', dedup_key='disputes.poll')
        poll._run_inline()
        self.assertEqual(poll.state, 'done', poll.last_error)
        self.assertEqual(poll.result, {'disputes': 1})
        dispute = self.Dispute.search([('store_id', '=', self.store.id)])
        self.assertEqual((dispute.state, dispute.evidence_due_by), ('review', due))
        self.assertTrue(self.store.disputes_polled_at)

    def test_dispute_topics_and_polling_need_the_disputes_permission(self):
        self.assertNotIn('DISPUTES_CREATE', self.store._webhook_topics())
        poll = self.Job._enqueue(self.store, 'disputes.poll', dedup_key='disputes.poll')
        poll._run_inline()
        self.assertEqual(poll.result, {'skipped': 'missing permission'})
        self.store.granted_scopes = (self.store.granted_scopes or '') + ',read_shopify_payments_disputes'
        self.assertIn('DISPUTES_CREATE', self.store._webhook_topics())
