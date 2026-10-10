# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Durable job engine.

Every unit of sync work is a row. The runner claims one row at a time with
``FOR UPDATE SKIP LOCKED`` so several workers can run safely, executes it in
its own database transaction so one failure never rolls back another, and
schedules retries with exponential backoff. Work for the same Shopify resource
runs strictly in order. Nothing is dropped: jobs that keep failing end in the
exceptions inbox with a plain explanation and a Retry button.
"""
import datetime
import json
import logging
import random
import time
import traceback
import uuid

from psycopg2 import IntegrityError
from psycopg2.extensions import TransactionRollbackError

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessDenied, AccessError, MissingError, RedirectWarning, UserError

from .. import compat
from ..lib import errors

_logger = logging.getLogger(__name__)

BASE_DELAY = 30
MAX_DELAY = 6 * 3600
STALE_AFTER_MINUTES = 15
# Cron workers are killed after limit_time_real (120 s by default). A run
# starts jobs for at most RUN_BUDGET seconds and each job stops asking Shopify
# for more after JOB_BUDGET seconds, so a run always finishes inside the limit.
RUN_BUDGET = 25.0
JOB_BUDGET = 80.0
# Advisory lock class held by the transaction executing a job.
LOCK_CLASS = 72941

HANDLERS = {
    'store.check': ('eh.shopify.store', '_job_store_check'),
    'locations.sync': ('eh.shopify.store', '_job_locations_sync'),
    'webhooks.register': ('eh.shopify.store', '_job_webhooks_register'),
    'token.refresh': ('eh.shopify.store', '_job_token_refresh'),
    'event.process': ('eh.shopify.event', '_job_process'),
    'order.import': ('eh.shopify.order', '_job_order_import'),
    'orders.poll': ('eh.shopify.store', '_job_orders_poll'),
    'fulfillment.push': ('eh.shopify.order', '_job_fulfillment_push'),
    'inventory.flush': ('eh.shopify.store', '_job_inventory_flush'),
    'fulfillment.import': ('eh.shopify.order', '_job_fulfillment_import'),
    'order.cancel': ('eh.shopify.order', '_job_order_cancel'),
    'fulfillment.tracking': ('eh.shopify.order', '_job_fulfillment_tracking'),
    'pickup.ready': ('eh.shopify.order', '_job_pickup_ready'),
    'product.import': ('eh.shopify.product', '_job_product_import'),
    'products.poll': ('eh.shopify.product', '_job_products_poll'),
    'bulk.start': ('eh.shopify.bulk', '_job_bulk_start'),
    'product.push': ('eh.shopify.product', '_job_product_push'),
    'product.publish': ('eh.shopify.product', '_job_product_publish'),
    'refund.import': ('eh.shopify.refund', '_job_refund_import'),
    'refund.create': ('eh.shopify.refund', '_job_refund_create'),
    'return.sync': ('eh.shopify.return', '_job_return_sync'),
    'bulk.poll': ('eh.shopify.bulk', '_job_bulk_poll'),
    'bulk.read': ('eh.shopify.bulk', '_job_bulk_read'),
    'bulk.chunk': ('eh.shopify.bulk', '_job_bulk_chunk'),
    'product.image': ('eh.shopify.product', '_job_product_image'),
    'product.publish.check': ('eh.shopify.product', '_job_product_publish_check'),
    'payouts.poll': ('eh.shopify.store', '_job_payouts_poll'),
    'payout.post': ('eh.shopify.payout', '_job_payout_post'),
    'disputes.poll': ('eh.shopify.dispute', '_job_disputes_poll'),
    'dispute.sync': ('eh.shopify.dispute', '_job_dispute_sync'),
    'price_lists.sync': ('eh.shopify.store', '_job_price_lists_sync'),
    'price_list.push': ('eh.shopify.price.list', '_job_price_list_push'),
    'customer.sync': ('eh.shopify.customer', '_job_customer_sync'),
    'company.sync': ('eh.shopify.customer', '_job_company_sync'),
    'gift_cards.opening': ('eh.shopify.store', '_job_gift_card_opening'),
    'order.edit.push': ('eh.shopify.order', '_job_order_edit_push'),
    'order.invoice': ('eh.shopify.order', '_job_order_invoice'),
    'products.price_check': ('eh.shopify.product', '_job_products_price_check'),
}

REASONS = {
    'auth': 'Shopify rejected the connection. Reconnect the store, then retry.',
    'scope': 'The Shopify app is missing a permission. Add it in Shopify, approve it on the store, then retry.',
    'throttled': 'Shopify asked us to slow down. This retries automatically.',
    'transient': 'Shopify or the network was unavailable. This retries automatically.',
    'ambiguous': 'Shopify did not confirm whether the change was applied. Check it in Shopify, then Retry or '
                 'Cancel.',
    'query': 'This API version rejected the request. Choose a newer API version on the store or contact support.',
    'cost_exceeded': 'The request was too large for Shopify. This retries with smaller pages.',
    'user_errors': 'Shopify refused the change.',
    'shop_unavailable': 'The Shopify store is paused, frozen or closed. This retries later.',
    'incomplete': 'Shopify returned partial data. This retries automatically.',
    'not_found': 'The record no longer exists in Shopify.',
    'version': 'The API version is outside the supported window. Choose a supported version on the store.',
    'bulk': 'A Shopify bulk export failed. This retries automatically.',
    'serialization': 'Two updates collided in the database. This retries automatically.',
    'mapping': 'Something in Odoo needs your attention for this record. The message says what; fix it, then retry.',
    'permission': 'Odoo refused this work because of access rights. Give the connector user the rights the '
                  'message names, then retry.',
    'record_gone': 'The Odoo record this work was about has been deleted. Cancel this work, or import the '
                   'record again from Shopify.',
    'unexpected': 'An unexpected error occurred. Technical details are below.',
}

CLAIM_SQL = """
    SELECT j.id
      FROM eh_shopify_job j
      JOIN eh_shopify_store s ON s.id = j.store_id
     WHERE j.state = 'pending'
       AND j.next_run_at <= (clock_timestamp() AT TIME ZONE 'UTC')
       AND s.active
       AND s.state IN ('connected', 'attention')
       AND (j.resource_key IS NULL OR NOT EXISTS (
            SELECT 1
              FROM eh_shopify_job o
             WHERE o.store_id = j.store_id
               AND o.resource_key = j.resource_key
               AND o.id <> j.id
               AND (o.state = 'running' OR (o.state = 'pending' AND o.id < j.id))))
     ORDER BY j.priority, j.next_run_at, j.id
     LIMIT 1
       FOR UPDATE OF j SKIP LOCKED
"""


class ShopifyJob(models.Model):
    _name = 'eh.shopify.job'
    _description = 'Shopify Sync Job'
    _order = 'id desc'

    name = fields.Char(required=True)
    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    job_type = fields.Char(required=True, index=True)
    state = fields.Selection([
        ('pending', 'Waiting'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Needs attention'),
        ('dead', 'Gave up'),
        ('cancelled', 'Cancelled'),
    ], default='pending', required=True, index=True)
    priority = fields.Integer(default=10)
    dedup_key = fields.Char(index=True)
    resource_key = fields.Char(index=True)
    payload = fields.Json('Raw payload')
    result = fields.Json('Raw result')
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer('Attempt limit', default=8)
    next_run_at = fields.Datetime('Next run', default=fields.Datetime.now, index=True)
    started_at = fields.Datetime('Started')
    finished_at = fields.Datetime('Finished')
    locked_at = fields.Datetime('Locked')
    duration_ms = fields.Integer('Duration (ms)')
    error_kind = fields.Char()
    last_error = fields.Text()
    error_details = fields.Json('Raw error details')
    correlation_id = fields.Char('Correlation ID', default=lambda self: uuid.uuid4().hex, copy=False)
    res_model = fields.Char('Related model')
    res_id = fields.Integer('Related record')
    claim_token = fields.Char(copy=False)
    reason = fields.Char(compute='_compute_reason')
    payload_text = fields.Text('Payload', compute='_compute_texts')
    result_text = fields.Text('Result', compute='_compute_texts')
    details_text = fields.Text('Technical details', compute='_compute_texts')

    @api.depends('payload', 'result', 'error_details')
    def _compute_texts(self):
        for job in self:
            job.payload_text = json.dumps(job.payload, indent=2, sort_keys=True, default=str) if job.payload else False
            job.result_text = json.dumps(job.result, indent=2, sort_keys=True, default=str) if job.result else False
            job.details_text = json.dumps(job.error_details, indent=2, sort_keys=True, default=str) \
                if job.error_details else False

    def init(self):
        cr = self.env.cr
        cr.execute("""CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_job_dedup_uniq
                      ON eh_shopify_job (store_id, dedup_key)
                      WHERE dedup_key IS NOT NULL AND state IN ('pending', 'running')""")
        cr.execute("""CREATE INDEX IF NOT EXISTS eh_shopify_job_claim_idx
                      ON eh_shopify_job (priority, next_run_at, id) WHERE state = 'pending'""")
        cr.execute("""CREATE INDEX IF NOT EXISTS eh_shopify_job_resource_idx
                      ON eh_shopify_job (store_id, resource_key, id)
                      WHERE resource_key IS NOT NULL AND state IN ('pending', 'running')""")

    @api.depends('error_kind', 'last_error', 'state')
    def _compute_reason(self):
        for job in self:
            if job.state not in ('failed', 'dead', 'pending') or not job.error_kind:
                job.reason = False
                continue
            text = REASONS.get(job.error_kind, REASONS['unexpected'])
            if job.error_kind == 'user_errors' and job.last_error:
                text = '%s %s' % (text, job.last_error[:200])
            job.reason = text

    # ------------------------------------------------------------------ enqueue
    @api.model
    def _handlers(self):
        return dict(HANDLERS)

    @api.model
    def _enqueue(self, store, job_type, name=None, payload=None, dedup_key=None, resource_key=None, priority=10,
                 record=None, run_at=None, max_attempts=8):
        if job_type not in self._handlers():
            raise ValueError('Unknown Shopify job type %s' % job_type)
        Job = self.sudo()
        run_at = run_at or fields.Datetime.now()
        if dedup_key:
            existing = Job.search([('store_id', '=', store.id), ('dedup_key', '=', dedup_key),
                                   ('state', 'in', ('pending', 'running'))], limit=1)
            if existing:
                if existing.state == 'pending':
                    vals = {'next_run_at': min(existing.next_run_at, run_at)}
                    if payload is not None:
                        vals['payload'] = payload
                    existing.write(vals)
                return existing
        vals = {
            'name': name or job_type,
            'store_id': store.id,
            'job_type': job_type,
            'payload': payload,
            'dedup_key': dedup_key,
            'resource_key': resource_key,
            'priority': priority,
            'next_run_at': run_at,
            'max_attempts': max_attempts,
            'res_model': record._name if record else False,
            'res_id': record.id if record else False,
        }
        try:
            with self.env.cr.savepoint():
                job = Job.create(vals)
        except IntegrityError:
            return Job.search([('store_id', '=', store.id), ('dedup_key', '=', dedup_key),
                               ('state', 'in', ('pending', 'running'))], limit=1)
        self._trigger_runner()
        return job

    @api.model
    def _enqueue_after_running(self, store, job_type, dedup_key, **kwargs):
        """Queue work whose inputs just changed. A running job may have read
        them before the change, so instead of merging into it the work is
        queued under a second key and runs after it."""
        Job = self.sudo()
        for key in (dedup_key, dedup_key + ':next'):
            if not Job.search_count([('store_id', '=', store.id), ('dedup_key', '=', key), ('state', '=', 'running')]):
                return self._enqueue(store, job_type, dedup_key=key, **kwargs)
        return self._enqueue(store, job_type, dedup_key=dedup_key + ':next', **kwargs)

    @api.model
    def _trigger_runner(self):
        data = self.env.cr.precommit.data
        if data.get('eh_shopify.runner_triggered'):
            return
        data['eh_shopify.runner_triggered'] = True
        cron = self.env.ref('eh_shopify_connector.cron_eh_shopify_job_runner', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    # ------------------------------------------------------------------ runner
    @api.model
    def _cron_run_jobs(self, time_budget=RUN_BUDGET, limit=1000):
        self._schedule_side_queues()
        deadline = time.monotonic() + time_budget
        processed = 0
        while processed < limit and time.monotonic() < deadline:
            claimed = self._claim_next()
            if not claimed:
                break
            job_id, token = claimed
            self._execute_in_new_cursor(job_id, token)
            processed += 1
        return processed

    @api.model
    def _schedule_side_queues(self):
        """Turn queued stock changes into jobs, committed before any claim."""
        try:
            if compat.in_test_mode(self.env):
                return self.env['eh.shopify.store']._schedule_inventory_flushes()
            with self.env.registry.cursor() as cr:
                return self.env(cr=cr)['eh.shopify.store']._schedule_inventory_flushes()
        except Exception:  # noqa: BLE001
            _logger.exception('Could not schedule Shopify stock updates.')
            return 0

    @api.model
    def _claim_next(self, cr=None):
        own = cr is None
        cr = cr or self.env.registry.cursor()
        try:
            cr.execute(CLAIM_SQL)
            row = cr.fetchone()
            if not row:
                return None
            cr.execute("""UPDATE eh_shopify_job
                             SET state = 'running', attempts = attempts + 1,
                                 locked_at = (clock_timestamp() AT TIME ZONE 'UTC'),
                                 started_at = (clock_timestamp() AT TIME ZONE 'UTC'),
                                 write_date = (clock_timestamp() AT TIME ZONE 'UTC'),
                                 claim_token = md5(random()::text || clock_timestamp()::text)
                           WHERE id = %s
                       RETURNING claim_token""", (row[0],))
            token = cr.fetchone()[0]
            if own:
                cr.commit()
            return row[0], token
        finally:
            if own:
                cr.close()

    @api.model
    def _execute_in_new_cursor(self, job_id, claim_token=None):
        registry = self.env.registry
        started = time.monotonic()
        try:
            with registry.cursor() as cr:
                # Held until this transaction ends: proves to the stale reset
                # that the job is still being worked on.
                cr.execute('SELECT pg_advisory_xact_lock(%s, %s)', (LOCK_CLASS, job_id))
                env = api.Environment(cr, SUPERUSER_ID, {'eh_shopify_deadline': time.monotonic() + JOB_BUDGET})
                job = env['eh.shopify.job'].browse(job_id)
                result = job._invoke()
                job._mark_done(result, started, claim_token)
        except Exception as error:  # noqa: BLE001, every failure must be recorded
            trace = traceback.format_exc()
            _logger.info('Shopify job %s failed: %s', job_id, error)
            try:
                with registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env['eh.shopify.job'].browse(job_id)._mark_failed(error, started, trace, claim_token)
            except Exception:  # noqa: BLE001, the stale reset recovers the row later
                _logger.exception('Could not record the failure of Shopify job %s', job_id)

    def _run_inline(self):
        """Run pending jobs in the current transaction (manual Run now and tests)."""
        self.env.flush_all()
        blocked = []
        for job in self:
            self.env.cr.execute("SELECT id FROM eh_shopify_job WHERE id = %s AND state = 'pending' "
                                "FOR UPDATE SKIP LOCKED", (job.id,))
            if not self.env.cr.fetchone():
                continue
            blocker = job._blocking_predecessor()
            if blocker:
                blocked.append((job, blocker))
                continue
            job.invalidate_recordset()
            token = uuid.uuid4().hex
            job.write({'state': 'running', 'attempts': job.attempts + 1, 'claim_token': token,
                       'started_at': fields.Datetime.now(), 'locked_at': fields.Datetime.now()})
            started = time.monotonic()
            try:
                with self.env.cr.savepoint():
                    result = job._invoke()
            except Exception as error:  # noqa: BLE001
                trace = traceback.format_exc()
                self.env.invalidate_all()
                job._mark_failed(error, started, trace, token)
            else:
                job._mark_done(result, started, token)
        return blocked

    def _blocking_predecessor(self):
        """An older waiting job, or a running one, for the same Shopify resource."""
        self.ensure_one()
        if not self.resource_key:
            return False
        self.env.cr.execute("""SELECT id FROM eh_shopify_job
                                WHERE store_id = %s AND resource_key = %s AND id <> %s
                                  AND (state = 'running' OR (state = 'pending' AND id < %s))
                                ORDER BY id LIMIT 1""", (self.store_id.id, self.resource_key, self.id, self.id))
        row = self.env.cr.fetchone()
        return self.browse(row[0]) if row else False

    def _invoke(self):
        self.ensure_one()
        model_name, method = self._handlers()[self.job_type]
        company = self.store_id.company_id
        target = self.env[model_name].with_company(company)
        return getattr(target, method)(self.with_company(company))

    def _claim_is_current(self, claim_token):
        if claim_token and self.claim_token != claim_token:
            _logger.info('Shopify job %s was reset while it ran; its late result is ignored.', self.id)
            return False
        return True

    def _mark_done(self, result, started, claim_token=None):
        self.ensure_one()
        if not self._claim_is_current(claim_token):
            return False
        self.write({
            'state': 'done',
            'result': result if isinstance(result, (dict, list)) else ({'value': result} if result else None),
            'finished_at': fields.Datetime.now(),
            'locked_at': False,
            'duration_ms': int((time.monotonic() - started) * 1000),
            'error_kind': False,
            'last_error': False,
            'error_details': None,
            'claim_token': False,
        })

    def _classify_odoo_error(self, error):
        """Say what an Odoo-side refusal is, so the merchant reads the real message.

        These are not crashes: Odoo declined the work and said why. Retrying the same
        work eight times cannot change the answer, so only a lock clash is retryable.
        """
        if error.__class__.__name__ in ('LockError', 'ConcurrencyError'):
            return 'serialization', True
        if isinstance(error, MissingError):
            return 'record_gone', False
        if isinstance(error, (AccessError, AccessDenied)):
            return 'permission', False
        return 'mapping', False

    def _mark_failed(self, error, started, trace=None, claim_token=None):
        self.ensure_one()
        if not self._claim_is_current(claim_token):
            return False
        now = fields.Datetime.now()
        if isinstance(error, errors.ShopifyError):
            kind = error.kind
            retryable = error.retryable
            details = error.as_dict()
            if getattr(error, 'ambiguous', False):
                kind, retryable = 'ambiguous', False
        elif isinstance(error, TransactionRollbackError):
            kind, retryable, details = 'serialization', True, {}
        elif isinstance(error, (UserError, RedirectWarning)):
            kind, retryable = self._classify_odoo_error(error)
            details = {}
        else:
            kind, retryable, details = 'unexpected', True, {'traceback': (trace or '')[-6000:]}
        vals = {
            'error_kind': kind,
            'last_error': (getattr(error, 'message', None) or str(error) or error.__class__.__name__)[:2000],
            'error_details': details,
            'locked_at': False,
            'claim_token': False,
            'finished_at': now,
            'duration_ms': int((time.monotonic() - started) * 1000),
        }
        if retryable and self.attempts < self.max_attempts:
            vals['state'] = 'pending'
            vals['next_run_at'] = now + datetime.timedelta(seconds=self._retry_delay(error))
        elif retryable:
            vals['state'] = 'dead'
        else:
            vals['state'] = 'failed'
        self.write(vals)
        if vals['state'] in ('failed', 'dead'):
            self.env['eh.shopify.event'].sudo().search([
                ('job_id', '=', self.id), ('state', '=', 'queued')]).write(
                {'state': 'failed', 'note': self.last_error})
        if kind in ('auth', 'scope', 'version', 'shop_unavailable') and isinstance(error, errors.ShopifyError):
            self.store_id._record_failure(error)

    def _retry_delay(self, error):
        delay = min(BASE_DELAY * (2 ** max(0, self.attempts - 1)), MAX_DELAY)
        delay = max(delay, getattr(error, 'retry_after', None) or 0)
        return int(delay * (0.8 + 0.4 * random.random()))

    @api.model
    def _reset_stale(self):
        """Recover jobs whose worker died. A row is only touched when no
        transaction holds its advisory lock, attempts still count, retries back
        off, and a job that keeps killing its worker ends in the inbox."""
        if compat.in_test_mode(self.env):
            return self._reset_stale_in(self.env.cr)
        with self.env.registry.cursor() as cr:
            return self._reset_stale_in(cr)

    @api.model
    def _reset_stale_in(self, cr):
        cr.execute("""
            WITH stale AS (
                SELECT id, attempts, max_attempts
                  FROM eh_shopify_job
                 WHERE state = 'running'
                   AND locked_at < (now() AT TIME ZONE 'UTC') - make_interval(mins => %s)
                   AND pg_try_advisory_xact_lock(%s, id)
                   FOR UPDATE SKIP LOCKED
            )
            UPDATE eh_shopify_job j
               SET state = CASE WHEN s.attempts >= s.max_attempts THEN 'dead' ELSE 'pending' END,
                   locked_at = NULL,
                   claim_token = NULL,
                   finished_at = (now() AT TIME ZONE 'UTC'),
                   next_run_at = (now() AT TIME ZONE 'UTC')
                                 + make_interval(secs => LEAST(%s * power(2, GREATEST(s.attempts - 1, 0)), %s)),
                   error_kind = 'transient',
                   last_error = CASE WHEN s.attempts >= s.max_attempts
                                     THEN 'The worker stopped while this job ran, too many times. Retry it after '
                                          'checking the store.'
                                     ELSE 'The worker stopped while this job was running. It will run again.' END
              FROM stale s
             WHERE j.id = s.id
        """, (int(STALE_AFTER_MINUTES), LOCK_CLASS, BASE_DELAY, MAX_DELAY))
        return cr.rowcount

    @api.model
    def _cron_purge(self):
        days = self.env['ir.config_parameter'].sudo().get_int('eh_shopify.retention_days', 30)
        cutoff = fields.Datetime.now() - datetime.timedelta(days=max(days, 1))
        old_jobs = self.sudo().search([('state', 'in', ('done', 'cancelled')), '|', ('finished_at', '<', cutoff),
                                       '&', ('finished_at', '=', False), ('write_date', '<', cutoff)])
        old_events = self.env['eh.shopify.event'].sudo().search([
            ('state', 'in', ('processed', 'ignored')), ('received_at', '<', cutoff)])
        counts = {'jobs': len(old_jobs), 'events': len(old_events)}
        old_events.unlink()
        old_jobs.unlink()
        # What stays for a person to look at keeps its error, not the customer data it carried.
        kept_jobs = self.sudo().search([('state', 'in', ('failed', 'dead')), ('write_date', '<', cutoff)]).filtered(
            lambda job: job.payload)
        kept_events = self.env['eh.shopify.event'].sudo().search([
            ('state', 'in', ('queued', 'failed')), ('received_at', '<', cutoff)]).filtered(lambda event: event.payload)
        kept_jobs.write({'payload': {'purged': True}})
        kept_events.write({'payload': {'purged': True}})
        counts['payloads'] = len(kept_jobs) + len(kept_events)
        return counts

    # ------------------------------------------------------------------ user actions
    @staticmethod
    def _payload_was_cleared(job):
        return (job.payload or {}).get('purged') is True

    def action_retry(self):
        compat.check_access(self, 'read')
        cleared = self.filtered(self._payload_was_cleared)
        if cleared:
            raise UserError(_('Sync job %s cannot run again: what it needed was cleared when its personal data was '
                              'removed. Start the work again from the store.') % cleared[0].id)
        if self.filtered(lambda job: job.state == 'cancelled'):
            # A manager cancelled it, so only a manager brings it back.
            self._require_manager()
        now = fields.Datetime.now()
        jobs = self.sudo().filtered(lambda j: j.state in ('failed', 'dead', 'cancelled'))
        for job in jobs:
            if job.dedup_key:
                live = self.sudo().search([('store_id', '=', job.store_id.id), ('dedup_key', '=', job.dedup_key),
                                           ('state', 'in', ('pending', 'running')), ('id', '!=', job.id)], limit=1)
                if live:
                    if live.state == 'pending':
                        live.next_run_at = now
                    job.write({'state': 'cancelled', 'finished_at': now,
                               'last_error': _('Replaced by sync job %s, which does the same work.') % live.id})
                    continue
            try:
                with self.env.cr.savepoint():
                    job.write({'state': 'pending', 'next_run_at': now, 'finished_at': False,
                               'max_attempts': max(job.max_attempts, job.attempts + 3)})
            except IntegrityError:
                job.invalidate_recordset()
        if jobs:
            self._trigger_runner()
        return True

    @api.model
    def _blocked_by_failure(self, store, dedup_key, retry_after=None):
        """True while the latest attempt at this work failed for good, so the
        scheduler does not keep re-creating a job that cannot succeed. With
        ``retry_after``, work that catches up by itself, such as polling, is
        tried again once that long has passed since the failure."""
        Job = self.sudo()
        failed = Job.search([('store_id', '=', store.id), ('dedup_key', '=', dedup_key),
                             ('state', 'in', ('failed', 'dead'))], order='id desc', limit=1)
        if not failed:
            return False
        if retry_after and (failed.finished_at or failed.write_date) < fields.Datetime.now() - retry_after:
            return False
        done = Job.search([('store_id', '=', store.id), ('dedup_key', '=', dedup_key), ('state', '=', 'done')],
                          order='id desc', limit=1)
        return not done or done.id < failed.id

    def _close_cancelled(self):
        now = fields.Datetime.now()
        self.write({'state': 'cancelled', 'finished_at': now, 'claim_token': False})
        self.env['eh.shopify.event'].sudo().search([('job_id', 'in', self.ids), ('state', '=', 'queued')]).write(
            {'state': 'ignored', 'note': _('The sync job for this delivery was cancelled.')})

    def _require_manager(self):
        if not self.env.su and not self.env.user.has_group('eh_shopify_connector.group_shopify_manager'):
            raise AccessError(_('Only a Shopify manager can run or cancel sync jobs.'))

    def action_cancel(self):
        compat.check_access(self, 'read')
        self._require_manager()
        self.sudo().filtered(lambda j: j.state in ('pending', 'failed', 'dead'))._close_cancelled()
        return True

    def action_run_now(self):
        compat.check_access(self, 'read')
        self._require_manager()
        cleared = self.filtered(self._payload_was_cleared)
        if cleared:
            raise UserError(_('Sync job %s cannot run again: what it needed was cleared when its personal data was '
                              'removed. Start the work again from the store.') % cleared[0].id)
        for job in self.sudo():
            blocker = job._blocking_predecessor()
            if blocker:
                raise UserError(_('Sync job %s must wait for sync job %s, which works on the same Shopify record.')
                                % (job.id, blocker.id))
        self.filtered(lambda j: j.state in ('failed', 'dead')).action_retry()
        self.sudo().filtered(lambda j: j.state == 'pending')._run_inline()
        return True

    def action_open_record(self):
        self.ensure_one()
        if not (self.res_model and self.res_id):
            return False
        return {'type': 'ir.actions.act_window', 'res_model': self.res_model, 'res_id': self.res_id,
                'view_mode': 'form'}
