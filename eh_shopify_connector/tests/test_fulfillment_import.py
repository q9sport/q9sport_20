# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import fields
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart

LOCATION = 'gid://shopify/Location/1'


def shopify_fulfillment(order, number, lines=None, status='SUCCESS', location=LOCATION, tracking=None):
    items = order['_all']['lineItems']
    indexes = range(len(items)) if lines is None else lines
    return {'id': 'gid://shopify/Fulfillment/%s%02d' % (order['legacyResourceId'], number),
            'name': '%s-F%s' % (order['name'], number), 'status': status,
            'createdAt': '2026-09-15T12:00:%02dZ' % number, 'updatedAt': '2026-09-15T12:00:%02dZ' % number,
            'location': {'id': location}, 'trackingInfo': [tracking] if tracking else [],
            '_lines': [{'id': 'gid://shopify/FulfillmentLineItem/%s%02d' % (number, index),
                        'quantity': items[index]['quantity'], 'lineItem': {'id': items[index]['id']}}
                       for index in indexes]}


@tagged('post_install', '-at_install', 'eh_shopify')
class TestFulfillmentImport(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.env['product.product'].create([{'name': 'Brass gear %s' % i, 'default_code': 'SKU-%s' % i}
                                           for i in range(3)])
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Job = self.env['eh.shopify.job']
        Location = self.env['eh.shopify.location']
        vals = {'store_id': self.store.id, 'name': 'Main', 'shopify_gid': LOCATION, 'active_in_shopify': True,
                'warehouse_id': self.warehouse.id, 'delivery_mode': 'shopify', 'sync_stock': False}
        self.location = Location.search([('store_id', '=', self.store.id), ('shopify_gid', '=', LOCATION)])
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

    def _run_import(self, binding):
        job = self.Order._enqueue_fulfillment_import(self.store, binding.shopify_gid)
        job._run_inline()
        return job

    def test_shopify_shipment_validates_the_odoo_delivery(self):
        order = make_order(number=71, lines=2)
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        self.assertEqual(len(picking), 1)
        order['_all']['fulfillments'] = [shopify_fulfillment(
            order, 1, tracking={'company': 'UPS', 'number': '1Z999', 'url': None})]
        job = self._run_import(binding)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(picking.state, 'done')
        record = binding.fulfillment_ids
        self.assertEqual((len(record), record.origin, record.picking_id), (1, 'shopify', picking))
        if compat.carrier_fields_available(self.env):
            self.assertEqual(picking.carrier_tracking_ref, '1Z999')
        self.assertFalse(self.Job.search([('job_type', '=', 'fulfillment.push'), ('res_model', '=', 'stock.picking'),
                                          ('res_id', '=', picking.id)]))
        self.assertFalse(self.shop.fulfillments, 'nothing is sent back to Shopify')
        products = binding.line_ids.filtered(lambda line: line.kind == 'product')
        self.assertEqual(products.mapped('fulfilled_quantity'), [1.0, 1.0])

    def test_partial_shipment_leaves_a_backorder_then_ships_it(self):
        order = make_order(number=72, lines=2)
        binding = self._import(order)
        first = binding.sale_order_id.picking_ids
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1, lines=[0])]
        job = self._run_import(binding)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(first.state, 'done')
        backorder = binding.sale_order_id.picking_ids - first
        self.assertEqual(len(backorder), 1)
        self.assertNotEqual(backorder.state, 'done')
        order['_all']['fulfillments'].append(shopify_fulfillment(order, 2, lines=[1]))
        job = self._run_import(binding)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(backorder.state, 'done')
        self.assertEqual(len(binding.fulfillment_ids), 2)

    def test_repeat_import_ships_nothing_twice_and_cancellation_is_flagged(self):
        order = make_order(number=73, lines=1)
        binding = self._import(order)
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1)]
        self._run_import(binding)
        pickings = binding.sale_order_id.picking_ids
        job = self._run_import(binding)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(job.result.get('imported'), [])
        self.assertEqual(binding.sale_order_id.picking_ids, pickings)
        order['_all']['fulfillments'][0]['status'] = 'CANCELLED'
        job = self._run_import(binding)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(binding.fulfillment_ids.status, 'CANCELLED')
        self.assertTrue(binding.sale_order_id.activity_ids)

    def test_shipments_at_locations_odoo_ships_are_ignored(self):
        self.env['eh.shopify.location'].search([('store_id', '=', self.store.id)]).write({'delivery_mode': 'odoo'})
        order = make_order(number=74, lines=1)
        binding = self._import(order)
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1)]
        job = self._run_import(binding)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertIn('skipped', job.result)
        self.assertNotEqual(binding.sale_order_id.picking_ids.state, 'done')

    def test_order_import_chains_the_shipment_import(self):
        order = make_order(number=75, lines=1)
        order['displayFulfillmentStatus'] = 'FULFILLED'
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1)]
        binding = self._import(order)
        job = self.Job.search([('job_type', '=', 'fulfillment.import'), ('store_id', '=', self.store.id)])
        self.assertEqual(len(job), 1)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(binding.sale_order_id.picking_ids.state, 'done')

    def test_fulfillment_webhook_queues_the_right_job(self):
        Event = self.env['eh.shopify.event']
        event = Event.new({'store_id': self.store.id, 'topic': 'fulfillments/create', 'webhook_id': 'w-76',
                           'payload': {'order_id': 76}})
        event._on_fulfillment_change()
        self.assertTrue(self.Job.search([('job_type', '=', 'order.import'),
                                         ('dedup_key', '=', 'order:gid://shopify/Order/76')]))
        self._import(make_order(number=77, lines=1))
        event = Event.new({'store_id': self.store.id, 'topic': 'fulfillments/create', 'webhook_id': 'w-77',
                           'payload': {'order_id': 77}})
        event._on_fulfillment_change()
        self.assertTrue(self.Job.search([('job_type', '=', 'fulfillment.import'),
                                         ('dedup_key', '=', 'fulfillment-import:gid://shopify/Order/77')]))

    def test_lot_tracked_product_is_explained_not_guessed(self):
        vals = {'name': 'Serial gear', 'default_code': 'SKU-9', 'tracking': 'lot'}
        vals.update(compat.product_type_vals(self.env, 'storable'))
        self.env['product.template'].create(vals)
        order = make_order(number=78, lines=1)
        item = order['_all']['lineItems'][0]
        item['sku'] = item['variant']['sku'] = 'SKU-9'
        item['variant']['id'] = 'gid://shopify/ProductVariant/9'
        binding = self._import(order)
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1)]
        job = self._run_import(binding)
        self.assertEqual(job.state, 'failed')
        self.assertIn('lot', job.last_error)
        self.assertNotEqual(binding.sale_order_id.picking_ids.state, 'done')

    def test_fulfillment_topics_need_the_permission(self):
        self.store.granted_scopes = 'read_orders,read_locations'
        self.assertIn('ORDERS_FULFILLED', self.store._webhook_topics())
        self.assertNotIn('FULFILLMENTS_CREATE', self.store._webhook_topics())
        self.store.granted_scopes = 'read_orders,read_fulfillments'
        self.assertIn('FULFILLMENTS_CREATE', self.store._webhook_topics())

    def test_unknown_tracking_company_creates_one_carrier(self):
        if not compat.carrier_fields_available(self.env):
            self.skipTest('carriers are not installed')
        first = make_order(number=79, lines=1)
        binding = self._import(first)
        first['_all']['fulfillments'] = [shopify_fulfillment(
            first, 1, tracking={'company': 'Aramex Express', 'number': 'AX1', 'url': None})]
        self.assertEqual(self._run_import(binding).state, 'done')
        carrier = binding.sale_order_id.picking_ids.carrier_id
        self.assertEqual(carrier.name, 'Aramex Express')
        mapping = self.env['eh.shopify.carrier'].search([('store_id', '=', self.store.id),
                                                         ('tracking_company', '=', 'Aramex Express')])
        self.assertEqual(mapping.carrier_id, carrier)
        second = make_order(number=80, lines=1)
        other = self._import(second)
        second['_all']['fulfillments'] = [shopify_fulfillment(
            second, 1, tracking={'company': 'aramex express', 'number': 'AX2', 'url': None})]
        self.assertEqual(self._run_import(other).state, 'done')
        self.assertEqual(other.sale_order_id.picking_ids.carrier_id, carrier)
        self.assertEqual(self.env['delivery.carrier'].search_count([('name', '=ilike', 'Aramex Express')]), 1)

    def test_carrier_creation_can_be_switched_off(self):
        if not compat.carrier_fields_available(self.env):
            self.skipTest('carriers are not installed')
        self.store.auto_create_carriers = False
        order = make_order(number=89, lines=1)
        binding = self._import(order)
        order['_all']['fulfillments'] = [shopify_fulfillment(
            order, 1, tracking={'company': 'Nowhere Post', 'number': 'NP1', 'url': None})]
        self.assertEqual(self._run_import(binding).state, 'done')
        picking = binding.sale_order_id.picking_ids
        self.assertEqual(picking.state, 'done')
        self.assertFalse(picking.carrier_id)
        self.assertEqual(picking.carrier_tracking_ref, 'NP1')
        self.assertFalse(self.env['delivery.carrier'].search_count([('name', '=', 'Nowhere Post')]))

    def test_tracking_edits_on_shopify_shipments_stay_in_odoo(self):
        if not compat.carrier_fields_available(self.env):
            self.skipTest('carriers are not installed')
        order = make_order(number=90, lines=1)
        binding = self._import(order)
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1)]
        self.assertEqual(self._run_import(binding).state, 'done')
        picking = binding.sale_order_id.picking_ids
        picking.carrier_tracking_ref = 'TYPED-IN-ODOO'
        self.assertFalse(self.Job.search([('job_type', '=', 'fulfillment.tracking')]))

    def test_shipment_from_a_warehouse_without_open_deliveries_is_refused(self):
        other = self.env['stock.warehouse'].create({'name': 'Second warehouse', 'code': 'EH2'})
        self.location.warehouse_id = other
        order = make_order(number=96, lines=1)
        binding = self._import(order)
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1)]
        job = self._run_import(binding)
        self.assertEqual(job.state, 'failed')
        self.assertIn('another warehouse', job.last_error)
        self.assertNotEqual(binding.sale_order_id.picking_ids.state, 'done')

    def test_a_failed_shipment_is_retried_while_the_good_one_is_kept(self):
        vals = {'name': 'Serial gear', 'default_code': 'SKU-9', 'tracking': 'lot'}
        vals.update(compat.product_type_vals(self.env, 'storable'))
        self.env['product.template'].create(vals)
        order = make_order(number=97, lines=2)
        item = order['_all']['lineItems'][1]
        item['sku'] = item['variant']['sku'] = 'SKU-9'
        item['variant']['id'] = 'gid://shopify/ProductVariant/9'
        binding = self._import(order)
        order['_all']['fulfillments'] = [shopify_fulfillment(order, 1, lines=[0]),
                                         shopify_fulfillment(order, 2, lines=[1])]
        job = self._run_import(binding)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(len(binding.fulfillment_ids), 1)
        retry = self.Job.search([('dedup_key', '=', 'fulfillment-import-retry:%s' % order['id'])])
        self.assertEqual(len(retry), 1)
        self.assertGreater(retry.next_run_at, fields.Datetime.now())
