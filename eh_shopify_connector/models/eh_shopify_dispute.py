# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify Payments chargebacks and inquiries.

Disputes arrive by webhook and are also read on a schedule, so a missed
webhook never hides one. A dispute that needs a response puts a to-do on the
Odoo sales order, due two days before Shopify's evidence deadline, so the team
answers in Shopify in time. When the dispute is won, lost or accepted, the
to-do is closed with the outcome. The money a dispute withholds or returns
moves through the payout transactions, so nothing is posted here twice.
"""
import datetime

from odoo import _, api, fields, models

from ..lib import documents, order_math

STATES = {'NEEDS_RESPONSE': 'open', 'UNDER_REVIEW': 'review', 'WON': 'won', 'LOST': 'lost', 'ACCEPTED': 'accepted'}
POLL_WINDOW = datetime.timedelta(days=120)
POLL_EVERY = datetime.timedelta(hours=6)
ANSWER_BEFORE = datetime.timedelta(days=2)
SCOPE = 'read_shopify_payments_disputes'


def _parse_dt(value):
    if not value:
        return False
    try:
        return datetime.datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except (TypeError, ValueError):
        return False


def _parse_date(value):
    """Dates on 2026-01, date times from 2026-04."""
    if not value:
        return False
    try:
        return datetime.datetime.strptime(value[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return False


class ShopifyStoreDisputes(models.Model):
    _inherit = 'eh.shopify.store'

    disputes_polled_at = fields.Datetime('Disputes last checked', readonly=True, copy=False)

    def _reads_disputes(self):
        self.ensure_one()
        return SCOPE in self._granted_scope_set()


class ShopifyDispute(models.Model):
    _name = 'eh.shopify.dispute'
    _description = 'Shopify Dispute'
    _order = 'initiated_at desc, id desc'
    _rec_name = 'name'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    legacy_id = fields.Char('Shopify number', readonly=True)
    name = fields.Char(readonly=True)
    order_binding_id = fields.Many2one('eh.shopify.order', 'Shopify order', ondelete='set null', readonly=True)
    sale_order_id = fields.Many2one('sale.order', 'Sales order', related='order_binding_id.sale_order_id')
    order_name = fields.Char('Order', readonly=True)
    status = fields.Char('Status in Shopify', readonly=True)
    dispute_type = fields.Char('Type', readonly=True)
    reason = fields.Char(readonly=True)
    network_reason_code = fields.Char('Card network code', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    amount = fields.Monetary(currency_field='currency_id', readonly=True)
    initiated_at = fields.Datetime('Opened', readonly=True)
    evidence_due_by = fields.Date('Answer by', readonly=True)
    evidence_sent_on = fields.Date('Answered on', readonly=True)
    finalized_on = fields.Date('Decided on', readonly=True)
    state = fields.Selection([
        ('open', 'Needs a response'),
        ('review', 'Under review'),
        ('won', 'Won'),
        ('lost', 'Lost'),
        ('accepted', 'Accepted'),
        ('closed', 'Closed'),
    ], readonly=True, index=True)
    activity_id = fields.Many2one('mail.activity', 'To-do', ondelete='set null', readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_dispute_gid_uniq "
                            "ON eh_shopify_dispute (store_id, shopify_gid)")

    # ------------------------------------------------------------------ jobs
    @api.model
    def _enqueue_sync(self, store, gid):
        return self.env['eh.shopify.job'].sudo()._enqueue_after_running(
            store, 'dispute.sync', 'dispute:%s' % gid, name=_('Sync dispute %s') % gid.rsplit('/', 1)[-1],
            payload={'gid': gid}, resource_key=gid, priority=4)

    def _job_dispute_sync(self, job):
        store = job.store_id
        gid = (job.payload or {}).get('gid')
        data = store._client().execute(store._doc_text(documents.DISPUTE), {'id': gid}).data or {}
        node = data.get('dispute')
        if not node:
            return {'missing': gid}
        dispute = self._upsert(store, node)
        return {'state': dispute.state}

    def _job_disputes_poll(self, job):
        store = job.store_id
        if not store._reads_disputes():
            return {'skipped': 'missing permission'}
        now = fields.Datetime.now()
        document = documents.DISPUTES
        seen = 0
        for nodes, _cursor, _more in store._client().iter_pages(
                store._doc_text(document), {'query': "initiated_at:>='%s'" % (now - POLL_WINDOW).strftime('%Y-%m-%d')},
                document.connection, page_size=document.page_size):
            for node in nodes:
                self._upsert(store, node)
                seen += 1
        store.sudo().disputes_polled_at = now
        return {'disputes': seen}

    # ------------------------------------------------------------------ records
    @api.model
    def _upsert(self, store, node):
        amount = node.get('amount') or {}
        order = node.get('order') or {}
        details = node.get('reasonDetails') or {}
        currency = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', amount.get('currencyCode'))], limit=1)
        order_binding = self.env['eh.shopify.order'].search(
            [('store_id', '=', store.id), ('shopify_gid', '=', order.get('id'))], limit=1) if order.get('id') else False
        legacy = str(node.get('legacyResourceId') or node['id'].rsplit('/', 1)[-1])
        kind = (node.get('type') or 'CHARGEBACK').lower()
        vals = {'store_id': store.id, 'shopify_gid': node['id'], 'legacy_id': legacy,
                'name': _('%(kind)s %(number)s') % {'kind': kind.capitalize(), 'number': legacy},
                'order_binding_id': order_binding.id if order_binding else False, 'order_name': order.get('name'),
                'status': node.get('status'), 'dispute_type': node.get('type'),
                'reason': (details.get('reason') or '').replace('_', ' ').lower() or False,
                'network_reason_code': details.get('networkReasonCode') or False,
                'currency_id': (currency or store.company_id.currency_id).id,
                'amount': float(order_math.dec(amount.get('amount'))),
                'initiated_at': _parse_dt(node.get('initiatedAt')),
                'evidence_due_by': _parse_date(node.get('evidenceDueBy')),
                'evidence_sent_on': _parse_date(node.get('evidenceSentOn')),
                'finalized_on': _parse_date(node.get('finalizedOn')),
                'state': STATES.get(node.get('status'), 'closed')}
        dispute = self.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1)
        if dispute:
            dispute.write(vals)
        else:
            dispute = self.create(vals)
        dispute._follow_up()
        return dispute

    def _follow_up(self):
        """A to-do on the sales order while Shopify waits for an answer, closed with the outcome."""
        for dispute in self:
            sale_order = dispute.order_binding_id.sale_order_id.sudo()
            if dispute.state == 'open':
                if sale_order and not dispute.activity_id:
                    today = fields.Date.context_today(dispute)
                    deadline = max(today, (dispute.evidence_due_by or today) - ANSWER_BEFORE)
                    user = sale_order.user_id or dispute.store_id.order_user_id or self.env.ref('base.user_admin')
                    note = _('Shopify Payments opened a %(kind)s of %(amount)s %(currency)s (%(reason)s). Submit '
                             'evidence in Shopify before %(due)s.') % {
                        'kind': (dispute.dispute_type or 'chargeback').lower(), 'amount': dispute.amount,
                        'currency': dispute.currency_id.name, 'reason': dispute.reason or _('no reason given'),
                        'due': dispute.evidence_due_by or _('the deadline Shopify shows')}
                    dispute.activity_id = sale_order.activity_schedule(
                        'mail.mail_activity_data_todo', date_deadline=deadline, summary=_('Answer the Shopify chargeback'),
                        note=note, user_id=user.id)
            elif dispute.activity_id and dispute.state in ('won', 'lost', 'accepted', 'closed'):
                outcome = dict(self._fields['state'].selection).get(dispute.state)
                dispute.activity_id.sudo().action_feedback(feedback=_('Shopify dispute outcome: %s') % outcome)
                # 19 archives a done activity instead of deleting it.
                dispute.activity_id = False
