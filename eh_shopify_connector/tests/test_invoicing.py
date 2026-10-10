# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart


@tagged('post_install', '-at_install', 'eh_shopify')
class TestInvoicing(ShopifyCase):
    """What the customer paid in Shopify reaches Odoo under every invoice policy."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        cls.env['product.product'].create({'name': 'Invoicing gear', 'default_code': 'SKU-0'})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})
        self.Order = self.env['eh.shopify.order']
        self.Job = self.env['eh.shopify.job']

    def _import(self, order):
        if order['id'] not in self.shop.orders:
            self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def _run_pending(self, job_type):
        jobs = self.Job.search([('store_id', '=', self.store.id), ('job_type', '=', job_type),
                                ('state', '=', 'pending')], order='id')
        for job in jobs:
            job._run_inline()
            self.assertEqual(job.state, 'done', job.last_error)
        return jobs

    def test_an_invoice_posted_by_hand_records_what_the_customer_paid(self):
        self.store.invoice_policy = 'manual'
        binding = self._import(make_order(number=91))
        transaction = binding.transaction_ids.filtered(lambda t: t.kind == 'SALE')
        self.assertEqual(transaction.state, 'pending', 'there is nothing to record before an invoice exists')
        invoice = binding.sale_order_id._create_invoices()
        invoice.action_post()
        self.assertTrue(self._run_pending('order.invoice'), 'posting by hand queues the payment')
        self.assertEqual(transaction.state, 'recorded')
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))

    def test_when_delivered_invoices_and_pays_on_the_delivery(self):
        self.store.invoice_policy = 'delivered'
        binding = self._import(make_order(number=92))
        self.assertFalse(binding.sale_order_id.invoice_ids, 'nothing is invoiced before the goods leave')
        compat.validate_picking(binding.sale_order_id.picking_ids)
        self.Job.search([('job_type', '=', 'fulfillment.push'), ('state', '=', 'pending')]).write({'state': 'cancelled'})
        self._run_pending('order.invoice')
        invoice = binding.sale_order_id.invoice_ids
        self.assertEqual(invoice.state, 'posted')
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))

    def test_the_invoice_carries_the_day_of_the_order(self):
        self.store.invoice_policy = 'paid'
        binding = self._import(make_order(number=93))
        invoice = binding.sale_order_id.invoice_ids
        self.assertEqual(invoice.invoice_date, datetime.date(2026, 9, 15),
                         'an order imported later still books on the day it was placed')
