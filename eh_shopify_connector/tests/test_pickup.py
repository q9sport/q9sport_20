# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_fulfillment import fulfillment_order
from .test_order_import import make_order
from .test_order_sale import ensure_chart

MAIN = 'gid://shopify/Location/1'


def pickup_order(number):
    order = make_order(number=number, lines=1)
    fulfillment = fulfillment_order(order, number)
    fulfillment['deliveryMethod'] = {'methodType': 'PICK_UP', 'serviceCode': None, 'presentedName': 'Pickup'}
    order['_all']['fulfillmentOrders'] = [fulfillment]
    return order, fulfillment


@tagged('post_install', '-at_install', 'eh_shopify')
class TestPickup(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.env['product.product'].create([{'name': 'Brass gear %s' % i, 'default_code': 'SKU-%s' % i}
                                           for i in range(2)])
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Job = self.env['eh.shopify.job']
        Location = self.env['eh.shopify.location']
        vals = {'store_id': self.store.id, 'name': 'Main warehouse', 'shopify_gid': MAIN, 'active_in_shopify': True,
                'warehouse_id': self.warehouse.id, 'delivery_mode': 'odoo', 'removed_in_shopify': False}
        self.location = Location.search([('store_id', '=', self.store.id), ('shopify_gid', '=', MAIN)])
        if self.location:
            self.location.write(vals)
        else:
            self.location = Location.create(vals)

    def _import(self, order):
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def _pickup_job(self, picking):
        return self.Job.search([('job_type', '=', 'pickup.ready'), ('res_model', '=', 'stock.picking'),
                                ('res_id', '=', picking.id)])

    def test_ready_for_pickup_tells_shopify_and_collection_sends_no_email(self):
        order, fulfillment = pickup_order(101)
        binding = self._import(order)
        self.assertTrue(binding.is_pickup)
        picking = binding.sale_order_id.picking_ids
        self.assertTrue(picking.eh_shopify_is_pickup)
        self.assertFalse(self._pickup_job(picking), 'nothing is sent until someone says it is ready')
        picking.action_eh_shopify_ready_for_pickup()
        job = self._pickup_job(picking)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(self.shop.pickup_ready, [fulfillment['id']])
        self.assertTrue(picking.eh_shopify_pickup_ready_at)
        if compat.carrier_fields_available(self.env):
            picking.carrier_tracking_ref = 'SHOULD-NOT-TRAVEL'
        compat.validate_picking(picking)
        push = self.Job.search([('job_type', '=', 'fulfillment.push'), ('res_id', '=', picking.id)])
        push._run_inline()
        self.assertEqual(push.state, 'done', push.last_error)
        created = self.shop.fulfillments[0]
        self.assertFalse(created['notifyCustomer'])
        self.assertFalse(created['trackingInfo'])

    def test_location_can_mark_pickups_ready_when_reserved(self):
        self.location.pickup_ready_when_reserved = True
        order, fulfillment = pickup_order(102)
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        if picking.state != 'assigned':
            picking.action_assign()
        self.assertEqual(picking.state, 'assigned')
        job = self._pickup_job(picking)
        self.assertEqual(len(job), 1)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(self.shop.pickup_ready, [fulfillment['id']])

    def test_shipping_orders_are_never_marked_ready(self):
        self.location.pickup_ready_when_reserved = True
        order = make_order(number=103, lines=1)
        order['_all']['fulfillmentOrders'] = [fulfillment_order(order, 103)]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        self.assertFalse(binding.is_pickup)
        self.assertFalse(picking.eh_shopify_is_pickup)
        picking.action_eh_shopify_ready_for_pickup()
        self.assertFalse(self._pickup_job(picking))

    def test_ready_marked_in_shopify_is_recorded_on_the_delivery(self):
        order, fulfillment = pickup_order(104)
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        event = self.env['eh.shopify.event'].new({
            'store_id': self.store.id, 'topic': 'fulfillment_orders/line_items_prepared_for_pickup',
            'webhook_id': 'w-104', 'payload': {'fulfillment_order': {'id': fulfillment['id']}}})
        self.assertEqual(event._on_prepared_for_pickup()['ready'], [picking.name])
        self.assertTrue(picking.eh_shopify_pickup_ready_at)

    def test_stock_edited_in_shopify_is_queued_for_correction(self):
        vals = {'store_id': self.store.id, 'shopify_gid': 'gid://shopify/ProductVariant/0',
                'inventory_item_gid': 'gid://shopify/InventoryItem/0', 'sku': 'SKU-0', 'match_method': 'sku',
                'product_id': self.env['product.product'].search([('default_code', '=', 'SKU-0')], limit=1).id}
        Variant = self.env['eh.shopify.variant']
        variant = Variant.search([('store_id', '=', self.store.id), ('shopify_gid', '=', vals['shopify_gid'])])
        if variant:
            variant.write(vals)
        else:
            variant = Variant.create(vals)
        self.env['eh.shopify.inventory.dirty'].search([]).unlink()
        event = self.env['eh.shopify.event'].new({
            'store_id': self.store.id, 'topic': 'inventory_levels/update', 'webhook_id': 'w-inv',
            'payload': {'inventory_item_id': 0, 'location_id': 1, 'available': 99}})
        self.assertEqual(event._on_inventory_level_update()['queued'], variant.product_id.ids)
        self.assertTrue(self.env['eh.shopify.inventory.dirty'].search([('product_id', '=', variant.product_id.id)]))
        self.assertIn('INVENTORY_LEVELS_UPDATE', self.store._webhook_topics())
        self.store.inventory_enabled = False
        self.assertNotIn('INVENTORY_LEVELS_UPDATE', self.store._webhook_topics())
