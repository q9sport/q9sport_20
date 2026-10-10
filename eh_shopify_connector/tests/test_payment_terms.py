# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart


def term_days(term):
    line = term.line_ids
    return line.nb_days if 'nb_days' in line._fields else line.days


@tagged('post_install', '-at_install', 'eh_shopify')
class TestPaymentTerms(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.env['product.product'].create([{'name': 'Terms gear %s' % i, 'default_code': 'SKU-%s' % i} for i in range(2)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']

    def _import(self, number, terms):
        order = make_order(number=number, lines=1)
        order['paymentTerms'] = terms
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])]).sale_order_id

    def test_net_terms_become_an_odoo_payment_term_reused_by_later_orders(self):
        terms = {'paymentTermsName': 'Net 37', 'paymentTermsType': 'NET', 'dueInDays': 37}
        first = self._import(111, terms)
        term = first.payment_term_id
        self.assertEqual((len(term.line_ids), term_days(term)), (1, 37))
        second = self._import(112, dict(terms))
        self.assertEqual(second.payment_term_id, term, 'the same term is reused')

    def test_due_on_receipt_uses_a_term_due_at_once(self):
        sale_order = self._import(113, {'paymentTermsName': 'Due on receipt', 'paymentTermsType': 'RECEIPT',
                                        'dueInDays': None})
        self.assertTrue(sale_order.payment_term_id)
        self.assertEqual(term_days(sale_order.payment_term_id), 0)

    def test_terms_are_explained_when_they_cannot_be_matched(self):
        self.store.auto_create_payment_terms = False
        sale_order = self._import(114, {'paymentTermsName': 'Net 41', 'paymentTermsType': 'NET', 'dueInDays': 41})
        self.assertFalse(sale_order.payment_term_id.filtered(lambda term: term_days(term) == 41))
        self.assertFalse(self.env['account.payment.term'].search([('name', '=', 'Net 41')]))
        self.assertTrue(sale_order.message_ids.filtered(lambda message: 'Net 41' in (message.body or '')))
        fixed = self._import(115, {'paymentTermsName': 'Due on 1 October', 'paymentTermsType': 'FIXED',
                                   'dueInDays': None})
        self.assertTrue(fixed.message_ids.filtered(lambda message: 'Due on 1 October' in (message.body or '')))
