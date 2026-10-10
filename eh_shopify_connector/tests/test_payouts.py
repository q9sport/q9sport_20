# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from odoo import fields
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart
from .test_refunds import add_refund


def _in_currency(node, code):
    """Every money amount of an order payload in ``code``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ('currencyCode', 'presentmentCurrencyCode') and isinstance(value, str):
                node[key] = code
            else:
                _in_currency(value, code)
    elif isinstance(node, list):
        for item in node:
            _in_currency(item, code)
    return node


@tagged('post_install', '-at_install', 'eh_shopify')
class TestPayouts(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        cls.env['product.product'].create([{'name': 'Payout gear %s' % i, 'default_code': 'SKU-%s' % i} for i in range(3)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.write({'payouts_enabled': True, 'currency_code': self.env.company.currency_id.name})
        self.shop.payments_account['defaultCurrency'] = self.env.company.currency_id.name
        self.Order = self.env['eh.shopify.order']
        self.Payout = self.env['eh.shopify.payout']
        self.Job = self.env['eh.shopify.job']
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})
        self.issued = (fields.Datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _import(self, order):
        if order['id'] not in self.shop.orders:
            self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def _run(self, job_type):
        for job in self.Job.search([('store_id', '=', self.store.id), ('job_type', '=', job_type),
                                    ('state', '=', 'pending')], order='id'):
            job._run_inline()
            self.assertEqual(job.state, 'done', job.last_error)

    def _poll(self):
        poll = self.Job._enqueue(self.store, 'payouts.poll', dedup_key='payouts.poll')
        poll._run_inline()
        self.assertEqual(poll.state, 'done', poll.last_error)
        self._run('refund.import')
        self._run('payout.post')
        return poll

    def _payout(self, number):
        return self.Payout.search([('store_id', '=', self.store.id), ('legacy_id', '=', str(number))])

    def _sale_payment(self, binding):
        return binding.transaction_ids.filtered(lambda t: t.kind == 'SALE').payment_id

    def _add(self, number, status, rows, net=None):
        currency = self.env.company.currency_id.name
        return self.shop.add_payout(number, status, self.issued, rows, currency=currency, net=net)

    def test_a_paid_payout_settles_its_payments_and_books_the_fees(self):
        binding = self._import(make_order(number=51, lines=1))
        payment = self._sale_payment(binding)
        outstanding = compat.payment_outstanding_lines(payment)
        self.assertTrue(outstanding, 'the payment waits on its outstanding account')
        self._add(901, 'PAID', [('CHARGE', 10.0, 0.59, '511', binding.shopify_gid)])
        self._poll()
        payout = self._payout(901)
        self.assertEqual(payout.state, 'posted', payout.reason)
        self.assertEqual(payout.move_id.state, 'posted')
        self.assertTrue(outstanding.reconciled)
        self.assertEqual(payout.line_ids.match_state, 'matched')
        fee = payout.move_id.line_ids.filtered(lambda l: l.account_id == self.store.payout_fee_account_id)
        self.assertAlmostEqual(fee.balance, 0.59, places=2)
        self.assertAlmostEqual(payout.transit_line_id.balance, 9.41, places=2)
        self.assertTrue(self.store.payout_transit_account_id.reconcile)

    def test_refunds_in_a_payout_settle_the_refund_payment(self):
        order = make_order(number=52, lines=2)
        binding = self._import(order)
        add_refund(order, 521, [(0, 1)])
        self._import(order)
        self._run('refund.import')
        refund_payment = binding.transaction_ids.filtered(lambda t: t.kind == 'REFUND').payment_id
        self.assertTrue(refund_payment)
        refund_outstanding = compat.payment_outstanding_lines(refund_payment)
        self._add(902, 'PAID', [('CHARGE', 20.0, 0.88, '521', binding.shopify_gid),
                                ('REFUND', -10.0, 0.0, '5219', binding.shopify_gid)])
        self._poll()
        payout = self._payout(902)
        self.assertEqual(payout.state, 'posted', payout.reason)
        self.assertTrue(refund_outstanding.reconciled)
        self.assertAlmostEqual(payout.transit_line_id.balance, 9.12, places=2)

    def test_a_payout_waits_for_missing_payments_and_can_be_posted_now(self):
        self._add(903, 'PAID', [('CHARGE', 15.0, 0.5, '99999', None)])
        self._poll()
        payout = self._payout(903)
        self.assertEqual(payout.state, 'unmatched')
        self.assertFalse(payout.move_id)
        payout.action_post_now()
        self._run('payout.post')
        self.assertEqual(payout.state, 'posted', payout.reason)
        self.assertEqual(payout.line_ids.move_line_id.account_id, self.store.payout_suspense_account_id)
        self.assertIn('suspense', payout.reason)

    def test_a_scheduled_payout_waits_and_a_failed_one_is_reversed(self):
        binding = self._import(make_order(number=53, lines=1))
        outstanding = compat.payment_outstanding_lines(self._sale_payment(binding))
        shopify_payout = self._add(904, 'SCHEDULED', [('CHARGE', 10.0, 0.3, '531', binding.shopify_gid)])
        self._poll()
        payout = self._payout(904)
        self.assertEqual((payout.state, bool(payout.move_id)), ('waiting', False))
        shopify_payout['status'] = 'PAID'
        self._poll()
        self.assertEqual(payout.state, 'posted', payout.reason)
        self.assertTrue(outstanding.reconciled)
        shopify_payout['status'] = 'FAILED'
        self._poll()
        self.assertEqual(payout.state, 'reversed', payout.reason)
        self.assertEqual(payout.reversal_move_id.state, 'posted')
        self.assertFalse(outstanding.reconciled, 'the payment is open again')

    def test_the_bank_deposit_is_matched_to_the_payout(self):
        binding = self._import(make_order(number=54, lines=1))
        self._add(905, 'PAID', [('CHARGE', 10.0, 0.59, '541', binding.shopify_gid)])
        self._poll()
        payout = self._payout(905)
        self.assertEqual(payout.state, 'posted', payout.reason)
        deposit = self.env['account.bank.statement.line'].create({
            'journal_id': self.bank.id, 'date': fields.Date.context_today(self.env['res.users']),
            'payment_ref': 'SHOPIFY PAYOUT', 'amount': 9.41})
        self.assertEqual(payout.state, 'deposited')
        self.assertEqual(payout.deposit_line_id, deposit)
        self.assertTrue(deposit.is_reconciled)
        self.assertTrue(payout.transit_line_id.reconciled)

    def test_a_payout_in_a_second_currency_settles_and_meets_its_deposit(self):
        company = self.env.company
        other = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', 'EUR' if company.currency_id.name != 'EUR' else 'USD')], limit=1)
        other.active = True
        Rate = self.env['res.currency.rate']
        Rate.create({'currency_id': other.id, 'company_id': company.id, 'name': '2026-01-01', 'rate': 0.9})
        Rate.create({'currency_id': other.id, 'company_id': company.id, 'rate': 0.8,
                     'name': fields.Date.context_today(self.env['res.users'])})
        self.store.currency_code = other.name
        self.shop.payments_account['defaultCurrency'] = other.name
        order = make_order(number=58, lines=1)
        _in_currency(order, other.name)
        binding = self._import(order)
        payment = self._sale_payment(binding)
        self.assertEqual(payment.currency_id, other)
        self.shop.add_payout(910, 'PAID', self.issued, [('CHARGE', 10.0, 0.59, '581', binding.shopify_gid)],
                             currency=other.name)
        self._poll()
        payout = self._payout(910)
        self.assertEqual((payout.state, payout.currency_id), ('posted', other), payout.reason)
        self.assertFalse(compat.payment_outstanding_lines(payment), 'the payout settles the payment across rates')
        self.assertAlmostEqual(payout.transit_line_id.amount_currency, 9.41, places=2)
        bank = self.env['account.journal'].create({'name': 'Shopify %s bank' % other.name, 'type': 'bank',
                                                   'code': 'SHX', 'currency_id': other.id})
        exchange_journal = company.currency_exchange_journal_id
        company.currency_exchange_journal_id = False
        deposit = self.env['account.bank.statement.line'].create({
            'journal_id': bank.id, 'date': fields.Date.context_today(self.env['res.users']),
            'payment_ref': 'SHOPIFY PAYOUT', 'amount': 9.41})
        self.assertEqual(payout.state, 'posted', 'without an exchange journal the statement line is still created')
        self.assertIn('exchange', payout.reason)
        company.currency_exchange_journal_id = exchange_journal
        payout._match_deposit()
        self.assertEqual(payout.state, 'deposited', 'a deposit at a later rate still matches in the payout currency')
        self.assertEqual(payout.deposit_line_id, deposit)
        self.assertTrue(payout.transit_line_id.reconciled)

    def test_disputes_and_unknown_types_use_the_adjustment_account(self):
        binding = self._import(make_order(number=55, lines=1))
        self._add(906, 'PAID', [('CHARGE', 10.0, 0.0, '551', binding.shopify_gid),
                                ('DISPUTE_WITHDRAWAL', -4.0, 0.0, None, binding.shopify_gid),
                                ('SOMETHING_NEW_FROM_SHOPIFY', 1.0, 0.0, None, None)])
        self._poll()
        payout = self._payout(906)
        self.assertEqual(payout.state, 'posted', payout.reason)
        others = payout.line_ids.filtered(lambda l: l.family in ('dispute', 'adjustment'))
        self.assertEqual(len(others), 2)
        self.assertEqual(others.mapped('move_line_id.account_id'), self.store.payout_adjustment_account_id)
        self.assertAlmostEqual(payout.transit_line_id.balance, 7.0, places=2)

    def test_transactions_that_do_not_add_up_post_nothing(self):
        binding = self._import(make_order(number=56, lines=1))
        self._add(907, 'PAID', [('CHARGE', 10.0, 0.5, '561', binding.shopify_gid)], net=12.0)
        self._poll()
        payout = self._payout(907)
        self.assertEqual(payout.state, 'error')
        self.assertIn('add up', payout.reason)
        self.assertFalse(payout.move_id)

    def test_a_store_without_shopify_payments_or_the_permission_is_explained(self):
        self.shop.payments_account = None
        poll = self._poll()
        self.assertIn('skipped', poll.result)
        self.assertIn('Shopify Payments', self.store.payout_note)
        self.store.granted_scopes = 'read_orders'
        poll = self.Job._enqueue(self.store, 'payouts.poll', dedup_key='payouts.poll.again')
        poll._run_inline()
        self.assertEqual(poll.result, {'skipped': 'missing permission'})
