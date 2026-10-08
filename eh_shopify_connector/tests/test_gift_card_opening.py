# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models import eh_shopify_gift_card
from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart


@tagged('post_install', '-at_install', 'eh_shopify')
class TestGiftCardOpening(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        cls.env['product.product'].create({'name': 'Opening gear', 'default_code': 'SKU-0'})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        if 'read_gift_cards' not in (self.store.granted_scopes or ''):
            self.store.granted_scopes = ','.join(filter(None, [self.store.granted_scopes, 'read_gift_cards']))
        self.store.write({'invoice_policy': 'manual', 'order_import_from': '2026-06-01 00:00:00'})
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})
        self.Card = self.env['eh.shopify.gift.card']
        self.Job = self.env['eh.shopify.job']
        self.currency = self.env.company.currency_id.name

    def _book(self):
        self.store.action_book_gift_card_opening()
        for _attempt in range(20):
            job = self.Job.search([('store_id', '=', self.store.id), ('job_type', '=', 'gift_cards.opening'),
                                   ('state', '=', 'pending')], order='id', limit=1)
            if not job:
                return
            job._run_inline()
            self.assertEqual(job.state, 'done', job.last_error)
        self.fail('the opening balance jobs did not finish')

    def _owed(self):
        lines = self.env['account.move.line'].search([('account_id', '=', self.store.gift_card_account_id.id),
                                                      ('parent_state', '=', 'posted')])
        return round(sum(lines.mapped('balance')), 2)

    def test_cards_sold_before_odoo_are_booked_as_they_stood_at_the_cut_over(self):
        order = make_order(number=71)
        self.shop.add_order(order)
        job = self.env['eh.shopify.order']._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.shop.add_gift_card(1, 70.0, self.currency, transactions=[
            ('debit', 30.0, '2026-07-01T10:00:00Z'), ('debit', 5.0, '2026-05-01T10:00:00Z')])
        self.shop.add_gift_card(2, 40.0, self.currency, order_gid=order['id'])
        self.shop.add_gift_card(3, 0.0, self.currency)
        self.shop.add_gift_card(4, 15.0, self.currency, enabled=False)
        self.shop.add_gift_card(6, 20.0, self.currency, order_gid='gid://shopify/Order/9999',
                                order_created='2026-07-02T00:00:00Z')
        self.shop.add_gift_card(7, 0.0, self.currency, transactions=[('debit', 12.0, '2026-08-01T00:00:00Z')])
        self._book()
        cards = self.Card.search([('store_id', '=', self.store.id)])
        self.assertEqual(sorted((card.last_characters, card.balance_booked) for card in cards),
                         [('0001', 100.0), ('0007', 12.0)],
                         'spends since the cut over were booked by imported orders, so they are owed back here')
        self.assertEqual(set(cards.mapped('state')), {'booked'})
        self.assertEqual(self._owed(), -112.0)
        counterpart = cards.move_id.line_ids.filtered(lambda line: line.account_id != self.store.gift_card_account_id)
        self.assertEqual(counterpart.account_id.account_type, 'equity_unaffected')
        self._book()
        self.assertEqual(len(self.Card.search([('store_id', '=', self.store.id)])), 2, 'a card is booked once')
        self.shop.add_gift_card(5, 10.0, self.currency)
        self._book()
        self.assertEqual(self._owed(), -122.0)
        self.assertIn('1 gift card', self.store.gift_card_opening_note)

    def test_a_long_card_list_is_read_in_slices_and_booked_in_one_entry(self):
        for number in (11, 12, 13):
            self.shop.add_gift_card(number, 10.0, self.currency)
        with patch.object(eh_shopify_gift_card, 'PAGE', 1), patch.object(eh_shopify_gift_card, 'PAGES_PER_JOB', 1):
            self._book()
        cards = self.Card.search([('store_id', '=', self.store.id)])
        self.assertEqual((len(cards), len(cards.move_id), set(cards.mapped('state'))), (3, 1, {'booked'}))
        self.assertEqual(self._owed(), -30.0)
        self.assertFalse(self.store.gift_card_opening_cursor)

    def test_missing_permission_or_liability_setting_is_explained(self):
        self.store.granted_scopes = 'read_orders'
        with self.assertRaises(UserError):
            self.store.action_book_gift_card_opening()
        self.store.granted_scopes = 'read_orders,read_gift_cards'
        self.store.gift_card_liability = False
        with self.assertRaises(UserError):
            self.store.action_book_gift_card_opening()
