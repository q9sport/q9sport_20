# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart
from .test_refunds import add_refund


@tagged('post_install', '-at_install', 'eh_shopify')
class TestRefundInShopify(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        cls.env['product.product'].create([{'name': 'Brass gear %s' % i, 'default_code': 'SKU-%s' % i} for i in range(3)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.Order = self.env['eh.shopify.order']
        self.Refund = self.env['eh.shopify.refund']
        self.Job = self.env['eh.shopify.job']
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})

    def _import(self, order):
        if order['id'] not in self.shop.orders:
            self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def _credit_note(self, invoice, keep_first_line_only=True):
        credit_note = invoice._reverse_moves([{'invoice_date': invoice.invoice_date}], cancel=False)
        if keep_first_line_only:
            product_lines = credit_note.invoice_line_ids.filtered(lambda l: l.display_type in (False, 'product'))
            credit_note.write({'invoice_line_ids': [Command.unlink(line.id) for line in product_lines[1:]]})
        credit_note.action_post()
        return credit_note

    def _wizard(self, credit_note):
        action = credit_note.action_eh_shopify_refund()
        return self.env['eh.shopify.refund.wizard'].browse(action['res_id'])

    def _refund_job(self, credit_note):
        return self.Job.search([('job_type', '=', 'refund.create'), ('res_model', '=', 'account.move'),
                                ('res_id', '=', credit_note.id)], order='id desc', limit=1)

    def test_credit_note_is_refunded_in_shopify_once(self):
        order = make_order(number=41, lines=2)
        binding = self._import(order)
        credit_note = self._credit_note(binding.sale_order_id.invoice_ids)
        self.assertTrue(credit_note.eh_shopify_can_refund)
        wizard = self._wizard(credit_note)
        self.assertEqual((wizard.suggested_total, len(wizard.line_ids)), (10.0, 1))
        wizard.action_confirm()
        job = self._refund_job(credit_note)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        sent = self.shop.refund_creates[-1]
        self.assertEqual(sent['transactions'][0]['amount'], '10.00')
        self.assertEqual(sent['transactions'][0]['parentId'], order['transactions'][0]['id'])
        self.assertEqual(sent['refundLineItems'][0]['quantity'], 1)
        self.assertEqual(sent['note'], credit_note.name)
        self.assertEqual(credit_note.eh_shopify_refund_state, 'done')
        refund = self.Refund.search([('credit_note_ids', 'in', credit_note.ids)])
        self.assertEqual(refund.state, 'done')
        self.assertIn(credit_note.payment_state, ('paid', 'in_payment'))
        self.assertFalse(credit_note.eh_shopify_can_refund)
        echo = self.Refund._enqueue_refund_import(self.store, refund.shopify_gid, order['id'])
        echo._run_inline()
        self.assertEqual(echo.result, {'unchanged': refund.shopify_gid})

    def test_a_repeated_confirmation_never_refunds_twice(self):
        order = make_order(number=42, lines=1)
        binding = self._import(order)
        credit_note = self._credit_note(binding.sale_order_id.invoice_ids)
        wizard = self._wizard(credit_note)
        wizard.action_confirm()
        job = self._refund_job(credit_note)
        payload = dict(job.payload)
        job._run_inline()
        again = self.Job._enqueue(self.store, 'refund.create', payload=payload, dedup_key='refund.create.again')
        again._run_inline()
        self.assertIn('skipped', again.result)
        self.assertEqual(len(self.shop.refund_creates), 1)
        self.Refund.search([('credit_note_ids', 'in', credit_note.ids)]).unlink()
        credit_note.eh_shopify_refund_state = False
        third = self.Job._enqueue(self.store, 'refund.create', payload=payload, dedup_key='refund.create.third')
        third._run_inline()
        self.assertEqual(third.state, 'done', third.last_error)
        self.assertEqual(len(self.shop.refund_creates), 1, 'the earlier refund carrying the credit note is linked')

    def test_more_than_shopify_can_return_is_blocked(self):
        order = make_order(number=43, lines=2)
        binding = self._import(order)
        add_refund(order, 431, [(0, 1)])
        self._import(order)
        credit_note = self._credit_note(binding.sale_order_id.invoice_ids, keep_first_line_only=False)
        wizard = self._wizard(credit_note)
        self.assertIn('at most', wizard.blocked_reason)
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_shopify_refusal_is_shown_on_the_credit_note(self):
        order = make_order(number=44, lines=1)
        binding = self._import(order)
        credit_note = self._credit_note(binding.sale_order_id.invoice_ids)
        self._wizard(credit_note).action_confirm()
        self.shop.refund_refusal = 'Refund amount is not allowed'
        job = self._refund_job(credit_note)
        job._run_inline()
        self.assertEqual(credit_note.eh_shopify_refund_state, 'error')
        self.assertIn('Refund amount is not allowed', credit_note.eh_shopify_refund_note)
        self.assertTrue(credit_note.eh_shopify_can_refund, 'the manager can try again')

    def test_only_shopify_managers_refund_in_shopify(self):
        order = make_order(number=45, lines=1)
        binding = self._import(order)
        credit_note = self._credit_note(binding.sale_order_id.invoice_ids)
        user = self._user('eh_billing_clerk', ['eh_shopify_connector.group_shopify_user', 'account.group_account_invoice'])
        with self.assertRaises(AccessError):
            credit_note.with_user(user).action_eh_shopify_refund()

    # ------------------------------------------------------------------ review round
    def test_a_push_that_stopped_after_shopify_refunded_is_linked_not_booked_twice(self):
        order = make_order(number=47, lines=2)
        binding = self._import(order)
        invoice = binding.sale_order_id.invoice_ids
        credit_note = self._credit_note(invoice)
        self._wizard(credit_note).action_confirm()
        job = self._refund_job(credit_note)
        answer = self.shop.op_EhRefundCreate({'input': job.payload['input'], 'idempotencyKey': job.payload['key']})
        job.write({'state': 'dead'})
        credit_note.invalidate_recordset(['eh_shopify_can_refund'])
        self.assertTrue(credit_note.eh_shopify_can_refund, 'a request that gave up shows the button again')
        refund_gid = answer['refundCreate']['refund']['id']
        imported = self.Refund._enqueue_refund_import(self.store, refund_gid, order['id'])
        imported._run_inline()
        self.assertEqual(imported.state, 'done', imported.last_error)
        refund = self.Refund.search([('shopify_gid', '=', refund_gid)])
        self.assertEqual(refund.state, 'done', refund.reason)
        self.assertEqual(refund.credit_note_ids, credit_note)
        self.assertEqual(self.env['account.move'].search_count([('move_type', '=', 'out_refund'),
                                                                ('reversed_entry_id', '=', invoice.id)]), 1)
        self.assertEqual(credit_note.eh_shopify_refund_state, 'done')

    def test_a_changed_dialog_is_refused(self):
        order = make_order(number=48, lines=2)
        binding = self._import(order)
        credit_note = self._credit_note(binding.sale_order_id.invoice_ids)
        wizard = self._wizard(credit_note)
        wizard.line_ids[0].quantity = 0
        with self.assertRaisesRegex(UserError, 'no longer matches'):
            wizard.action_confirm()
        self.assertFalse(self._refund_job(credit_note))

    def test_cancel_chosen_in_the_dialog_reaches_the_sales_order(self):
        order = make_order(number=49, lines=2)
        binding = self._import(order)
        credit_note = self._credit_note(binding.sale_order_id.invoice_ids)
        wizard = self._wizard(credit_note)
        wizard.line_ids.restock_type = 'CANCEL'
        wizard.action_confirm()
        job = self._refund_job(credit_note)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        sale_line = binding.line_ids.filtered(
            lambda l: l.shopify_gid == order['_all']['lineItems'][0]['id']).sale_line_id
        self.assertEqual(sale_line.product_uom_qty, 0.0)

    def test_a_refund_in_another_customer_currency_is_blocked(self):
        order = make_order(number=50, lines=1)
        binding = self._import(order)
        credit_note = self._credit_note(binding.sale_order_id.invoice_ids)
        self.shop._money_bag = lambda value, currency: {
            'shopMoney': {'amount': '%.2f' % value, 'currencyCode': currency},
            'presentmentMoney': {'amount': '%.2f' % value, 'currencyCode': 'EUR'}}
        wizard = self._wizard(credit_note)
        self.assertIn('EUR', wizard.blocked_reason or '')
