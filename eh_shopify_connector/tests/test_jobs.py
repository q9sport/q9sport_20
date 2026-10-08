# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from ..lib import client
from .common import ShopifyCase


@tagged('post_install', '-at_install', 'eh_shopify')
class TestJobEngine(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.Job = self.env['eh.shopify.job']

    def test_dedup_keeps_one_waiting_job(self):
        first = self.Job._enqueue(self.store, 'locations.sync', dedup_key='locations.sync', payload={'n': 1})
        second = self.Job._enqueue(self.store, 'locations.sync', dedup_key='locations.sync', payload={'n': 2})
        self.assertEqual(first, second)
        self.assertEqual(first.payload, {'n': 2})
        first._run_inline()
        third = self.Job._enqueue(self.store, 'locations.sync', dedup_key='locations.sync')
        self.assertNotEqual(third, first)

    def test_success_records_result(self):
        job = self.Job._enqueue(self.store, 'locations.sync')
        job._run_inline()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.result['created'], 1)
        self.assertEqual(job.attempts, 1)
        self.assertFalse(job.error_kind)

    def test_transient_failure_rolls_back_and_backs_off(self):
        self.fake.script(*[(503, {}, {})] * 6)
        job = self.Job._enqueue(self.store, 'locations.sync')
        job._run_inline()
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.error_kind, 'transient')
        self.assertGreater(job.next_run_at, fields.Datetime.now() + datetime.timedelta(seconds=20))
        self.assertIn('automatically', job.reason)
        self.assertFalse(self.env['eh.shopify.location'].search([('store_id', '=', self.store.id)]))

    def test_schema_rejection_needs_attention(self):
        self.fake.script((200, {}, [{'message': "Field 'x' doesn't exist", 'extensions': {'code': 'undefinedField'}}]))
        job = self.Job._enqueue(self.store, 'locations.sync')
        job._run_inline()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.error_kind, 'query')
        self.assertIn('API version', job.reason)

    def test_gives_up_after_max_attempts(self):
        self.fake.script(*[(503, {}, {})] * 6)
        job = self.Job._enqueue(self.store, 'locations.sync', max_attempts=1)
        job._run_inline()
        self.assertEqual(job.state, 'dead')

    def test_unanswered_mutation_is_not_replayed(self):
        self.fake.script(None, client.TransportReadTimeout('late'))
        job = self.Job._enqueue(self.store, 'webhooks.register')
        job._run_inline()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.error_kind, 'ambiguous')
        self.assertIn('did not confirm', job.reason)

    def test_scope_failure_flags_store(self):
        self.fake.script((200, {}, {'errors': [{'message': 'Access denied', 'extensions': {
            'code': 'ACCESS_DENIED', 'requiredAccess': '`read_locations` access scope.'}}]}))
        job = self.Job._enqueue(self.store, 'locations.sync')
        job._run_inline()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.store.state, 'attention')
        self.assertIn('read_locations', self.store.last_error)

    def test_claim_respects_resource_order(self):
        order = 'gid://shopify/Order/1'
        older = self.Job._enqueue(self.store, 'store.check', resource_key=order)
        newer = self.Job._enqueue(self.store, 'store.check', resource_key=order)
        other = self.Job._enqueue(self.store, 'store.check', resource_key='gid://shopify/Order/2', priority=20)
        self.env.flush_all()

        def claim():
            claimed = self.Job._claim_next(cr=self.env.cr)
            return claimed[0] if claimed else None

        first = self.Job._claim_next(cr=self.env.cr)
        self.assertEqual(first[0], older.id)
        self.assertTrue(first[1], 'a claim must carry a token')
        self.assertEqual(claim(), other.id)
        self.assertIsNone(claim())
        self.env.cr.execute("UPDATE eh_shopify_job SET state = 'done' WHERE id = %s", (older.id,))
        self.assertEqual(claim(), newer.id)

    def test_disconnected_store_jobs_wait(self):
        job = self.Job._enqueue(self.store, 'store.check')
        self.store.state = 'disconnected'
        self.env.flush_all()
        self.assertIsNone(self.Job._claim_next(cr=self.env.cr))
        self.assertEqual(job.state, 'pending')

    def test_stale_running_job_is_reset(self):
        job = self.Job._enqueue(self.store, 'store.check')
        job.write({'state': 'running', 'attempts': 2, 'locked_at': fields.Datetime.now() - datetime.timedelta(hours=1)})
        stuck = self.Job._enqueue(self.store, 'locations.sync')
        stuck.write({'state': 'running', 'attempts': 8, 'max_attempts': 8,
                     'locked_at': fields.Datetime.now() - datetime.timedelta(hours=1)})
        self.env.flush_all()
        self.assertEqual(self.Job._reset_stale(), 2)
        (job | stuck).invalidate_recordset()
        self.assertEqual(job.state, 'pending')
        self.assertGreater(job.next_run_at, fields.Datetime.now())
        self.assertEqual(stuck.state, 'dead')

    def test_late_result_after_reset_is_ignored(self):
        job = self.Job._enqueue(self.store, 'store.check')
        job.write({'state': 'running', 'claim_token': 'current'})
        self.assertFalse(job._mark_done({'late': True}, 0.0, 'stale-token'))
        self.assertEqual(job.state, 'running')
        self.assertTrue(job._mark_done({'ok': True}, 0.0, 'current') is None)
        self.assertEqual(job.state, 'done')

    def test_retry_next_to_live_duplicate_does_not_collide(self):
        old = self.Job._enqueue(self.store, 'store.check', dedup_key='store.check')
        old.write({'state': 'failed'})
        live = self.Job._enqueue(self.store, 'store.check', dedup_key='store.check')
        live.next_run_at = fields.Datetime.now() + datetime.timedelta(hours=1)
        old.action_retry()
        self.assertEqual(old.state, 'cancelled')
        self.assertIn(str(live.id), old.last_error)
        self.assertLessEqual(live.next_run_at, fields.Datetime.now())

    def test_cancelled_jobs_are_purged_and_their_events_closed(self):
        event, _created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-c', 'X-Shopify-Topic': 'products/create'}, {'id': 3})
        event.job_id.action_cancel()
        self.assertTrue(event.job_id.finished_at)
        self.assertEqual(event.state, 'ignored')
        self.env.flush_all()
        self.env.cr.execute("UPDATE eh_shopify_job SET finished_at = NULL, write_date = %s WHERE id = %s",
                            (fields.Datetime.now() - datetime.timedelta(days=90), event.job_id.id))
        job_id = event.job_id.id
        self.env.invalidate_all()
        self.Job._cron_purge()
        self.assertFalse(self.Job.browse(job_id).exists())

    def test_maintenance_does_not_requeue_work_that_failed_for_good(self):
        failed = self.Job._enqueue(self.store, 'orders.poll', dedup_key='orders.poll')
        failed.write({'state': 'failed', 'error_kind': 'auth'})
        self.store.orders_polled_until = False
        self.env['eh.shopify.store']._cron_maintenance()
        self.assertEqual(self.Job.search_count([('store_id', '=', self.store.id), ('dedup_key', '=', 'orders.poll')]), 1)

    def test_run_now_respects_resource_order(self):
        first = self.Job._enqueue(self.store, 'store.check', resource_key='gid://shopify/Order/5')
        first.next_run_at = fields.Datetime.now() + datetime.timedelta(minutes=5)
        second = self.Job._enqueue(self.store, 'store.check', resource_key='gid://shopify/Order/5')
        with self.assertRaises(UserError):
            second.action_run_now()
        self.assertEqual(second.state, 'pending')

    def test_user_can_retry(self):
        user = self._user('retry_user', ['eh_shopify_connector.group_shopify_user'])
        job = self.Job._enqueue(self.store, 'store.check')
        job.write({'state': 'dead', 'attempts': 8})
        job.with_user(user).action_retry()
        self.assertEqual(job.state, 'pending')
        self.assertGreaterEqual(job.max_attempts, 11)

    def test_only_managers_run_or_cancel_and_nobody_crafts_jobs(self):
        user = self._user('plain_user', ['eh_shopify_connector.group_shopify_user'])
        manager = self._user('job_manager', ['eh_shopify_connector.group_shopify_manager'])
        job = self.Job._enqueue(self.store, 'store.check')
        with self.assertRaises(AccessError):
            job.with_user(user).action_run_now()
        with self.assertRaises(AccessError):
            job.with_user(user).action_cancel()
        with self.assertRaises(AccessError):
            self.Job.with_user(manager).create({'name': 'crafted', 'store_id': self.store.id,
                                                'job_type': 'event.process', 'payload': {'event_id': 1}})
        job.with_user(manager).action_cancel()
        self.assertEqual(job.state, 'cancelled')

    def test_event_job_for_another_store_is_refused(self):
        other = self.env['eh.shopify.store'].create({'shop_domain': 'eh-other.myshopify.com', 'state': 'connected'})
        event, _created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-x', 'X-Shopify-Topic': 'app/uninstalled'}, {'id': 1})
        crafted = self.Job._enqueue(other, 'event.process', payload={'event_id': event.id})
        crafted._run_inline()
        self.assertEqual(crafted.result, {'skipped': 'event belongs to another store'})
        self.assertEqual(self.store.state, 'connected')

    def test_purge_keeps_failures(self):
        old = fields.Datetime.now() - datetime.timedelta(days=60)
        done = self.Job._enqueue(self.store, 'store.check')
        failed = self.Job._enqueue(self.store, 'locations.sync')
        done.write({'state': 'done', 'finished_at': old})
        failed.write({'state': 'failed', 'finished_at': old})
        self.Job._cron_purge()
        self.assertFalse(done.exists())
        self.assertTrue(failed.exists())

    def test_unknown_topic_is_ignored_not_failed(self):
        event, _created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-9', 'X-Shopify-Topic': 'collections/create'}, {'id': 5})
        event.job_id._run_inline()
        self.assertEqual(event.state, 'ignored')
        self.assertEqual(event.job_id.state, 'done')

    def test_location_webhook_queues_debounced_sync(self):
        event, _created = self.env['eh.shopify.event']._receive(self.store, {
            'X-Shopify-Webhook-Id': 'w-10', 'X-Shopify-Topic': 'locations/update'},
            {'admin_graphql_api_id': 'gid://shopify/Location/1'})
        event.job_id._run_inline()
        sync = self.Job.search([('store_id', '=', self.store.id), ('dedup_key', '=', 'locations.sync')])
        self.assertEqual(len(sync), 1)
        self.assertGreater(sync.next_run_at, fields.Datetime.now())

    def test_maintenance_queues_renewal_and_selfheal(self):
        self.store._credential().token_expires_at = fields.Datetime.now() + datetime.timedelta(minutes=5)
        self.env['eh.shopify.store']._cron_maintenance()
        types = set(self.Job.search([('store_id', '=', self.store.id)]).mapped('job_type'))
        self.assertIn('token.refresh', types)
        self.assertIn('webhooks.register', types)

    def test_a_cancelled_job_comes_back_only_for_a_manager(self):
        job = self.env['eh.shopify.job']._enqueue(self.store, 'orders.poll', dedup_key='orders.poll:rights')
        job.write({'state': 'cancelled'})
        user = self._user('shopify-reader', ['eh_shopify_connector.group_shopify_user'])
        with self.assertRaises(AccessError):
            job.with_user(user).action_retry()
        manager = self._user('shopify-boss', ['eh_shopify_connector.group_shopify_manager'])
        job.with_user(manager).action_retry()
        self.assertEqual(job.state, 'pending', 'a manager brings back what a manager cancelled')
