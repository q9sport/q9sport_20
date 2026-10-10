# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart

ITEM = 'gid://shopify/InventoryItem/0'
LOCATION = 'gid://shopify/Location/1'
KEY = (ITEM, LOCATION)


@tagged('post_install', '-at_install', 'eh_shopify')
class TestInventoryPush(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        vals = {'name': 'Stocked gear', 'default_code': 'SKU-0', 'list_price': 10.0}
        vals.update(compat.product_type_vals(cls.env, 'storable'))
        cls.product = cls.env['product.template'].create(vals).product_variant_id
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        cls.stock = cls.warehouse.lot_stock_id

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        Variant = self.env['eh.shopify.variant']
        vals = {'store_id': self.store.id, 'shopify_gid': 'gid://shopify/ProductVariant/0',
                'inventory_item_gid': ITEM, 'product_id': self.product.id, 'sku': 'SKU-0', 'match_method': 'sku'}
        self.variant = Variant.search([('store_id', '=', self.store.id), ('shopify_gid', '=', vals['shopify_gid'])])
        self.variant = self.variant.write(vals) and self.variant if self.variant else Variant.create(vals)
        Location = self.env['eh.shopify.location']
        Location.search([('store_id', '=', self.store.id)]).write({'sync_stock': False})
        vals = {'store_id': self.store.id, 'name': 'Main', 'shopify_gid': LOCATION, 'active_in_shopify': True,
                'warehouse_id': self.warehouse.id, 'location_ids': [(6, 0, self.stock.ids)],
                'delivery_mode': 'odoo', 'sync_stock': True, 'removed_in_shopify': False}
        self.location = Location.search([('store_id', '=', self.store.id), ('shopify_gid', '=', LOCATION)])
        self.location = self.location.write(vals) and self.location if self.location else Location.create(vals)
        self.shop.inventory[KEY] = 3

    # ------------------------------------------------------------------ helpers
    def _set_on_hand(self, quantity):
        self.env.flush_all()
        product = self.product.with_context(location=self.stock.ids)
        product.invalidate_recordset(['qty_available'])
        self.env['stock.quant']._update_available_quantity(self.product, self.stock,
                                                           quantity - product.qty_available)
        self.env.flush_all()

    def _calls(self, operation, start=0):
        return [r['payload'] for r in self.fake.requests[start:]
                if 'payload' in r and operation in r['payload'].get('query', '')]

    def _level(self):
        return self.env['eh.shopify.inventory.level'].search([('variant_binding_id', '=', self.variant.id),
                                                               ('location_binding_id', '=', self.location.id)])

    # ------------------------------------------------------------------ tests
    def test_first_push_reads_then_sets_compare_and_set(self):
        self._set_on_hand(10)
        totals = self.store._push_inventory()
        self.assertEqual((totals['sent'], totals['failed']), (1, 0))
        self.assertEqual(self.shop.inventory[KEY], 10)
        sent = self._calls('EhInventorySet')[-1]['variables']
        quantity = sent['input']['quantities'][0]
        self.assertEqual((quantity['quantity'], quantity['changeFromQuantity']), (10, 3))
        self.assertTrue(sent['idempotencyKey'])
        level = self._level()
        self.assertEqual((level.state, level.target_quantity, level.shopify_available), ('synced', 10, 10))

    def test_safety_stock_and_unchanged_quantity_is_not_sent(self):
        self._set_on_hand(10)
        self.store.inventory_safety_stock = 4
        self.store._push_inventory()
        self.assertEqual(self.shop.inventory[KEY], 6)
        start = len(self.fake.requests)
        totals = self.store._push_inventory()
        self.assertFalse(self._calls('EhInventorySet', start), 'an unchanged quantity is not sent again')
        self.assertEqual(totals['unchanged'], 1)

    def test_shopify_sale_not_yet_imported_is_not_offered_again(self):
        self._set_on_hand(10)
        self.store._push_inventory()
        self.shop.inventory[KEY] = 9
        self.shop.inventory_committed[KEY] = 1
        start = len(self.fake.requests)
        self.store._push_inventory()
        self.assertEqual(self.shop.inventory[KEY], 9)
        self.assertFalse(self._calls('EhInventorySet', start))

    def test_sale_during_the_update_is_kept(self):
        self._set_on_hand(10)

        def shopify_sale():
            self.shop.inventory[KEY] -= 1
            self.shop.inventory_committed[KEY] = self.shop.inventory_committed.get(KEY, 0) + 1
        self.shop.after_inventory_read = shopify_sale
        self.store._push_inventory()
        self.assertEqual(self.shop.inventory[KEY], 9)
        sets = self._calls('EhInventorySet')
        self.assertEqual(len(sets), 2, 'the refused write is read again and recomputed')
        self.assertEqual(sets[1]['variables']['input']['quantities'][0]['changeFromQuantity'], 2)
        self.assertNotEqual(sets[0]['variables']['idempotencyKey'], sets[1]['variables']['idempotencyKey'])
        self.assertEqual(self._level().state, 'synced')

    def test_imported_order_reservation_is_not_counted_twice(self):
        self._set_on_hand(10)
        order = make_order(number=61, lines=1)
        self.shop.add_order(order)
        job = self.env['eh.shopify.order']._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        binding = self.env['eh.shopify.order'].search([('store_id', '=', self.store.id),
                                                       ('shopify_gid', '=', order['id'])])
        self.assertTrue(binding.sale_order_id.picking_ids)
        self.shop.inventory[KEY] = 9
        self.shop.inventory_committed[KEY] = 1
        start = len(self.fake.requests)
        self.store._push_inventory()
        level = self._level()
        self.assertEqual(level.odoo_reserved_for_store, 1.0)
        self.assertEqual(level.odoo_free, 9.0)
        self.assertEqual(level.target_quantity, 9)
        self.assertEqual(self.shop.inventory[KEY], 9)
        self.assertFalse(self._calls('EhInventorySet', start))

    def test_item_not_stocked_is_activated_with_a_key(self):
        del self.shop.inventory[KEY]
        self._set_on_hand(5)
        totals = self.store._push_inventory()
        self.assertEqual(totals['sent'], 1)
        self.assertEqual(self.shop.inventory[KEY], 5)
        self.assertTrue(self._calls('EhInventoryActivate')[0]['variables']['idempotencyKey'])

    def test_untracked_and_missing_items_are_explained(self):
        self._set_on_hand(5)
        self.shop.untracked_items.add(ITEM)
        self.store._push_inventory()
        self.assertEqual(self._level().state, 'untracked')
        self.assertFalse(self._calls('EhInventorySet'))
        self.shop.untracked_items.clear()
        self.shop.missing_items.add(ITEM)
        totals = self.store._push_inventory()
        self.assertEqual((totals['failed'], self._level().state), (1, 'error'))

    def test_locations_not_sending_stock_are_ignored(self):
        self.location.sync_stock = False
        self._set_on_hand(7)
        self.assertEqual(self.store._push_inventory()['pairs'], 0)
        self.assertEqual(self.shop.inventory[KEY], 3)

    def test_stock_move_queues_rows_and_one_job_drains_them(self):
        Queue = self.env['eh.shopify.inventory.dirty']
        suppliers = self.env.ref('stock.stock_location_suppliers')
        move = {'name': 'Receive gear', 'product_id': self.product.id, 'product_uom_qty': 4,
                'uom_id': self.product.uom_id.id, 'location_id': suppliers.id,
                'location_dest_id': self.stock.id}
        move = {key: value for key, value in move.items() if key in self.env['stock.move']._fields}
        receipt = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.in_type_id.id, 'location_id': suppliers.id,
            'location_dest_id': self.stock.id, 'move_ids': [(0, 0, move)]})
        compat.validate_picking(receipt)
        self.assertEqual(receipt.state, 'done')
        self.assertTrue(Queue.search([('store_id', '=', self.store.id), ('product_id', '=', self.product.id)]))
        Store = self.env['eh.shopify.store']
        Store._schedule_inventory_flushes()
        Store._schedule_inventory_flushes()
        jobs = self.env['eh.shopify.job'].search([('store_id', '=', self.store.id),
                                                  ('job_type', '=', 'inventory.flush')])
        self.assertEqual(len(jobs), 1)
        jobs._run_inline()
        self.assertEqual(jobs.state, 'done', jobs.last_error)
        self.assertFalse(Queue.search([('store_id', '=', self.store.id)]))
        self.assertEqual(self.shop.inventory[KEY], 4)

    def test_switched_off_store_clears_its_queue(self):
        Queue = self.env['eh.shopify.inventory.dirty']
        self.assertTrue(self.env['eh.shopify.store']._mark_inventory_dirty(self.product))
        self.store.inventory_enabled = False
        self.env['eh.shopify.store']._schedule_inventory_flushes()
        self.assertFalse(Queue.search([('store_id', '=', self.store.id)]))
        self.assertFalse(self.env['eh.shopify.job'].search([('store_id', '=', self.store.id),
                                                            ('job_type', '=', 'inventory.flush')]))

    def test_daily_check_queues_every_linked_product(self):
        self.store.inventory_reconciled_at = False
        self.store._cron_maintenance()
        self.assertTrue(self.store.inventory_reconciled_at)
        self.assertTrue(self.env['eh.shopify.inventory.dirty'].search([('store_id', '=', self.store.id),
                                                                       ('product_id', '=', self.product.id)]))

    def test_only_managers_can_queue_a_full_stock_check(self):
        user = self._user('eh_stock_viewer', ['eh_shopify_connector.group_shopify_user'])
        with self.assertRaises(AccessError):
            self.store.with_user(user).action_push_inventory()

    def test_link_overrides_share_cap_fixed_and_off(self):
        self._set_on_hand(10)
        self.variant.stock_share = 50
        self.store._push_inventory()
        self.assertEqual(self.shop.inventory[KEY], 5)
        self.variant.stock_max = 3
        self.store._push_inventory()
        self.assertEqual(self.shop.inventory[KEY], 3)
        self.shop.inventory_committed[KEY] = 1
        self.variant.write({'stock_policy': 'fixed', 'stock_fixed': 7})
        self.store._push_inventory()
        self.assertEqual(self.shop.inventory[KEY], 7, 'a fixed quantity ignores Odoo stock and Shopify orders')
        self.variant.stock_policy = 'off'
        start = len(self.fake.requests)
        self.assertEqual(self.store._push_inventory()['pairs'], 0)
        self.assertFalse(self._calls('EhInventorySet', start))

    def test_changing_a_stock_rule_queues_the_product(self):
        Queue = self.env['eh.shopify.inventory.dirty']
        Queue.search([]).unlink()
        self.variant.stock_share = 80
        self.assertTrue(Queue.search([('store_id', '=', self.store.id), ('product_id', '=', self.product.id)]))

    def test_stock_rules_are_validated(self):
        with self.assertRaises(ValidationError):
            self.variant.stock_share = 150
        with self.assertRaises(ValidationError):
            self.variant.stock_max = -1

    def test_waiting_retry_keeps_its_backoff_and_a_dead_flush_cools_down(self):
        Store = self.env['eh.shopify.store']
        Job = self.env['eh.shopify.job']
        flushes = [('store_id', '=', self.store.id), ('dedup_key', '=', 'inventory.flush')]
        Store._mark_inventory_dirty(self.product)
        Store._schedule_inventory_flushes()
        job = Job.search(flushes)
        self.assertEqual(len(job), 1)
        later = fields.Datetime.now() + datetime.timedelta(minutes=10)
        job.write({'next_run_at': later})
        Store._schedule_inventory_flushes()
        self.assertEqual(job.next_run_at, later, 'a waiting retry is not pulled forward')
        job.write({'state': 'dead', 'finished_at': fields.Datetime.now()})
        Store._schedule_inventory_flushes()
        self.assertEqual(Job.search_count(flushes), 1, 'no new attempt right after giving up')
        job.write({'finished_at': fields.Datetime.now() - datetime.timedelta(hours=2)})
        Store._schedule_inventory_flushes()
        self.assertEqual(Job.search_count(flushes + [('state', '=', 'pending')]), 1, 'tried again after the cool down')

    def test_reenabling_sync_or_changing_safety_stock_queues_a_full_check(self):
        Queue = self.env['eh.shopify.inventory.dirty']
        mine = [('store_id', '=', self.store.id), ('product_id', '=', self.product.id)]
        self.store.inventory_enabled = False
        Queue.search([]).unlink()
        self.store.inventory_enabled = True
        self.assertTrue(Queue.search(mine + [('reconcile', '=', True)]))
        Queue.search([]).unlink()
        self.store.inventory_safety_stock = 3
        self.assertTrue(Queue.search(mine))

    def test_move_line_changes_queue_the_product(self):
        Queue = self.env['eh.shopify.inventory.dirty']
        self._set_on_hand(5)
        customers = self.env.ref('stock.stock_location_customers')
        move = {'name': 'Ship gear', 'product_id': self.product.id, 'product_uom_qty': 2,
                'uom_id': self.product.uom_id.id, 'location_id': self.stock.id, 'location_dest_id': customers.id}
        move = {key: value for key, value in move.items() if key in self.env['stock.move']._fields}
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.out_type_id.id, 'location_id': self.stock.id,
            'location_dest_id': customers.id, 'move_ids': [(0, 0, move)]})
        picking.action_confirm()
        picking.action_assign()
        self.assertTrue(picking.move_ids.move_line_ids)
        Queue.search([]).unlink()
        picking.move_ids.move_line_ids.unlink()
        self.assertTrue(Queue.search([('product_id', '=', self.product.id)]))
