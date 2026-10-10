# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify Payments payouts in Odoo accounting.

Shopify has no payout webhook, so payouts are read on a schedule. When a payout
is paid, its balance transactions are read and posted as one journal entry on
the payout journal: each charge and refund settles the outstanding line of the
Odoo payment it belongs to and is reconciled with it, fees go to the fee
account, disputes, reserves and adjustments to the adjustment account, and the
net amount waits on a transit account for the bank deposit. The payment is
found by the Shopify transaction id, never by amount, and the transactions
must add up to the payout before anything is posted. A payout whose payments
are not in Odoo yet waits for them for a few days, then posts the missing ones
on the suspense account, or earlier when a manager chooses Post now. A bank
statement line of the net amount is matched to the transit line when exactly
one fits. A payout that fails after it was posted is reversed and its payments
are open again. Only Community accounting is used.
"""
import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .. import compat
from ..lib import documents, errors, order_math, util

FAILED = ('FAILED', 'CANCELED')
DISPUTE_TYPES = {'DISPUTE_WITHDRAWAL', 'DISPUTE_REVERSAL'}
RESERVE_TYPES = {'RESERVED_FUNDS', 'RESERVED_FUNDS_WITHDRAWAL', 'RESERVED_FUNDS_REVERSAL', 'RISK_WITHDRAWAL',
                 'RISK_REVERSAL'}
FIRST_WINDOW = datetime.timedelta(days=30)
OVERLAP = datetime.timedelta(days=7)
POLL_EVERY = datetime.timedelta(hours=6)
DEPOSIT_BEFORE = datetime.timedelta(days=3)
DEPOSIT_AFTER = datetime.timedelta(days=10)
FEE_KEYS = ('chargesFee', 'refundsFee', 'adjustmentsFee', 'reservedFundsFee', 'retriedPayoutsFee', 'advanceFees')


def _parse_dt(value):
    if not value:
        return False
    try:
        return datetime.datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except (TypeError, ValueError):
        return False


def _money(bag):
    return float(order_math.dec((bag or {}).get('amount')))


def _family(kind):
    kind = kind or ''
    if kind == 'CHARGE':
        return 'payment'
    if kind == 'REFUND':
        return 'refund'
    if kind in DISPUTE_TYPES or kind.startswith('CHARGEBACK'):
        return 'dispute'
    if kind in RESERVE_TYPES:
        return 'reserve'
    return 'adjustment'


class ShopifyStorePayouts(models.Model):
    _inherit = 'eh.shopify.store'

    payouts_enabled = fields.Boolean(
        'Reconcile Shopify Payments payouts', default=False,
        help='Post each paid payout as one journal entry that settles the Odoo payments it contains, books the fees '
             'and waits for the bank deposit on a transit account.')
    payouts_start_date = fields.Date('Reconcile payouts issued from', help='Older payouts stay in Shopify only.')
    payout_wait_days = fields.Integer('Wait for missing payments (days)', default=7,
                                      help='A paid payout waits this long for payments not yet in Odoo, then posts '
                                           'them on the suspense account.')
    payout_journal_id = fields.Many2one('account.journal', 'Payout journal', check_company=True,
                                        domain="[('type', '=', 'general')]")
    payout_transit_account_id = fields.Many2one('account.account', 'Payouts in transit',
                                                help='Where the net payout waits for the bank deposit.')
    payout_fee_account_id = fields.Many2one('account.account', 'Payment fees')
    payout_adjustment_account_id = fields.Many2one('account.account', 'Disputes, reserves and adjustments')
    payout_suspense_account_id = fields.Many2one('account.account', 'Payments not found in Odoo')
    payouts_polled_at = fields.Datetime('Payouts last checked', readonly=True, copy=False)
    payout_note = fields.Char(readonly=True, copy=False)
    payout_count = fields.Integer(compute='_compute_payout_count')

    def _compute_payout_count(self):
        Payout = self.env['eh.shopify.payout']
        for store in self:
            store.payout_count = Payout.search_count([('store_id', '=', store.id)]) if store.id else 0

    def action_open_payouts(self):
        self.ensure_one()
        return self._open('eh.shopify.payout', _('Payouts'), [('store_id', '=', self.id)])

    def action_sync_payouts(self):
        self.ensure_one()
        compat.check_access(self, 'write')
        self.env['eh.shopify.job'].sudo()._enqueue(self, 'payouts.poll', name=_('Check Shopify Payments payouts'),
                                                   dedup_key='payouts.poll', priority=6)
        return self._notify(_('Payout check started'), _('Paid payouts are posted as they are read.'))

    # ------------------------------------------------------------------ setup
    def _payout_account(self, prefix, name, account_type, reconcile):
        company = self.company_id
        Account = self.env['account.account'].sudo().with_company(company)
        domain = compat.account_company_domain(self.env, company)
        existing = Account.search(domain + [('name', '=', name)], limit=1)
        if existing:
            return existing
        for index in range(1, 100):
            code = '%s%02d' % (prefix, index)
            if not Account.search_count(domain + [('code', '=', code)]):
                break
        return Account.create(compat.account_create_vals(self.env, company, {
            'code': code, 'name': name, 'account_type': account_type, 'reconcile': reconcile}))

    def _payout_setup(self):
        """Journal and accounts for payouts; the ones not chosen are created once."""
        self.ensure_one()
        company = self.company_id
        vals = {}
        if not self.payout_journal_id:
            journal = self.env['account.journal'].sudo().search([('type', '=', 'general'),
                                                                 ('company_id', '=', company.id)], limit=1)
            if not journal:
                raise errors.MappingError(_('Create a miscellaneous journal for %s to post payouts.') % company.name)
            vals['payout_journal_id'] = journal.id
        if not self.payout_transit_account_id:
            vals['payout_transit_account_id'] = self._payout_account(
                '1019', _('Shopify payouts in transit'), 'asset_current', True).id
        if not self.payout_fee_account_id:
            vals['payout_fee_account_id'] = self._payout_account(
                '6199', _('Shopify Payments fees'), 'expense', False).id
        if not self.payout_adjustment_account_id:
            vals['payout_adjustment_account_id'] = self._payout_account(
                '1018', _('Shopify Payments adjustments'), 'asset_current', False).id
        if not self.payout_suspense_account_id:
            suspense = company.account_journal_suspense_account_id
            vals['payout_suspense_account_id'] = (suspense or self._payout_account(
                '1017', _('Shopify payments not found in Odoo'), 'asset_current', True)).id
        if vals:
            self.sudo().write(vals)

    # ------------------------------------------------------------------ poll
    def _job_payouts_poll(self, job):
        store = job.store_id
        if not store.payouts_enabled:
            return {'skipped': 'payout reconciliation is switched off'}
        if store.granted_scopes and 'read_shopify_payments_payouts' not in store._granted_scope_set():
            store.sudo().payout_note = _('Add the read_shopify_payments_payouts permission to reconcile payouts.')
            return {'skipped': 'missing permission'}
        Payout = self.env['eh.shopify.payout']
        now = fields.Datetime.now()
        waiting = Payout.search([('store_id', '=', store.id), ('state', 'in', ('waiting', 'unmatched', 'error'))],
                                order='issued_at', limit=1)
        marks = [value for value in (waiting.issued_at, store.payouts_polled_at) if value]
        since = min(marks) - OVERLAP if marks else now - FIRST_WINDOW
        if store.payouts_start_date:
            since = max(since, datetime.datetime.combine(store.payouts_start_date, datetime.time.min))
        document = documents.PAYOUTS
        client = store._client()
        seen, queued = 0, 0
        try:
            for nodes, _cursor, _more in client.iter_pages(store._doc_text(document),
                                                           {'query': "issued_at:>='%s'" % since.strftime('%Y-%m-%d')},
                                                           document.connection, page_size=document.page_size):
                for node in nodes:
                    seen += 1
                    payout = Payout._upsert(store, node)
                    if payout._needs_posting():
                        payout._enqueue_post()
                        queued += 1
        except errors.NotFound:
            store.sudo().write({'payout_note': _('This store does not use Shopify Payments, so there are no payouts '
                                                 'to reconcile.'), 'payouts_polled_at': now})
            return {'skipped': 'no Shopify Payments account'}
        Payout.search([('store_id', '=', store.id), ('state', '=', 'posted')])._match_deposit()
        store.sudo().write({'payouts_polled_at': now, 'payout_note': False})
        return {'payouts': seen, 'queued': queued}


class ShopifyPayout(models.Model):
    _name = 'eh.shopify.payout'
    _description = 'Shopify Payout'
    _order = 'issued_at desc, id desc'
    _rec_name = 'name'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    legacy_id = fields.Char('Shopify number', readonly=True, index=True)
    name = fields.Char(readonly=True)
    status = fields.Char('Status code in Shopify', readonly=True)
    issued_at = fields.Datetime('Issued', readonly=True)
    transaction_type = fields.Char('Direction code', readonly=True)
    status_label = fields.Char('Status in Shopify', compute='_compute_readable_labels')
    transaction_type_label = fields.Char('Direction', compute='_compute_readable_labels')
    external_trace_id = fields.Char('Bank reference', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    amount_net = fields.Monetary('Net payout', currency_field='currency_id', readonly=True)
    charges_gross = fields.Monetary('Charges', currency_field='currency_id', readonly=True)
    refunds_gross = fields.Monetary('Refunds', currency_field='currency_id', readonly=True)
    adjustments_gross = fields.Monetary('Adjustments', currency_field='currency_id', readonly=True)
    fees_total = fields.Monetary('Fees', currency_field='currency_id', readonly=True)
    line_ids = fields.One2many('eh.shopify.payout.line', 'payout_id', readonly=True)
    move_id = fields.Many2one('account.move', 'Journal entry', readonly=True, copy=False)
    transit_line_id = fields.Many2one('account.move.line', readonly=True, copy=False)
    reversal_move_id = fields.Many2one('account.move', 'Reversal', readonly=True, copy=False)
    deposit_line_id = fields.Many2one('account.bank.statement.line', 'Bank deposit', readonly=True, copy=False)
    state = fields.Selection([
        ('waiting', 'Waiting for Shopify'),
        ('unmatched', 'Waiting for payments'),
        ('posted', 'Posted'),
        ('deposited', 'Matched to the bank'),
        ('reversed', 'Failed in Shopify'),
        ('error', 'Needs attention'),
    ], default='waiting', required=True, readonly=True, index=True)
    reason = fields.Char(readonly=True)

    @api.depends('status', 'transaction_type')
    def _compute_readable_labels(self):
        for payout in self:
            payout.status_label = util.readable_enum(payout.status)
            payout.transaction_type_label = util.readable_enum(payout.transaction_type)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_payout_gid_uniq "
                            "ON eh_shopify_payout (store_id, shopify_gid)")

    # ------------------------------------------------------------------ records
    @api.model
    def _upsert(self, store, node):
        net = node.get('net') or {}
        summary = node.get('summary') or {}
        currency = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', net.get('currencyCode'))], limit=1)
        legacy = str(node.get('legacyResourceId') or node['id'].rsplit('/', 1)[-1])
        issued = _parse_dt(node.get('issuedAt'))
        vals = {'store_id': store.id, 'shopify_gid': node['id'], 'legacy_id': legacy, 'status': node.get('status'),
                'name': _('Payout %(number)s of %(date)s') % {'number': legacy,
                                                                'date': issued.date() if issued else ''},
                'issued_at': issued, 'transaction_type': node.get('transactionType'),
                'external_trace_id': node.get('externalTraceId') or False,
                'currency_id': (currency or store.company_id.currency_id).id, 'amount_net': _money(net),
                'charges_gross': _money(summary.get('chargesGross')),
                'refunds_gross': _money(summary.get('refundsFeeGross')),
                'adjustments_gross': _money(summary.get('adjustmentsGross')),
                'fees_total': sum(_money(summary.get(key)) for key in FEE_KEYS)}
        payout = self.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1)
        if payout:
            payout.write(vals)
        else:
            payout = self.create(vals)
        return payout

    def _needs_posting(self):
        self.ensure_one()
        if self.status == 'PAID':
            return self.state in ('waiting', 'unmatched', 'error')
        return self.status in FAILED and self.state in ('posted', 'deposited', 'waiting', 'unmatched')

    def _enqueue_post(self, force=False):
        self.ensure_one()
        return self.env['eh.shopify.job'].sudo()._enqueue_after_running(
            self.store_id, 'payout.post', 'payout:%s' % self.id, name=_('Post Shopify payout %s') % self.legacy_id,
            payload={'payout_id': self.id, 'force': force}, resource_key=self.shopify_gid, priority=6, record=self)

    def action_post_now(self):
        self.env['eh.shopify.job']._require_manager()
        for payout in self.sudo().filtered(lambda record: record.state in ('unmatched', 'error')):
            payout._enqueue_post(force=True)
        return True

    def action_check_again(self):
        self.env['eh.shopify.job']._require_manager()
        for payout in self.sudo().filtered(lambda record: record.state in ('waiting', 'unmatched', 'error', 'posted')):
            if payout.state == 'posted':
                payout._match_deposit()
            else:
                payout._enqueue_post()
        return True

    # ------------------------------------------------------------------ job
    def _job_payout_post(self, job):
        payout = self.browse((job.payload or {}).get('payout_id')).exists()
        if not payout:
            return {'skipped': 'the payout was removed'}
        if payout.status in FAILED:
            return payout._reverse()
        if payout.status != 'PAID':
            return {'skipped': 'Shopify has not paid this payout yet'}
        if payout.move_id:
            return {'posted': payout.move_id.id}
        store = payout.store_id
        payout._read_lines()
        total = sum(payout.line_ids.mapped('net'))
        if abs(abs(total) - abs(payout.amount_net)) > 0.01:
            payout.write({'state': 'error', 'reason': _(
                'The balance transactions of this payout add up to %(lines)s but Shopify paid %(payout)s, so nothing '
                'was posted.') % {'lines': round(total, 2), 'payout': round(payout.amount_net, 2)}})
            return {'error': payout.legacy_id}
        payout._match_lines()
        missing = payout.line_ids.filtered(lambda line: line.match_state == 'unmatched')
        waited = fields.Datetime.now() - (payout.issued_at or fields.Datetime.now())
        if missing and not (job.payload or {}).get('force') and waited < datetime.timedelta(days=store.payout_wait_days):
            payout.write({'state': 'unmatched', 'reason': _(
                '%s payments of this payout are not in Odoo yet. The payout is posted when they arrive, or choose Post '
                'now to post them on the suspense account.') % len(missing)})
            return {'unmatched': len(missing)}
        return payout._post()

    def _read_lines(self):
        self.ensure_one()
        store = self.store_id
        document = documents.PAYOUT_TRANSACTIONS
        Line = self.env['eh.shopify.payout.line']
        existing = {line.shopify_gid: line for line in self.line_ids}
        pages = store._client().iter_pages(store._doc_text(document), {'query': 'payments_transfer_id:%s' % self.legacy_id},
                                           document.connection, page_size=document.page_size)
        for nodes, _cursor, _more in pages:
            for node in nodes:
                if node.get('type') == 'TRANSFER':
                    continue
                order = node.get('associatedOrder') or {}
                vals = {'payout_id': self.id, 'shopify_gid': node['id'], 'transaction_type': node.get('type'),
                        'source_type': node.get('sourceType'), 'family': _family(node.get('type')),
                        'source_order_transaction_id': str(node['sourceOrderTransactionId'])
                        if node.get('sourceOrderTransactionId') else False,
                        'order_name': order.get('name') or False, 'amount': _money(node.get('amount')),
                        'fee': _money(node.get('fee')), 'net': _money(node.get('net')),
                        'transaction_date': _parse_dt(node.get('transactionDate')),
                        'adjustment_reason': node.get('adjustmentReason') or False}
                if order.get('id'):
                    vals['order_binding_id'] = self.env['eh.shopify.order'].search(
                        [('store_id', '=', store.id), ('shopify_gid', '=', order['id'])], limit=1).id or False
                if node['id'] in existing:
                    existing[node['id']].write(vals)
                else:
                    Line.create(vals)

    def _match_lines(self):
        Transaction = self.env['eh.shopify.transaction']
        for line in self.line_ids:
            if line.family not in ('payment', 'refund'):
                line.match_state = 'account'
                continue
            transaction = Transaction.search([('store_id', '=', self.store_id.id), ('shopify_gid', '=',
                                              'gid://shopify/OrderTransaction/%s' % line.source_order_transaction_id)],
                                             limit=1) if line.source_order_transaction_id else Transaction
            outstanding = compat.payment_outstanding_lines(transaction.payment_id) if transaction.payment_id else False
            line.write({'transaction_id': transaction.id or False, 'payment_id': transaction.payment_id.id or False,
                        'order_binding_id': transaction.order_binding_id.id or line.order_binding_id.id or False,
                        'match_state': 'matched' if outstanding else 'unmatched'})

    def _post(self):
        self.ensure_one()
        store = self.store_id
        store._payout_setup()
        company = store.company_id
        company_currency = company.currency_id
        currency = self.currency_id or company_currency
        date = (self.issued_at or fields.Datetime.now()).date()
        Line = self.env['account.move.line']

        def amounts(value):
            balance = currency._convert(value, company_currency, company, date) if currency != company_currency else value
            return {'currency_id': currency.id, 'amount_currency': currency.round(value),
                    'balance': company_currency.round(balance)}

        vals_list, plan, suspense = [], [], 0
        for line in self.line_ids:
            partner = line.order_binding_id.sale_order_id.partner_id
            label = ' '.join(filter(None, [(line.transaction_type or '').replace('_', ' ').capitalize(), line.order_name]))
            outstanding = Line
            if line.match_state == 'matched':
                outstanding = compat.payment_outstanding_lines(line.payment_id)[:1]
            if outstanding:
                account, partner = outstanding.account_id, outstanding.partner_id or partner
            elif line.family in ('payment', 'refund'):
                account = store.payout_suspense_account_id
                suspense += 1
            else:
                account = store.payout_adjustment_account_id
            if line.amount:
                vals_list.append(dict(amounts(-line.amount), name=label or line.shopify_gid, account_id=account.id,
                                      partner_id=partner.id or False))
                plan.append((line, outstanding))
            if line.fee:
                vals_list.append(dict(amounts(line.fee), name=_('Shopify Payments fee %s') % (line.order_name or ''),
                                      account_id=store.payout_fee_account_id.id))
                plan.append((None, Line))
        if not vals_list:
            self.write({'state': 'error', 'reason': _('Shopify listed no transactions for this payout.')})
            return {'error': self.legacy_id}
        vals_list.append({'name': _('Net of %s') % self.name, 'account_id': store.payout_transit_account_id.id,
                          'currency_id': currency.id,
                          'amount_currency': currency.round(-sum(vals['amount_currency'] for vals in vals_list)),
                          'balance': company_currency.round(-sum(vals['balance'] for vals in vals_list))})
        move = self.env['account.move'].sudo().with_company(company).create({
            'move_type': 'entry', 'journal_id': store.payout_journal_id.id, 'date': date,
            'ref': ' '.join(filter(None, [self.name, self.external_trace_id])),
            'line_ids': [(0, 0, vals) for vals in vals_list]})
        created = move.line_ids.sorted('id')
        move.action_post()
        for (line, outstanding), move_line in zip(plan, created):
            if not line:
                continue
            line.move_line_id = move_line
            if outstanding and not outstanding.reconciled and move_line.parent_state == 'posted':
                (move_line + outstanding).reconcile()
        reason = _('%s payments were not found in Odoo and wait on the suspense account.') % suspense if suspense else False
        self.write({'move_id': move.id, 'transit_line_id': created[-1].id, 'state': 'posted', 'reason': reason})
        self._match_deposit()
        return {'posted': move.id}

    def _reverse(self):
        self.ensure_one()
        status = (self.status or '').lower()
        move = self.move_id
        if not move or move.state != 'posted' or self.reversal_move_id:
            self.write({'state': 'reversed', 'reason': _('Shopify marked this payout %s.') % status})
            return {'reversed': self.legacy_id}
        move.line_ids.filtered(lambda line: line.reconciled or line.matched_debit_ids or line.matched_credit_ids
                               ).remove_move_reconcile()
        reversal = move._reverse_moves([{'date': fields.Date.context_today(self),
                                         'ref': _('Reversal of %s') % self.name}], cancel=False)
        if reversal.state == 'draft':
            reversal.action_post()
        for original in move.line_ids.filtered(lambda line: line.account_id.reconcile and not line.reconciled):
            counterpart = reversal.line_ids.filtered(
                lambda line, original=original: line.account_id == original.account_id and not line.reconciled
                and line.currency_id.is_zero(line.amount_currency + original.amount_currency))[:1]
            if counterpart:
                (original + counterpart).reconcile()
        self.write({'reversal_move_id': reversal.id, 'state': 'reversed', 'deposit_line_id': False, 'reason': _(
            'Shopify marked this payout %s after it was posted, so the entry was reversed and its payments are open '
            'again.') % status})
        return {'reversed': self.legacy_id}

    def _match_deposit(self):
        """Match the transit line to a bank statement line of the same amount when exactly one fits."""
        StatementLine = self.env['account.bank.statement.line'].sudo()
        now = fields.Datetime.now()
        for payout in self.filtered(lambda record: record.state == 'posted' and record.transit_line_id):
            transit = payout.transit_line_id
            if transit.reconciled:
                continue
            company = payout.company_id
            currency = payout.currency_id or company.currency_id
            # A deposit arrives in the payout currency: on a bank journal in that currency, compared in that currency.
            target = transit.balance if currency == company.currency_id else transit.amount_currency
            issued = payout.issued_at or now
            candidates = StatementLine.search([
                ('company_id', '=', company.id), ('is_reconciled', '=', False),
                ('date', '>=', (issued - DEPOSIT_BEFORE).date()), ('date', '<=', (issued + DEPOSIT_AFTER).date())])
            candidates = candidates.filtered(
                lambda line: line.journal_id.type == 'bank' and not line.foreign_currency_id
                and (line.journal_id.currency_id or company.currency_id) == currency
                and currency.compare_amounts(line.amount, target) == 0)
            if payout.external_trace_id:
                traced = candidates.filtered(lambda line: payout.external_trace_id in (line.payment_ref or ''))
                if len(traced) == 1:
                    candidates = traced
            if len(candidates) != 1:
                continue
            if currency != company.currency_id and not company.currency_exchange_journal_id:
                payout.reason = _('Choose an exchange gain or loss journal on the company so the %s deposit can be '
                                  'matched.') % currency.name
                continue
            try:
                with self.env.cr.savepoint():
                    matched = compat.match_bank_deposit(candidates, transit)
            except (UserError, ValidationError) as error:
                payout.reason = _('The bank deposit could not be matched: %s') % error
                continue
            if matched:
                payout.write({'state': 'deposited', 'deposit_line_id': candidates.id})


class ShopifyPayoutLine(models.Model):
    _name = 'eh.shopify.payout.line'
    _description = 'Shopify Payout Transaction'
    _order = 'transaction_date, id'
    _rec_name = 'shopify_gid'

    payout_id = fields.Many2one('eh.shopify.payout', required=True, ondelete='cascade', index=True)
    store_id = fields.Many2one('eh.shopify.store', related='payout_id.store_id', store=True, index=True)
    company_id = fields.Many2one('res.company', related='payout_id.company_id', store=True, index=True)
    currency_id = fields.Many2one('res.currency', related='payout_id.currency_id')
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    transaction_type = fields.Char('Type code', readonly=True)
    transaction_type_label = fields.Char('Type', compute='_compute_readable_type')
    source_type = fields.Char(readonly=True)
    family = fields.Selection([
        ('payment', 'Customer payment'),
        ('refund', 'Refund'),
        ('dispute', 'Dispute'),
        ('reserve', 'Reserve'),
        ('adjustment', 'Adjustment'),
    ], readonly=True)
    source_order_transaction_id = fields.Char('Shopify transaction', readonly=True)
    order_binding_id = fields.Many2one('eh.shopify.order', 'Shopify order', ondelete='set null', readonly=True)
    order_name = fields.Char('Order', readonly=True)
    transaction_id = fields.Many2one('eh.shopify.transaction', ondelete='set null', readonly=True)
    payment_id = fields.Many2one('account.payment', 'Odoo payment', ondelete='set null', readonly=True)
    amount = fields.Monetary(currency_field='currency_id', readonly=True)
    fee = fields.Monetary(currency_field='currency_id', readonly=True)
    net = fields.Monetary(currency_field='currency_id', readonly=True)
    transaction_date = fields.Datetime('Date', readonly=True)
    adjustment_reason = fields.Char(readonly=True)
    match_state = fields.Selection([
        ('matched', 'Settles its Odoo payment'),
        ('unmatched', 'Payment not in Odoo'),
        ('account', 'Booked to an account'),
    ], string='Match', readonly=True)
    move_line_id = fields.Many2one('account.move.line', 'Journal item', readonly=True)

    @api.depends('transaction_type')
    def _compute_readable_type(self):
        for line in self:
            line.transaction_type_label = util.readable_enum(line.transaction_type)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_payout_line_gid_uniq "
                            "ON eh_shopify_payout_line (payout_id, shopify_gid)")


class BankStatementLinePayouts(models.Model):
    _inherit = 'account.bank.statement.line'

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        companies = lines.mapped('company_id')
        if companies:
            payouts = self.env['eh.shopify.payout'].sudo().search([('company_id', 'in', companies.ids),
                                                                    ('state', '=', 'posted')])
            if payouts:
                payouts._match_deposit()
        return lines
