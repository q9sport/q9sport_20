# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import copy

from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import bag, make_order
from .test_order_sale import ensure_chart


@tagged('post_install', '-at_install', 'eh_shopify')
class TestOrderEdits(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.bank = cls.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        cls.env['product.product'].create([{'name': 'Edit gear %s' % i, 'default_code': 'SKU-%s' % i} for i in range(3)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Job = self.env['eh.shopify.job']
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})
        self.stamp = 0

    def _import(self, order):
        if order['id'] not in self.shop.orders:
            self.shop.add_order(order)
        else:
            self.stamp += 1
            order['updatedAt'] = '2026-09-16T10:%02d:00Z' % self.stamp
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    @staticmethod
    def _total(order, amount):
        for key in ('currentTotalPriceSet', 'totalPriceSet'):
            order[key] = bag('%.2f' % amount)

    def _order_of_two(self, number):
        order = make_order(number=number, lines=1)
        item = order['_all']['lineItems'][0]
        item['quantity'] = item['currentQuantity'] = 2
        self._total(order, 20.0)
        order['transactions'][0]['amountSet'] = bag('20.00')
        return order

    def _sale_line(self, binding, gid):
        return binding.line_ids.filtered(lambda l: l.shopify_gid == gid).sale_line_id

    def test_a_quantity_lowered_before_shipping_follows_in_odoo(self):
        order = self._order_of_two(91)
        binding = self._import(order)
        item = order['_all']['lineItems'][0]
        sale_line = self._sale_line(binding, item['id'])
        self.assertEqual(sale_line.product_uom_qty, 2.0)
        item['currentQuantity'] = 1
        self._total(order, 10.0)
        self._import(order)
        self.assertEqual(sale_line.product_uom_qty, 1.0)
        self.assertEqual(binding.line_ids.filtered(lambda l: l.shopify_gid == item['id']).quantity, 1)
        moves = binding.sale_order_id.picking_ids.move_ids.filtered(
            lambda m: m.sale_line_id == sale_line and m.state != 'cancel')
        self.assertEqual(sum(moves.mapped('product_uom_qty')), 1.0)
        self.assertEqual(sale_line.price_unit, 10.0, 'the Shopify price stays when the quantity changes')
        self.assertEqual(binding.parity_state, 'ok', binding.reason)

    def test_an_item_added_in_shopify_is_added_to_the_order(self):
        order = make_order(number=92, lines=1)
        binding = self._import(order)
        added = copy.deepcopy(order['_all']['lineItems'][0])
        added.update({'id': 'gid://shopify/LineItem/92999', 'sku': 'SKU-1', 'name': 'Item 1',
                      'variant': {'id': 'gid://shopify/ProductVariant/1', 'sku': 'SKU-1', 'barcode': None,
                                  'inventoryItem': {'id': 'gid://shopify/InventoryItem/1'}},
                      'product': {'id': 'gid://shopify/Product/1'}})
        order['_all']['lineItems'].append(added)
        self._total(order, 20.0)
        self._import(order)
        sale_line = self._sale_line(binding, added['id'])
        self.assertEqual((sale_line.product_id.default_code, sale_line.product_uom_qty), ('SKU-1', 1.0))
        self.assertEqual(sale_line.order_id, binding.sale_order_id)
        self.assertEqual(binding.parity_state, 'ok', binding.reason)
        self._import(order)
        self.assertEqual(len(binding.line_ids.filtered(lambda l: l.shopify_gid == added['id'])), 1, 'added once')

    def test_an_edit_after_shipping_is_left_to_a_person(self):
        order = self._order_of_two(93)
        binding = self._import(order)
        compat.validate_picking(binding.sale_order_id.picking_ids)
        self.Job.search([('job_type', '=', 'fulfillment.push'), ('state', '=', 'pending')]).write({'state': 'cancelled'})
        item = order['_all']['lineItems'][0]
        item['currentQuantity'] = 1
        self._total(order, 10.0)
        self._import(order)
        self.assertEqual(self._sale_line(binding, item['id']).product_uom_qty, 2.0)
        self.assertIn('after it was shipped or invoiced', binding.reason)
        self._import(order)
        todos = binding.sale_order_id.activity_ids.filtered(lambda activity: activity.summary == 'Shopify order edited')
        self.assertEqual(len(todos), 1, 'one to-do however often the order updates')
