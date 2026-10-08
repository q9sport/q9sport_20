# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.exceptions import UserError
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import bag, make_order
from .test_order_sale import ensure_chart
from .test_refunds import add_refund

PUSH = 'order.edit.push'


@tagged('post_install', '-at_install', 'eh_shopify')
class TestOrderEditPush(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        cls.products = cls.env['product.product'].create([{'name': 'Edit push gear %s' % i, 'default_code': 'SKU-%s' % i,
                                                           'list_price': 10.0} for i in range(3)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        if 'write_order_edits' not in (self.store.granted_scopes or ''):
            self.store.granted_scopes = ','.join(filter(None, [self.store.granted_scopes, 'write_order_edits']))
        self.store.invoice_policy = 'manual'
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

    def _wizard(self, sale_order):
        action = sale_order.action_eh_shopify_send_edits()
        return self.env['eh.shopify.order.edit.wizard'].browse(action['res_id'])

    def _run_pending(self, resource_key):
        for job in self.Job.search([('resource_key', '=', resource_key), ('state', '=', 'pending')], order='id'):
            job._run_inline()
            self.assertEqual(job.state, 'done', '%s: %s' % (job.job_type, job.last_error))

    def _sale_line(self, binding, order, index):
        gid = order['_all']['lineItems'][index]['id']
        return binding.line_ids.filtered(lambda line: line.shopify_gid == gid).sale_line_id

    def _add_odoo_line(self, sale_order, product, quantity):
        compat.unlock_orders(sale_order)
        sale_order.write({'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': quantity})]})

    def _variant(self, index):
        gid = 'gid://shopify/ProductVariant/%s' % index
        Variant = self.env['eh.shopify.variant']
        variant = Variant.search([('store_id', '=', self.store.id), ('shopify_gid', '=', gid)], limit=1)
        if variant:
            # Importing an order already links the variants it sold.
            variant.write({'product_id': self.products[index].id, 'active_in_shopify': True})
            return variant
        return Variant.create({'store_id': self.store.id, 'product_id': self.products[index].id, 'shopify_gid': gid,
                               'sku': 'SKU-%s' % index})

    def test_quantities_and_products_changed_in_odoo_are_saved_in_shopify(self):
        order = make_order(number=81, lines=2)
        binding = self._import(order)
        sale_order = binding.sale_order_id
        first = self._sale_line(binding, order, 0)
        compat.set_sale_line_quantity(first, 3)
        self._variant(2)
        self._add_odoo_line(sale_order, self.products[2], 2)
        wizard = self._wizard(sale_order)
        self.assertFalse(wizard.blocked_reason)
        self.assertEqual(sorted((line.action, line.before, line.after) for line in wizard.line_ids),
                         [('add', 0, 2), ('set', 1, 3)])
        wizard.action_send()
        self._run_pending(order['id'])
        self.assertEqual(len(self.shop.order_edits), 1)
        items = self.shop.orders[order['id']]['_all']['lineItems']
        self.assertEqual([(item['sku'], item['currentQuantity']) for item in items],
                         [('SKU-0', 3), ('SKU-1', 1), ('SKU-2', 2)])
        added = binding.line_ids.filtered(lambda line: line.variant_gid == 'gid://shopify/ProductVariant/2')
        self.assertEqual(added.sale_line_id.product_id, self.products[2])
        self.assertEqual(len(sale_order.order_line), 3, 'the saved edit does not come back as new lines')
        self.assertEqual(first.product_uom_qty, 3)
        self.assertEqual(self._wizard(sale_order).blocked_reason, 'Shopify already holds the quantities of this order.')

    def test_a_shipped_order_is_edited_in_shopify_only(self):
        order = make_order(number=82)
        binding = self._import(order)
        sale_order = binding.sale_order_id
        compat.validate_picking(sale_order.picking_ids)
        self.Job.search([('job_type', '=', 'fulfillment.push'), ('state', '=', 'pending')]).write({'state': 'cancelled'})
        compat.set_sale_line_quantity(self._sale_line(binding, order, 0), 2)
        wizard = self._wizard(sale_order)
        self.assertIn('shipped', wizard.blocked_reason)
        with self.assertRaises(UserError):
            wizard.action_send()

    def test_a_refused_edit_is_explained_and_changes_nothing(self):
        order = make_order(number=83)
        binding = self._import(order)
        compat.set_sale_line_quantity(self._sale_line(binding, order, 0), 2)
        self.shop.order_edit_refuse = True
        self._wizard(binding.sale_order_id).action_send()
        self._run_pending(order['id'])
        self.assertIn('refused', binding.reason)
        self.assertFalse(self.shop.order_edits)
        self.assertEqual(binding.line_ids.filtered(lambda line: line.kind == 'product').quantity, 1)

    def test_products_not_sold_in_shopify_are_listed_and_not_sent(self):
        order = make_order(number=84)
        binding = self._import(order)
        self._add_odoo_line(binding.sale_order_id, self.products[2], 1)
        wizard = self._wizard(binding.sale_order_id)
        self.assertEqual([(line.action, line.note) for line in wizard.line_ids], [('skip', 'Not sold in Shopify.')])
        self.assertTrue(wizard.blocked_reason)

    def test_a_variant_already_on_the_order_is_never_added_twice(self):
        order = make_order(number=85)
        binding = self._import(order)
        self._variant(0)
        self._add_odoo_line(binding.sale_order_id, self.products[0], 1)
        wizard = self._wizard(binding.sale_order_id)
        self.assertEqual([line.action for line in wizard.line_ids], ['skip'])
        self.assertIn('Already on the Shopify order', wizard.line_ids.note)
        self.assertTrue(wizard.blocked_reason)

    def test_a_lost_commit_answer_is_checked_so_the_order_comes_back_once(self):
        order = make_order(number=86)
        binding = self._import(order)
        first = self._sale_line(binding, order, 0)
        compat.set_sale_line_quantity(first, 3)
        self.shop.order_edit_commit_lost = True
        self._wizard(binding.sale_order_id).action_send()
        self._run_pending(order['id'])
        self.assertEqual(len(self.shop.order_edits), 1)
        self.assertEqual(binding.line_ids.filtered(lambda line: line.kind == 'product').quantity, 3)
        self.assertEqual(first.product_uom_qty, 3, 'Shopify already holding 3 does not add 2 more in Odoo')
        self.assertEqual(len(binding.sale_order_id.order_line), 1)

    def test_a_timeout_before_the_commit_is_tried_again(self):
        order = make_order(number=87)
        binding = self._import(order)
        compat.set_sale_line_quantity(self._sale_line(binding, order, 0), 2)
        self.shop.order_edit_begin_timeout = True
        self._wizard(binding.sale_order_id).action_send()
        push = self.Job.search([('job_type', '=', PUSH), ('resource_key', '=', order['id']), ('state', '=', 'pending')])
        push._run_inline()
        self.assertNotEqual(push.state, 'done', 'an edit that never reached the commit is not reported as finished')
        self.assertFalse(self.shop.order_edits)
        push._run_inline()
        self.assertEqual(push.state, 'done', push.last_error)
        self.assertEqual(len(self.shop.order_edits), 1)

    def test_changes_sent_while_an_edit_runs_are_sent_after_it(self):
        order = make_order(number=88)
        binding = self._import(order)
        line = self._sale_line(binding, order, 0)
        compat.set_sale_line_quantity(line, 2)
        self._wizard(binding.sale_order_id).action_send()
        running = self.Job.search([('job_type', '=', PUSH), ('resource_key', '=', order['id'])])
        running.state = 'running'
        compat.set_sale_line_quantity(line, 4)
        self._wizard(binding.sale_order_id).action_send()
        waiting = self.Job.search([('job_type', '=', PUSH), ('resource_key', '=', order['id']), ('state', '=', 'pending')])
        self.assertEqual(len(waiting), 1, 'the second send is queued behind the running edit')
        running.state = 'cancelled'
        self._run_pending(order['id'])
        items = self.shop.orders[order['id']]['_all']['lineItems']
        self.assertEqual(items[0]['currentQuantity'], 4)

    def test_units_refunded_before_the_edit_are_not_sent_back(self):
        order = make_order(number=89, lines=1)
        item = order['_all']['lineItems'][0]
        item['quantity'] = item['currentQuantity'] = 3
        for key in ('currentTotalPriceSet', 'totalPriceSet'):
            order[key] = bag('30.00')
        order['transactions'][0]['amountSet'] = bag('30.00')
        binding = self._import(order)
        line = self._sale_line(binding, order, 0)
        add_refund(order, 891, [(0, 1)], restock='CANCEL')
        self._import(order)
        self._run_pending(order['id'])
        refund = binding.refund_ids
        self.assertEqual(refund.state, 'pending_invoice', 'a refund on an order not invoiced waits for the invoice')
        self.assertEqual(line.product_uom_qty, 3, 'the Odoo order still shows the unit Shopify removed')
        self.assertEqual(binding.line_ids.filtered(lambda l: l.shopify_gid == item['id']).quantity, 3,
                         'the link keeps the as sold basis')
        self._variant(2)
        self._add_odoo_line(binding.sale_order_id, self.products[2], 1)
        wizard = self._wizard(binding.sale_order_id)
        self.assertIn('refund', wizard.blocked_reason,
                      'sending from here would put the refunded unit back on the Shopify order')
        self.assertFalse(wizard.line_ids)
        with self.assertRaises(UserError):
            wizard.action_send()
        self.assertFalse(self.shop.order_edits)
        self.assertEqual(item['currentQuantity'], 2)
