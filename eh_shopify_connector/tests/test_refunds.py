# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import bag, make_order
from .test_order_sale import ensure_chart


def add_refund(order, number, lines, restock='NO_RESTOCK', total=None, updated='2026-09-16T08:00:00Z'):
    items = order['_all']['lineItems']
    refund_lines = []
    amount = 0.0
    for index, quantity in lines:
        amount += 10.0 * quantity
        refund_lines.append({
            'id': 'gid://shopify/RefundLineItem/%s%02d' % (number, index), 'quantity': quantity,
            'restockType': restock, 'restocked': restock == 'RETURN', 'location': None,
            'lineItem': {'id': items[index]['id']}, 'priceSet': bag('10.00'),
            'subtotalSet': bag('%.2f' % (10.0 * quantity)), 'totalTaxSet': bag('0.00')})
        items[index]['currentQuantity'] -= quantity
    total = total if total is not None else '%.2f' % amount
    transaction = {'id': 'gid://shopify/OrderTransaction/%s9' % number, 'kind': 'REFUND', 'status': 'SUCCESS',
                   'gateway': 'shopify_payments', 'manualPaymentGateway': False, 'test': False,
                   'processedAt': updated, 'createdAt': updated, 'amountSet': bag(total),
                   'parentTransaction': {'id': order['transactions'][0]['id']}}
    refund = {'id': 'gid://shopify/Refund/%s' % number, 'legacyResourceId': str(number), 'createdAt': updated,
              'processedAt': updated, 'note': 'Changed mind', 'totalRefundedSet': bag(total), 'lines': refund_lines,
              'shipping': [], 'duties': [], 'adjustments': [], 'transactions': [transaction]}
    order['_all'].setdefault('refunds', []).append(refund)
    refunded = sum(float(r['totalRefundedSet']['shopMoney']['amount']) for r in order['_all']['refunds'])
    order['totalRefundedSet'] = bag('%.2f' % refunded)
    order['displayFinancialStatus'] = 'PARTIALLY_REFUNDED'
    order['updatedAt'] = updated
    order['transactions'].append(transaction)
    order['transactionsCount'] = {'count': len(order['transactions'])}
    return refund


@tagged('post_install', '-at_install', 'eh_shopify')
class TestRefunds(ShopifyCase):

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

    def _run_refund_jobs(self):
        for job in self.Job.search([('store_id', '=', self.store.id), ('job_type', '=', 'refund.import'),
                                    ('state', '=', 'pending')], order='id'):
            job._run_inline()
            self.assertEqual(job.state, 'done', job.last_error)

    def test_partial_refund_becomes_a_posted_credit_note_with_its_payment(self):
        order = make_order(number=31, lines=2)
        binding = self._import(order)
        invoice = binding.sale_order_id.invoice_ids
        self.assertEqual(invoice.state, 'posted')
        add_refund(order, 311, [(0, 1)])
        self._import(order)
        self._run_refund_jobs()
        refund = binding.refund_ids
        self.assertEqual(refund.state, 'done', refund.reason)
        credit_note = refund.credit_note_ids
        self.assertEqual((credit_note.move_type, credit_note.state, credit_note.amount_total),
                         ('out_refund', 'posted', 10.0))
        self.assertEqual(credit_note.reversed_entry_id, invoice)
        refunded_line = binding.line_ids.filtered(lambda l: l.shopify_gid == order['_all']['lineItems'][0]['id']).sale_line_id
        self.assertEqual(refunded_line.qty_invoiced, 0.0)
        payment = binding.transaction_ids.filtered(lambda t: t.kind == 'REFUND')
        self.assertEqual(payment.state, 'recorded', payment.note)
        self.assertIn(credit_note.payment_state, ('paid', 'in_payment'))

    def test_the_same_refund_is_recorded_once(self):
        order = make_order(number=32, lines=1)
        binding = self._import(order)
        add_refund(order, 321, [(0, 1)])
        self._import(order)
        self._run_refund_jobs()
        job = self.Refund._enqueue_refund_import(self.store, 'gid://shopify/Refund/321', order['id'])
        job._run_inline()
        self.assertEqual(job.result, {'unchanged': 'gid://shopify/Refund/321'})
        self.assertEqual(len(binding.refund_ids.credit_note_ids), 1)

    def test_refund_before_invoicing_waits_then_follows_the_invoice(self):
        self.store.invoice_policy = 'manual'
        order = make_order(number=33, lines=1)
        binding = self._import(order)
        add_refund(order, 331, [(0, 1)])
        self._import(order)
        self._run_refund_jobs()
        refund = binding.refund_ids
        self.assertEqual(refund.state, 'pending_invoice')
        compat.create_invoices(binding.sale_order_id)
        self._run_refund_jobs()
        self.assertEqual(refund.state, 'done', refund.reason)
        self.assertEqual(refund.credit_note_ids.state, 'posted')

    def test_a_refund_that_does_not_add_up_stays_in_draft(self):
        order = make_order(number=34, lines=2)
        binding = self._import(order)
        add_refund(order, 341, [(0, 1)], total='15.00')
        self._import(order)
        self._run_refund_jobs()
        refund = binding.refund_ids
        self.assertEqual(refund.state, 'draft')
        self.assertIn('15.0', refund.reason)
        self.assertEqual(refund.credit_note_ids.state, 'draft')

    def test_cancelled_units_leave_the_open_delivery(self):
        order = make_order(number=35, lines=2)
        binding = self._import(order)
        add_refund(order, 351, [(1, 1)], restock='CANCEL')
        self._import(order)
        self._run_refund_jobs()
        sale_line = binding.line_ids.filtered(lambda l: l.shopify_gid == order['_all']['lineItems'][1]['id']).sale_line_id
        self.assertEqual(sale_line.product_uom_qty, 0.0)

    def test_refund_webhook_queues_the_import(self):
        order = make_order(number=36, lines=1)
        self._import(order)
        event = self.env['eh.shopify.event'].new({'store_id': self.store.id, 'topic': 'refunds/create',
                                                  'webhook_id': 'w-r36', 'payload': {'id': 361, 'order_id': 36}})
        self.assertEqual(event._on_refund_create(), {'queued': 'gid://shopify/Refund/361'})
        self.assertTrue(self.Job.search([('job_type', '=', 'refund.import'), ('dedup_key', '=', 'refund:gid://shopify/Refund/361')]))
        self.assertIn('REFUNDS_CREATE', self.store._webhook_topics())

    # ------------------------------------------------------------------ review round
    def _deliver(self, binding):
        compat.validate_picking(binding.sale_order_id.picking_ids)
        # Validating queues the fulfilment push on the order, which runs first.
        self.Job.search([('job_type', '=', 'fulfillment.push'), ('state', '=', 'pending')]).write({'state': 'cancelled'})

    def test_money_refunded_on_goods_kept_leaves_the_order_invoiced(self):
        order = make_order(number=37, lines=1)
        binding = self._import(order)
        self._deliver(binding)
        add_refund(order, 371, [(0, 1)])
        self._import(order)
        self._run_refund_jobs()
        refund = binding.refund_ids
        self.assertEqual(refund.state, 'done', refund.reason)
        lines = refund.credit_note_ids.invoice_line_ids.filtered(lambda l: l.display_type in (False, 'product'))
        self.assertFalse(lines.sale_line_ids)
        sale_line = binding.line_ids.filtered(lambda l: l.shopify_gid == order['_all']['lineItems'][0]['id']).sale_line_id
        self.assertEqual((sale_line.product_uom_qty, sale_line.qty_invoiced), (1.0, 1.0))
        self.assertNotEqual(binding.sale_order_id.invoice_status, 'to invoice')
        payment = binding.transaction_ids.filtered(lambda t: t.kind == 'REFUND').payment_id
        self.assertEqual(payment.payment_method_line_id.payment_type, 'outbound')

    def test_a_draft_refund_is_finished_when_its_credit_note_is_posted(self):
        self.store.refund_auto_post = False
        order = make_order(number=38, lines=2)
        binding = self._import(order)
        add_refund(order, 381, [(0, 1)])
        self._import(order)
        self._run_refund_jobs()
        refund = binding.refund_ids
        self.assertEqual(refund.state, 'draft')
        order['updatedAt'] = '2026-09-16T09:30:00Z'
        self._import(order)
        self._run_refund_jobs()
        self.assertEqual(len(refund.credit_note_ids), 1, 'an order update never makes a second credit note')
        self.assertEqual(self.env['account.move'].search_count([
            ('move_type', '=', 'out_refund'), ('invoice_origin', '=', binding.sale_order_id.name)]), 1)
        refund.credit_note_ids.action_post()
        self._run_refund_jobs()
        self.assertEqual(refund.state, 'done', refund.reason)
        self.assertEqual(binding.transaction_ids.filtered(lambda t: t.kind == 'REFUND').state, 'recorded')

    def test_cancelled_units_on_a_locked_order_are_removed_and_it_stays_locked(self):
        order = make_order(number=39, lines=2)
        binding = self._import(order)
        sale_order = binding.sale_order_id
        compat.lock_orders(sale_order)
        add_refund(order, 391, [(1, 1)], restock='CANCEL')
        self._import(order)
        self._run_refund_jobs()
        sale_line = binding.line_ids.filtered(lambda l: l.shopify_gid == order['_all']['lineItems'][1]['id']).sale_line_id
        self.assertEqual(sale_line.product_uom_qty, 0.0)
        self.assertTrue(sale_order.locked if 'locked' in sale_order._fields else sale_order.state == 'done')

    def test_a_refund_payment_confirmed_later_is_recorded(self):
        order = make_order(number=40, lines=2)
        binding = self._import(order)
        refund_data = add_refund(order, 401, [(0, 1)])
        refund_data['transactions'][0]['status'] = 'PENDING'
        self._import(order)
        self._run_refund_jobs()
        transaction = binding.transaction_ids.filtered(lambda t: t.kind == 'REFUND')
        self.assertEqual(binding.refund_ids.state, 'done', binding.refund_ids.reason)
        self.assertNotEqual(transaction.state, 'recorded')
        refund_data['transactions'][0]['status'] = 'SUCCESS'
        order['updatedAt'] = '2026-09-16T09:45:00Z'
        self._import(order)
        self._run_refund_jobs()
        self.assertEqual(transaction.state, 'recorded', transaction.note)

    def test_an_order_keeps_its_money_basis_when_the_store_setting_changes(self):
        order = make_order(number=46, lines=1)
        binding = self._import(order)
        mode = binding.currency_mode
        self.store.order_currency_mode = 'presentment' if mode == 'shop' else 'shop'
        order['updatedAt'] = '2026-09-16T09:50:00Z'
        self._import(order)
        self.assertEqual(binding.currency_mode, mode)

    def test_a_change_during_a_running_refund_import_is_read_again(self):
        job = self.Refund._enqueue_refund_import(self.store, 'gid://shopify/Refund/999', 'gid://shopify/Order/999')
        job.state = 'running'
        follow = self.Refund._enqueue_refund_import(self.store, 'gid://shopify/Refund/999', 'gid://shopify/Order/999')
        self.assertNotEqual(follow, job)
        self.assertEqual(follow.dedup_key, 'refund:gid://shopify/Refund/999:next')

    def test_units_cancelled_from_a_line_keep_its_price(self):
        order = make_order(number=57, lines=1)
        item = order['_all']['lineItems'][0]
        item['quantity'] = item['currentQuantity'] = 2
        for key in ('currentTotalPriceSet', 'totalPriceSet'):
            order[key] = bag('20.00')
        order['transactions'][0]['amountSet'] = bag('20.00')
        binding = self._import(order)
        add_refund(order, 571, [(0, 1)], restock='CANCEL')
        self._import(order)
        self._run_refund_jobs()
        sale_line = binding.line_ids.filtered(lambda l: l.shopify_gid == item['id']).sale_line_id
        self.assertEqual((sale_line.product_uom_qty, sale_line.price_unit), (1.0, 10.0))
