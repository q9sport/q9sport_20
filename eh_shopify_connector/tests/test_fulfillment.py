# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .. import compat
from ..lib import fulfillment_plan
from .common import ShopifyCase
from .test_order_import import make_order
from .test_order_sale import ensure_chart


def fulfillment_order(order, number, status='OPEN', holds=(), quantities=None):
    lines = []
    for index, item in enumerate(order['_all']['lineItems']):
        quantity = (quantities or {}).get(index, item['quantity'])
        lines.append({'id': 'gid://shopify/FulfillmentOrderLineItem/%s%02d' % (number, index), 'totalQuantity': quantity,
                      'remainingQuantity': quantity, 'lineItem': {'id': item['id']}})
    return {'id': 'gid://shopify/FulfillmentOrder/%s' % number, 'status': status, 'requestStatus': 'UNSUBMITTED',
            'assignedLocation': {'name': 'Main', 'location': {'id': 'gid://shopify/Location/1'}},
            'fulfillmentHolds': [{'reason': reason, 'reasonNotes': None} for reason in holds],
            'supportedActions': [], 'lineItems': {'nodes': lines}}


@tagged('post_install', '-at_install', 'eh_shopify')
class TestFulfillmentPush(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.country_id:
            company.country_id = cls.env.ref('base.us')
        ensure_chart(cls.env, company)
        cls.env['product.product'].create([{'name': 'Brass gear %s' % i, 'default_code': 'SKU-%s' % i}
                                           for i in range(3)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Job = self.env['eh.shopify.job']

    def _import(self, order):
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])

    def _push_job(self, picking):
        return self.Job.search([('job_type', '=', 'fulfillment.push'), ('res_model', '=', 'stock.picking'),
                                ('res_id', '=', picking.id)], order='id desc', limit=1)

    def test_odoo_shipment_becomes_a_shopify_fulfillment(self):
        order = make_order(number=41, lines=2)
        order['_all']['fulfillmentOrders'] = [fulfillment_order(order, 41)]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        self.assertEqual(len(picking), 1)
        if compat.carrier_fields_available(self.env):
            picking.carrier_tracking_ref = '1Z999, 1Z998'
        compat.validate_picking(picking)
        self.assertEqual(picking.state, 'done')
        job = self._push_job(picking)
        self.assertTrue(job, 'validating the delivery queues the Shopify fulfillment')
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(len(self.shop.fulfillments), 1)
        created = self.shop.fulfillments[0]
        self.assertTrue(created['notifyCustomer'])
        if compat.carrier_fields_available(self.env):
            self.assertEqual([t['number'] for t in created['trackingInfo']], ['1Z999', '1Z998'])
        self.assertEqual(self.shop.fulfillment_orders['gid://shopify/FulfillmentOrder/41']['status'], 'CLOSED')
        self.assertEqual(binding.fulfillment_ids.picking_id, picking)
        self.assertEqual(set(binding.line_ids.filtered(lambda l: l.kind == 'product').mapped('fulfilled_quantity')),
                         {1.0})

    def test_partial_shipment_then_backorder(self):
        order = make_order(number=42, lines=1)
        item = order['_all']['lineItems'][0]
        item.update({'quantity': 2, 'currentQuantity': 2})
        order['currentTotalPriceSet'] = order['totalPriceSet'] = {
            'shopMoney': {'amount': '20.00', 'currencyCode': 'USD'},
            'presentmentMoney': {'amount': '20.00', 'currencyCode': 'USD'}}
        order['_all']['fulfillmentOrders'] = [fulfillment_order(order, 42)]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        compat.validate_picking(picking, quantities={picking.move_ids.id: 1})
        self._push_job(picking)._run_inline()
        fo = self.shop.fulfillment_orders['gid://shopify/FulfillmentOrder/42']
        self.assertEqual(fo['status'], 'IN_PROGRESS')
        self.assertEqual(fo['lineItems']['nodes'][0]['remainingQuantity'], 1)
        backorder = binding.sale_order_id.picking_ids - picking
        self.assertEqual(len(backorder), 1)
        compat.validate_picking(backorder)
        self._push_job(backorder)._run_inline()
        self.assertEqual(fo['status'], 'CLOSED')
        self.assertEqual(len(self.shop.fulfillments), 2)

    def test_retry_never_fulfils_the_same_units_twice(self):
        order = make_order(number=43, lines=1)
        order['_all']['fulfillmentOrders'] = [fulfillment_order(order, 43)]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        compat.validate_picking(picking)
        job = self._push_job(picking)
        job._run_inline()
        again = binding._push_fulfillment(picking)
        self.assertEqual(again, {'skipped': 'nothing new to send'})
        self.assertEqual(len(self.shop.fulfillments), 1)

    def test_held_fulfillment_order_is_explained(self):
        order = make_order(number=44, lines=1)
        order['_all']['fulfillmentOrders'] = [fulfillment_order(order, 44, status='ON_HOLD',
                                                                holds=['AWAITING_PAYMENT'])]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        compat.validate_picking(picking)
        job = self._push_job(picking)
        job._run_inline()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.error_kind, 'mapping')
        self.assertIn('AWAITING_PAYMENT', job.last_error)
        self.assertFalse(self.shop.fulfillments)

    def test_locations_shipped_by_shopify_are_left_alone(self):
        order = make_order(number=45, lines=1)
        order['_all']['fulfillmentOrders'] = [fulfillment_order(order, 45)]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        self.env['eh.shopify.location'].create({
            'store_id': self.store.id, 'name': '3PL', 'shopify_gid': 'gid://shopify/Location/1',
            'warehouse_id': picking.picking_type_id.warehouse_id.id, 'delivery_mode': 'shopify'})
        compat.validate_picking(picking)
        job = self._push_job(picking)
        job._run_inline()
        self.assertEqual(job.state, 'done')
        self.assertIn('skipped', job.result)
        self.assertFalse(self.shop.fulfillments)

    def test_deliveries_of_other_orders_queue_nothing(self):
        partner = self.env['res.partner'].create({'name': 'Walk in'})
        product = self.env['product.product'].search([('default_code', '=', 'SKU-0')], limit=1)
        uom_field, _tax_field = compat.sale_line_names(self.env)
        so = self.env['sale.order'].create({'partner_id': partner.id, 'order_line': [(0, 0, {
            'product_id': product.id, 'product_uom_qty': 1, uom_field: product.uom_id.id})]})
        so.action_confirm()
        compat.validate_picking(so.picking_ids)
        self.assertFalse(self.Job.search([('job_type', '=', 'fulfillment.push'),
                                          ('res_id', 'in', so.picking_ids.ids)]))

    def test_tracking_changed_after_shipping_is_sent_to_shopify(self):
        if not compat.carrier_fields_available(self.env):
            self.skipTest('carriers are not installed')
        order = make_order(number=91, lines=1)
        order['_all']['fulfillmentOrders'] = [fulfillment_order(order, 91)]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        picking.carrier_tracking_ref = 'OLD1'
        compat.validate_picking(picking)
        push = self._push_job(picking)
        push._run_inline()
        self.assertEqual(push.state, 'done', push.last_error)
        self.assertFalse(self.Job.search([('job_type', '=', 'fulfillment.tracking')]),
                         'tracking typed before validation travels with the fulfillment')
        picking.carrier_tracking_ref = 'NEW1, NEW2'
        job = self.Job.search([('job_type', '=', 'fulfillment.tracking'), ('res_model', '=', 'stock.picking'),
                               ('res_id', '=', picking.id)])
        self.assertEqual(len(job), 1)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual([t['number'] for t in self.shop.fulfillments[0]['trackingInfo']], ['NEW1', 'NEW2'])
        self.assertFalse(self.shop.tracking_updates[-1]['notifyCustomer'])
        self.assertEqual(binding.fulfillment_ids.tracking_numbers, 'NEW1, NEW2')
        picking.carrier_tracking_ref = 'NEW1, NEW2'
        self.assertEqual(self.Job.search_count([('job_type', '=', 'fulfillment.tracking')]), 1,
                         'an unchanged value queues nothing')
        rerun = self.Job.browse(self.Job._enqueue(self.store, 'fulfillment.tracking', payload={'picking_id': picking.id},
                                                  dedup_key='tracking-rerun:%s' % picking.id).id)
        rerun._run_inline()
        self.assertEqual(rerun.result.get('updated'), [], 'the same tracking is not sent twice')
        self.assertEqual(len(self.shop.tracking_updates), 1)

    def _other_location_order(self, number):
        order = make_order(number=number, lines=1)
        fulfillment = fulfillment_order(order, number)
        fulfillment['assignedLocation'] = {'name': 'Overflow', 'location': {'id': 'gid://shopify/Location/2'}}
        fulfillment['supportedActions'] = [{'action': 'MOVE'}]
        order['_all']['fulfillmentOrders'] = [fulfillment]
        return order, fulfillment

    def _map_main_location(self, picking):
        Location = self.env['eh.shopify.location']
        main = Location.search([('store_id', '=', self.store.id), ('shopify_gid', '=', 'gid://shopify/Location/1')])
        vals = {'warehouse_id': picking.picking_type_id.warehouse_id.id, 'delivery_mode': 'odoo',
                'removed_in_shopify': False}
        if main:
            main.write(vals)
        else:
            Location.create(dict(vals, store_id=self.store.id, name='Main warehouse', active_in_shopify=True,
                                 shopify_gid='gid://shopify/Location/1'))

    def test_shipment_from_another_location_moves_the_fulfillment_order(self):
        order, fulfillment = self._other_location_order(92)
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        self._map_main_location(picking)
        compat.validate_picking(picking)
        job = self._push_job(picking)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual([move['newLocationId'] for move in self.shop.fo_moves], ['gid://shopify/Location/1'])
        self.assertEqual(fulfillment['assignedLocation']['location']['id'], 'gid://shopify/Location/1')
        self.assertEqual(len(self.shop.fulfillments), 1)
        self.assertEqual(fulfillment['status'], 'CLOSED')

    def test_moving_fulfillment_orders_can_be_switched_off(self):
        self.store.move_fulfillment_orders = False
        order, _fulfillment = self._other_location_order(93)
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        self._map_main_location(picking)
        compat.validate_picking(picking)
        job = self._push_job(picking)
        job._run_inline()
        self.assertEqual(job.state, 'failed')
        self.assertIn('another location', job.last_error)
        self.assertFalse(self.shop.fo_moves)
        self.assertFalse(self.shop.fulfillments)

    def test_released_hold_retries_the_waiting_shipment(self):
        order = make_order(number=94, lines=1)
        fulfillment = fulfillment_order(order, 94, status='ON_HOLD', holds=('AWAITING_PAYMENT',))
        order['_all']['fulfillmentOrders'] = [fulfillment]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        compat.validate_picking(picking)
        job = self._push_job(picking)
        job._run_inline()
        self.assertEqual(job.state, 'failed')
        fulfillment.update({'status': 'OPEN', 'fulfillmentHolds': []})
        event = self.env['eh.shopify.event'].new({
            'store_id': self.store.id, 'topic': 'fulfillment_orders/hold_released', 'webhook_id': 'w-94',
            'payload': {'fulfillment_order': {'id': fulfillment['id'], 'status': 'open'}}})
        self.assertEqual(event._on_fulfillment_order_ready()['retried'], 1)
        self.assertEqual(job.state, 'pending')
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(len(self.shop.fulfillments), 1)

    def test_released_hold_leaves_ambiguous_pushes_for_a_person(self):
        order = make_order(number=95, lines=1)
        fulfillment = fulfillment_order(order, 95, status='ON_HOLD', holds=('AWAITING_PAYMENT',))
        order['_all']['fulfillmentOrders'] = [fulfillment]
        binding = self._import(order)
        picking = binding.sale_order_id.picking_ids
        compat.validate_picking(picking)
        job = self._push_job(picking)
        job._run_inline()
        job.write({'state': 'failed', 'error_kind': 'ambiguous'})
        fulfillment.update({'status': 'OPEN', 'fulfillmentHolds': []})
        event = self.env['eh.shopify.event'].new({
            'store_id': self.store.id, 'topic': 'fulfillment_orders/hold_released', 'webhook_id': 'w-95',
            'payload': {'fulfillment_order': {'id': fulfillment['id'], 'status': 'open'}}})
        self.assertEqual(event._on_fulfillment_order_ready()['retried'], 0)
        self.assertEqual(job.state, 'failed')
        self.assertFalse(self.shop.fulfillments)

    def test_planner_moves_only_what_ships(self):
        line = 'gid://shopify/LineItem/1'

        def owing(number, location):
            return {'id': 'gid://shopify/FulfillmentOrder/%s' % number, 'status': 'OPEN',
                    'assignedLocation': {'location': {'id': location}},
                    'lineItems': {'nodes': [{'id': 'fol-%s' % number, 'remainingQuantity': 1,
                                             'lineItem': {'id': line}}]}}
        plan = fulfillment_plan.plan([owing(1, 'gid://shopify/Location/2'), owing(2, 'gid://shopify/Location/3')],
                                     {line: 1}, location_gid='gid://shopify/Location/1')
        self.assertEqual(len(plan['moves']), 1, 'one unit shipped moves one fulfillment order')
        self.assertEqual(plan['unallocated'], {line: 1})

    def _bind_location(self, gid, name, warehouse):
        Location = self.env['eh.shopify.location']
        binding = Location.search([('store_id', '=', self.store.id), ('shopify_gid', '=', gid)])
        vals = {'warehouse_id': warehouse.id, 'delivery_mode': 'odoo', 'removed_in_shopify': False}
        if binding:
            binding.write(vals)
        else:
            binding = Location.create(dict(vals, store_id=self.store.id, name=name, shopify_gid=gid,
                                           active_in_shopify=True))
        return binding

    def test_order_is_delivered_from_the_warehouse_of_its_shopify_location(self):
        main = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        second = self.env['stock.warehouse'].create({'name': 'Sydney warehouse', 'code': 'EHSYD'})
        self.store.warehouse_id = main
        self._bind_location('gid://shopify/Location/1', 'Main warehouse', main)
        self._bind_location('gid://shopify/Location/2', 'Sydney', second)
        order = make_order(number=98, lines=1)
        fulfillment = fulfillment_order(order, 98)
        fulfillment['assignedLocation'] = {'name': 'Sydney', 'location': {'id': 'gid://shopify/Location/2'}}
        order['_all']['fulfillmentOrders'] = [fulfillment]
        binding = self._import(order)
        self.assertEqual(binding.sale_order_id.warehouse_id, second)
        self.assertEqual(binding.sale_order_id.picking_ids.picking_type_id.warehouse_id, second)

    def test_order_split_over_warehouses_uses_the_default_and_says_so(self):
        main = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        second = self.env['stock.warehouse'].create({'name': 'Perth warehouse', 'code': 'EHPER'})
        self.store.warehouse_id = main
        self._bind_location('gid://shopify/Location/1', 'Main warehouse', main)
        self._bind_location('gid://shopify/Location/3', 'Perth', second)
        order = make_order(number=99, lines=2)
        first = fulfillment_order(order, 991, quantities={1: 0})
        other = fulfillment_order(order, 992, quantities={0: 0})
        other['assignedLocation'] = {'name': 'Perth', 'location': {'id': 'gid://shopify/Location/3'}}
        order['_all']['fulfillmentOrders'] = [first, other]
        binding = self._import(order)
        sale_order = binding.sale_order_id
        self.assertEqual(sale_order.warehouse_id, main)
        self.assertTrue(any('Perth' in (message.body or '') for message in sale_order.message_ids))
