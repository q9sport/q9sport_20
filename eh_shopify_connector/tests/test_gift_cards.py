# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import bag, make_order
from .test_order_sale import ensure_chart


def tender(number, gateway, amount):
    return [{'id': 'gid://shopify/OrderTransaction/%s1' % number, 'kind': 'SALE', 'status': 'SUCCESS',
             'gateway': gateway, 'manualPaymentGateway': False, 'test': False,
             'processedAt': '2026-09-15T09:00:00Z', 'createdAt': '2026-09-15T09:00:00Z',
             'amountSet': bag('%.2f' % amount), 'parentTransaction': None}]


@tagged('post_install', '-at_install', 'eh_shopify')
class TestGiftCardsAndStoreCredit(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        cls.env['product.product'].create([{'name': 'Liability gear %s' % i, 'default_code': 'SKU-%s' % i}
                                           for i in range(2)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.Order = self.env['eh.shopify.order']
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})

    def _import(self, order):
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def test_a_gift_card_sold_is_owed_not_earned(self):
        order = make_order(number=81, lines=1)
        order['_all']['lineItems'][0]['isGiftCard'] = True
        binding = self._import(order)
        invoice = binding.sale_order_id.invoice_ids
        self.assertEqual(invoice.state, 'posted')
        line = invoice.invoice_line_ids.filtered(lambda l: l.display_type in (False, 'product'))
        self.assertEqual(line.account_id, self.store.gift_card_account_id)
        self.assertEqual(line.account_id.account_type, 'liability_current')

    def test_paying_with_a_gift_card_reduces_what_is_owed(self):
        binding = self._import(make_order(number=82, lines=1, transactions=tender(82, 'gift_card', 10.0)))
        transaction = binding.transaction_ids
        self.assertEqual(transaction.state, 'recorded', transaction.note)
        self.assertEqual(transaction.gateway_id.tender_type, 'gift_card')
        outstanding = compat.payment_outstanding_lines(transaction.payment_id) or \
            transaction.payment_id.move_id.line_ids.filtered(lambda l: l.account_id == self.store.gift_card_account_id)
        self.assertEqual(outstanding.account_id, self.store.gift_card_account_id)
        self.assertGreater(outstanding.debit, 0.0)
        self.assertIn(binding.sale_order_id.invoice_ids.payment_state, ('paid', 'in_payment'))

    def test_store_credit_has_its_own_liability(self):
        binding = self._import(make_order(number=83, lines=1, transactions=tender(83, 'shopify_store_credit', 10.0)))
        transaction = binding.transaction_ids
        self.assertEqual(transaction.gateway_id.tender_type, 'store_credit')
        self.assertEqual(transaction.state, 'recorded', transaction.note)
        lines = transaction.payment_id.move_id.line_ids.filtered(
            lambda l: l.account_id == self.store.store_credit_account_id)
        self.assertTrue(lines)
        self.assertNotEqual(self.store.store_credit_account_id, self.store.gift_card_account_id)

    def test_liability_accounting_can_be_switched_off(self):
        self.store.gift_card_liability = False
        binding = self._import(make_order(number=84, lines=1, transactions=tender(84, 'gift_card', 10.0)))
        self.assertFalse(binding.transaction_ids.gateway_id.journal_id)
        self.assertEqual(binding.transaction_ids.state, 'error')
        self.assertFalse(self.store.gift_card_account_id)
