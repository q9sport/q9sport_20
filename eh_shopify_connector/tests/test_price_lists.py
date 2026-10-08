# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import ShopifyCase
from .test_catalogue import shopify_product


@tagged('post_install', '-at_install', 'eh_shopify')
class TestPriceLists(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gear = cls.env['product.product'].create({'name': 'Price list gear', 'default_code': 'PL-1',
                                                      'list_price': 50.0, 'taxes_id': [(6, 0, [])]})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.products_enabled = True
        self.currency = self.env.company.currency_id
        self.Job = self.env['eh.shopify.job']
        self.PriceList = self.env['eh.shopify.price.list']
        product = shopify_product(501, [('PL-1', None)])
        self.shop.add_product(product)
        job = self.env['eh.shopify.product']._enqueue_product_import(self.store, product['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.variant_gid = product['_variants'][0]['id']

    def _sync(self):
        job = self.Job._enqueue(self.store, 'price_lists.sync', dedup_key='price_lists.sync')
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return job

    def _pricelist(self, price):
        return self.env['product.pricelist'].create({
            'name': 'Wholesale pricelist', 'currency_id': self.currency.id,
            'item_ids': [(0, 0, {'applied_on': '0_product_variant', 'product_tmpl_id': self.gear.product_tmpl_id.id,
                                 'product_id': self.gear.id, 'compute_price': 'fixed', 'fixed_price': price,
                                 'min_quantity': 0})]})

    def _run_pushes(self):
        pushes = self.Job.search([('job_type', '=', 'price_list.push'), ('state', '=', 'pending')], order='id')
        for push in pushes:
            push._run_inline()
            self.assertEqual(push.state, 'done', push.last_error)
        return pushes

    def test_a_mapped_pricelist_sends_only_prices_that_differ(self):
        shopify_list = self.shop.add_price_list(1, 'Wholesale', self.currency.name)
        self._sync()
        price_list = self.PriceList.search([('store_id', '=', self.store.id)])
        self.assertEqual((price_list.name, price_list.currency_code, price_list.catalog_title),
                         ('Wholesale', self.currency.name, 'Wholesale catalog'))
        pricelist = self._pricelist(40.0)
        price_list.pricelist_id = pricelist
        self.assertEqual(len(self._run_pushes()), 1, 'mapping a pricelist sends its prices')
        self.assertEqual(shopify_list['_prices'][self.variant_gid]['price']['amount'], '40.00')
        price_list._enqueue_push()
        self._run_pushes()
        self.assertEqual(len(self.shop.price_list_updates), 1, 'unchanged prices are not sent again')
        pricelist.item_ids.fixed_price = 35.0
        self._run_pushes()
        self.assertEqual(shopify_list['_prices'][self.variant_gid]['price']['amount'], '35.00',
                         'a rule change is sent')
        self.assertTrue(price_list.sent_at)

    def test_a_pricelist_in_another_currency_is_refused(self):
        other = 'EUR' if self.currency.name != 'EUR' else 'USD'
        self.shop.add_price_list(2, 'Europe', other)
        self._sync()
        price_list = self.PriceList.search([('store_id', '=', self.store.id)])
        with self.assertRaises(ValidationError):
            price_list.pricelist_id = self._pricelist(40.0)

    def test_a_price_list_removed_in_shopify_is_marked_and_skipped(self):
        shopify_list = self.shop.add_price_list(3, 'Retired', self.currency.name)
        self._sync()
        price_list = self.PriceList.search([('store_id', '=', self.store.id)])
        del self.shop.price_lists[shopify_list['id']]
        self._sync()
        self.assertTrue(price_list.removed_in_shopify)
        price_list.pricelist_id = self._pricelist(40.0)
        self.assertFalse(self.Job.search([('job_type', '=', 'price_list.push'), ('state', '=', 'pending')]))

    def test_prices_are_rechecked_daily(self):
        self.shop.add_price_list(4, 'Daily', self.currency.name)
        self._sync()
        price_list = self.PriceList.search([('store_id', '=', self.store.id)])
        price_list.pricelist_id = self._pricelist(40.0)
        self._run_pushes()
        self.env.cr.execute("UPDATE eh_shopify_price_list SET sent_at = %s WHERE id = %s",
                            (fields.Datetime.now() - datetime.timedelta(days=2), price_list.id))
        price_list.invalidate_recordset(['sent_at'])
        self.env['eh.shopify.store']._cron_maintenance()
        self.assertTrue(self.Job.search([('job_type', '=', 'price_list.push'), ('state', '=', 'pending')]))

    def test_a_compare_at_price_is_sent_from_the_sales_price(self):
        shopify_list = self.shop.add_price_list(4, 'Wholesale', self.currency.name)
        self._sync()
        price_list = self.PriceList.search([('store_id', '=', self.store.id)])
        price_list.write({'pricelist_id': self._pricelist(40.0).id, 'compare_at_source': 'sales_price'})
        self._run_pushes()
        stored = shopify_list['_prices'][self.variant_gid]
        self.assertEqual((stored['price']['amount'], stored['compareAtPrice']['amount']), ('40.00', '50.00'))
        price_list.action_send_now()
        self._run_pushes()
        self.assertEqual(len(self.shop.price_list_updates), 1, 'an unchanged compare at price is not sent again')
        self.gear.product_tmpl_id.list_price = 60.0
        self._run_pushes()
        self.assertEqual(shopify_list['_prices'][self.variant_gid]['compareAtPrice']['amount'], '60.00',
                         'a new sales price is sent as the compare at price')

    def test_quantity_tiers_in_odoo_become_shopify_quantity_breaks(self):
        shopify_list = self.shop.add_price_list(5, 'Wholesale', self.currency.name)
        self._sync()
        pricelist = self._pricelist(40.0)
        pricelist.write({'item_ids': [(0, 0, {'applied_on': '0_product_variant',
                                              'product_tmpl_id': self.gear.product_tmpl_id.id,
                                              'product_id': self.gear.id, 'compute_price': 'fixed',
                                              'fixed_price': 32.0, 'min_quantity': 10})]})
        price_list = self.PriceList.search([('store_id', '=', self.store.id)])
        price_list.write({'pricelist_id': pricelist.id, 'send_quantity_breaks': True})
        self._run_pushes()
        self.assertEqual({quantity: money['amount']
                          for quantity, money in shopify_list['_breaks'][self.variant_gid].items()}, {10: '32.00'})
        price_list.action_send_now()
        self._run_pushes()
        self.assertEqual(len(self.shop.quantity_pricing_updates), 1, 'unchanged breaks are not sent again')
        pricelist.item_ids.filtered(lambda item: item.min_quantity == 10).unlink()
        self._run_pushes()
        self.assertFalse(shopify_list['_breaks'].get(self.variant_gid),
                         'a tier removed in Odoo is removed in Shopify')

    def test_breaks_in_shopify_are_left_alone_when_odoo_does_not_send_them(self):
        shopify_list = self.shop.add_price_list(6, 'Wholesale', self.currency.name)
        shopify_list['_breaks'][self.variant_gid] = {12: {'amount': '30.00', 'currencyCode': self.currency.name}}
        self._sync()
        price_list = self.PriceList.search([('store_id', '=', self.store.id)])
        price_list.pricelist_id = self._pricelist(40.0)
        self._run_pushes()
        self.assertFalse(self.shop.quantity_pricing_updates)
        self.assertEqual(shopify_list['_breaks'][self.variant_gid][12]['amount'], '30.00')
