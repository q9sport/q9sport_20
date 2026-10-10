# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase
from .test_order_import import bag, make_order
from .test_order_sale import ensure_chart
from .test_refunds import add_refund


@tagged('post_install', '-at_install', 'eh_shopify')
class TestReturns(ShopifyCase):

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
        self.store.invoice_policy = 'manual'
        self.Order = self.env['eh.shopify.order']
        self.Return = self.env['eh.shopify.return']
        self.Job = self.env['eh.shopify.job']
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': self.bank.id})

    def _delivered_order(self, number, lines=2):
        order = make_order(number=number, lines=lines)
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        binding = self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])
        delivery = binding.sale_order_id.picking_ids
        compat.validate_picking(delivery)
        self.assertEqual(delivery.state, 'done')
        # Validating queues the fulfilment push for this order; the runner would
        # send it first. Jobs of one order run in order, so clear it here.
        self.Job.search([('job_type', '=', 'fulfillment.push'), ('state', '=', 'pending')]).write({'state': 'cancelled'})
        return order, binding

    def _shopify_return(self, order, number, status, lines):
        gid = 'gid://shopify/Return/%s' % number
        self.shop.returns[gid] = {
            'id': gid, 'name': '#R%s' % number, 'status': status, 'orderId': order['id'], 'exchanges': [],
            'lines': [{'id': '%s-line-%s' % (gid, index), 'quantity': quantity,
                       'lineItemId': order['_all']['lineItems'][index]['id'], 'dispositions': dispositions}
                      for index, quantity, dispositions in lines]}
        return self.shop.returns[gid]

    def _sync(self, gid, order):
        job = self.Return._enqueue_return_sync(self.store, gid, order['id'])
        job._run_inline()
        return job

    def test_requested_return_prepares_a_return_transfer(self):
        order, binding = self._delivered_order(61)
        record = self._shopify_return(order, 611, 'REQUESTED', [(0, 1, [])])
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        self.assertEqual(returned.state, 'waiting')
        picking = returned.picking_ids
        self.assertEqual(picking.picking_type_code, 'incoming')
        self.assertNotIn(picking.state, ('done', 'cancel'))
        product = binding.line_ids.filtered(lambda l: l.shopify_gid == order['_all']['lineItems'][0]['id']).sale_line_id.product_id
        self.assertEqual(picking.move_ids.product_id, product)
        self.assertEqual(picking.move_ids.product_uom_qty, 1.0)

    def test_closed_return_receives_only_restocked_units(self):
        self.store.return_auto_validate = True
        order, binding = self._delivered_order(62)
        record = self._shopify_return(order, 621, 'OPEN', [(0, 1, []), (1, 1, [])])
        self._sync(record['id'], order)
        record['status'] = 'CLOSED'
        record['lines'][0]['dispositions'] = [{'type': 'RESTOCKED', 'quantity': 1, 'location': None}]
        record['lines'][1]['dispositions'] = [{'type': 'NOT_RESTOCKED', 'quantity': 1, 'location': None}]
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        self.assertEqual(returned.state, 'received', returned.reason)
        received = returned.picking_ids.move_ids.filtered(lambda m: m.state == 'done')
        restocked_product = binding.line_ids.filtered(
            lambda l: l.shopify_gid == order['_all']['lineItems'][0]['id']).sale_line_id.product_id
        self.assertEqual(received.product_id, restocked_product, 'units not restocked never enter stock')

    def test_units_not_restocked_are_received_into_the_chosen_location(self):
        self.store.return_auto_validate = True
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        quarantine = self.env['stock.location'].create({'name': 'Returns quarantine', 'usage': 'internal',
                                                        'location_id': warehouse.view_location_id.id})
        self.store.return_unsellable_location_id = quarantine
        order, binding = self._delivered_order(63)
        record = self._shopify_return(order, 631, 'OPEN', [(0, 1, []), (1, 1, [])])
        self._sync(record['id'], order)
        default_destination = self.Return.search([('shopify_gid', '=', record['id'])]).picking_ids.location_dest_id
        record['status'] = 'CLOSED'
        record['lines'][0]['dispositions'] = [{'type': 'RESTOCKED', 'quantity': 1, 'location': None}]
        record['lines'][1]['dispositions'] = [{'type': 'NOT_RESTOCKED', 'quantity': 1, 'location': None}]
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        self.assertEqual(returned.state, 'received', returned.reason)
        received = returned.picking_ids.move_ids.filtered(lambda m: m.state == 'done')
        by_product = {move.product_id.default_code: move.location_dest_id for move in received}
        self.assertEqual(by_product, {'SKU-0': default_destination, 'SKU-1': quarantine})
        self.assertFalse(returned.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')))

    def test_units_restocked_at_another_mapped_location_are_received_there(self):
        self.store.return_auto_validate = True
        second = self.env['stock.warehouse'].create({'name': 'Second shop floor', 'code': 'SHW2'})
        self.env['eh.shopify.location'].create({'store_id': self.store.id, 'name': 'Second shop',
                                                'shopify_gid': 'gid://shopify/Location/77', 'warehouse_id': second.id})
        order, binding = self._delivered_order(64, lines=1)
        record = self._shopify_return(order, 641, 'OPEN', [(0, 1, [])])
        self._sync(record['id'], order)
        record['lines'][0]['dispositions'] = [{'type': 'RESTOCKED', 'quantity': 1,
                                               'location': {'id': 'gid://shopify/Location/77'}}]
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        received = self.Return.search([('shopify_gid', '=', record['id'])]).picking_ids.move_ids.filtered(
            lambda m: m.state == 'done')
        self.assertEqual(received.location_dest_id, second.lot_stock_id)
        self.assertEqual(received.move_line_ids.location_dest_id, second.lot_stock_id)

    def test_units_restocked_at_a_mapped_location_of_the_same_warehouse_are_all_received(self):
        self.store.return_auto_validate = True
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        self.env['eh.shopify.location'].create({'store_id': self.store.id, 'name': 'Main shop',
                                                'shopify_gid': 'gid://shopify/Location/78', 'warehouse_id': warehouse.id})
        order = make_order(number=65, lines=1)
        item = order['_all']['lineItems'][0]
        item['quantity'] = item['currentQuantity'] = 2
        for key in ('currentTotalPriceSet', 'totalPriceSet'):
            order[key] = bag('20.00')
        order['transactions'][0]['amountSet'] = bag('20.00')
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        binding = self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])
        compat.validate_picking(binding.sale_order_id.picking_ids)
        self.Job.search([('job_type', '=', 'fulfillment.push'), ('state', '=', 'pending')]).write({'state': 'cancelled'})
        record = self._shopify_return(order, 651, 'OPEN', [(0, 2, [])])
        self._sync(record['id'], order)
        record['status'] = 'CLOSED'
        record['lines'][0]['dispositions'] = [
            {'type': 'RESTOCKED', 'quantity': 1, 'location': {'id': 'gid://shopify/Location/78'}},
            {'type': 'RESTOCKED', 'quantity': 1, 'location': None}]
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        self.assertEqual(returned.state, 'received', returned.reason)
        done = returned.picking_ids.move_ids.filtered(lambda m: m.state == 'done')
        self.assertEqual(sum(compat.move_done_qty(move) for move in done), 2.0,
                         'both restocked units enter stock, neither is cancelled')
        self.assertEqual(done.location_dest_id, warehouse.lot_stock_id)

    def test_declined_return_cancels_the_waiting_transfer(self):
        order, _binding = self._delivered_order(63)
        record = self._shopify_return(order, 631, 'REQUESTED', [(0, 1, [])])
        self._sync(record['id'], order)
        record['status'] = 'DECLINED'
        self._sync(record['id'], order)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        self.assertEqual(returned.state, 'cancelled')
        self.assertEqual(returned.picking_ids.state, 'cancel')

    def test_return_of_goods_never_delivered_is_explained(self):
        order = make_order(number=64, lines=1)
        self.shop.add_order(order)
        self.Order._enqueue_import(self.store, order['id'])._run_inline()
        record = self._shopify_return(order, 641, 'REQUESTED', [(0, 1, [])])
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'failed')
        self.assertIn('not delivered', job.last_error)

    def test_return_webhooks_need_the_returns_permission(self):
        self.store.granted_scopes = 'read_orders'
        self.assertNotIn('RETURNS_REQUEST', self.store._webhook_topics())
        self.store.granted_scopes = 'read_orders,read_returns'
        self.assertIn('RETURNS_REQUEST', self.store._webhook_topics())
        event = self.env['eh.shopify.event'].new({'store_id': self.store.id, 'topic': 'returns/request',
                                                  'webhook_id': 'w-ret', 'payload': {
                                                      'admin_graphql_api_id': 'gid://shopify/Return/9',
                                                      'order': {'admin_graphql_api_id': 'gid://shopify/Order/9'}}})
        self.assertEqual(event._on_return_change(), {'queued': 'gid://shopify/Return/9'})

    def test_refund_of_a_synced_return_does_not_ask_for_a_manual_return(self):
        self.store.invoice_policy = 'paid'
        order, binding = self._delivered_order(65, lines=1)
        record = self._shopify_return(order, 651, 'OPEN', [(0, 1, [])])
        self._sync(record['id'], order)
        refund = add_refund(order, 652, [(0, 1)], restock='RETURN')
        refund['return'] = {'id': record['id']}
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        for refund_job in self.Job.search([('job_type', '=', 'refund.import'), ('state', '=', 'pending')]):
            refund_job._run_inline()
        refund_binding = binding.refund_ids
        self.assertEqual(refund_binding.state, 'done', refund_binding.reason)
        self.assertFalse(refund_binding.restock_note)

    # ------------------------------------------------------------------ review round
    def _order_of_two(self, number):
        order = make_order(number=number, lines=1)
        item = order['_all']['lineItems'][0]
        item['quantity'] = item['currentQuantity'] = 2
        for key in ('currentTotalPriceSet', 'totalPriceSet'):
            order[key] = {'shopMoney': {'amount': '20.00', 'currencyCode': 'USD'},
                          'presentmentMoney': {'amount': '20.00', 'currencyCode': 'USD'}}
        order['transactions'][0]['amountSet'] = {'shopMoney': {'amount': '20.00', 'currencyCode': 'USD'},
                                                 'presentmentMoney': {'amount': '20.00', 'currencyCode': 'USD'}}
        self.shop.add_order(order)
        job = self.Order._enqueue_import(self.store, order['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        binding = self.Order.search([('store_id', '=', self.store.id), ('shopify_gid', '=', order['id'])])
        self.assertEqual(binding.sale_order_id.order_line.product_uom_qty, 2.0)
        return order, binding

    def _clear_pushes(self):
        self.Job.search([('job_type', '=', 'fulfillment.push'), ('state', '=', 'pending')]).write({'state': 'cancelled'})

    def test_units_shipped_in_two_deliveries_are_returned_whole(self):
        order, binding = self._order_of_two(71)
        delivery = binding.sale_order_id.picking_ids
        compat.validate_picking(delivery, quantities={delivery.move_ids.id: 1.0})
        backorder = binding.sale_order_id.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        self.assertTrue(backorder)
        compat.validate_picking(backorder)
        self._clear_pushes()
        record = self._shopify_return(order, 711, 'REQUESTED', [(0, 1, [])])
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        moves = self.Return.search([('shopify_gid', '=', record['id'])]).picking_ids.move_ids
        self.assertEqual(moves.mapped('product_uom_qty'), [1.0], 'one whole unit, never half a unit twice')

    def test_a_second_return_on_the_same_line_takes_back_only_what_is_left(self):
        self.store.return_auto_validate = True
        order, binding = self._order_of_two(72)
        compat.validate_picking(binding.sale_order_id.picking_ids)
        self._clear_pushes()
        first = self._shopify_return(order, 721, 'CLOSED', [(0, 1, [{'type': 'RESTOCKED', 'quantity': 1,
                                                                     'location': None}])])
        self._sync(first['id'], order)
        self.assertEqual(self.Return.search([('shopify_gid', '=', first['id'])]).state, 'received')
        second = self._shopify_return(order, 722, 'REQUESTED', [(0, 1, [])])
        job = self._sync(second['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        moves = self.Return.search([('shopify_gid', '=', second['id'])]).picking_ids.move_ids
        self.assertEqual(sum(moves.mapped('product_uom_qty')), 1.0)

    def test_units_restocked_while_the_return_is_open_are_received_and_the_rest_waits(self):
        self.store.return_auto_validate = True
        order, binding = self._delivered_order(73)
        restocked = [{'type': 'RESTOCKED', 'quantity': 1, 'location': None}]
        record = self._shopify_return(order, 731, 'OPEN', [(0, 1, restocked), (1, 1, [])])
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        self.assertEqual(returned.state, 'waiting')
        first_product = binding.line_ids.filtered(
            lambda l: l.shopify_gid == order['_all']['lineItems'][0]['id']).sale_line_id.product_id
        self.assertEqual(returned.picking_ids.move_ids.filtered(lambda m: m.state == 'done').product_id, first_product)
        self.assertTrue(returned.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')))
        record['lines'][1]['dispositions'] = [{'type': 'RESTOCKED', 'quantity': 1, 'location': None}]
        record['status'] = 'CLOSED'
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(returned.state, 'received')
        self.assertEqual(len(returned.picking_ids.move_ids.filtered(lambda m: m.state == 'done')), 2)

    def test_a_transfer_cancelled_in_odoo_is_not_created_again(self):
        order, _binding = self._delivered_order(74)
        record = self._shopify_return(order, 741, 'REQUESTED', [(0, 1, [])])
        self._sync(record['id'], order)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        returned.picking_ids.action_cancel()
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(len(returned.picking_ids), 1)
        self.assertEqual(returned.state, 'cancelled')

    def test_a_transfer_validated_by_hand_updates_the_return(self):
        order, _binding = self._delivered_order(75)
        record = self._shopify_return(order, 751, 'REQUESTED', [(0, 1, [])])
        self._sync(record['id'], order)
        record['status'] = 'CLOSED'
        self._sync(record['id'], order)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        self.assertEqual(returned.state, 'waiting')
        self.assertIn('Validate', returned.reason)
        compat.validate_picking(returned.picking_ids)
        self.assertEqual(returned.state, 'received')

    def test_a_return_with_many_lines_is_read_page_by_page(self):
        order, _binding = self._delivered_order(76)
        self.shop.return_page_size = 1
        record = self._shopify_return(order, 761, 'REQUESTED', [(0, 1, []), (1, 1, [])])
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(len(self.Return.search([('shopify_gid', '=', record['id'])]).picking_ids.move_ids), 2)

    def test_a_return_that_arrives_before_its_order_is_synced_once_the_order_is_in(self):
        order = make_order(number=77, lines=1)
        self.shop.add_order(order)
        record = self._shopify_return(order, 771, 'REQUESTED', [(0, 1, [])])
        job = self._sync(record['id'], order)
        self.assertIn('waiting', job.result)
        self.assertTrue(self.Job.search([('dedup_key', '=', 'return:%s:wait' % record['id']), ('state', '=', 'pending')]))
        order['returnStatus'] = 'RETURN_REQUESTED'
        imported = self.Order._enqueue_import(self.store, order['id'])
        imported._run_inline()
        self.assertEqual(imported.state, 'done', imported.last_error)
        self.assertTrue(self.Job.search([('job_type', '=', 'return.sync'), ('dedup_key', '=', 'return:%s' % record['id']),
                                         ('state', '=', 'pending')]))

    def test_a_change_during_a_running_sync_is_synced_again(self):
        job = self.Return._enqueue_return_sync(self.store, 'gid://shopify/Return/9', 'gid://shopify/Order/9')
        job.state = 'running'
        follow = self.Return._enqueue_return_sync(self.store, 'gid://shopify/Return/9', 'gid://shopify/Order/9')
        self.assertEqual(follow.dedup_key, 'return:gid://shopify/Return/9:next')
        self.store.granted_scopes = 'read_orders,read_returns'
        self.assertIn('RETURNS_UPDATE', self.store._webhook_topics())

    # ------------------------------------------------------------------ exchanges
    def _exchange(self, record, number, sku='SKU-2', variant='gid://shopify/ProductVariant/2'):
        price = {'shopMoney': {'amount': '10.00', 'currencyCode': 'USD'},
                 'presentmentMoney': {'amount': '10.00', 'currencyCode': 'USD'}}
        record['exchanges'] = [{'id': 'gid://shopify/ExchangeLineItem/%s' % number, 'quantity': 1, 'variantId': variant,
                                'lineItems': [{'id': 'gid://shopify/LineItem/%s' % number, 'name': 'Brass gear exchange',
                                               'sku': sku, 'quantity': 1,
                                               'variant': {'id': variant, 'sku': sku, 'barcode': None},
                                               'originalUnitPriceSet': price}]}]

    def test_an_exchange_ships_the_replacement_and_its_shipment_goes_to_shopify(self):
        order, binding = self._delivered_order(81)
        record = self._shopify_return(order, 811, 'OPEN', [(0, 1, [])])
        self._exchange(record, 8111)
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        exchange = returned.exchange_order_id
        self.assertEqual(exchange.state, 'sale')
        self.assertEqual((exchange.order_line.product_id.default_code, exchange.order_line.price_unit), ('SKU-2', 0.0))
        self.assertEqual(exchange.eh_shopify_order_id, binding)
        link = binding.line_ids.filtered(lambda l: l.shopify_gid == 'gid://shopify/LineItem/8111')
        self.assertEqual(link.sale_line_id, exchange.order_line)
        delivery = exchange.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing')
        self.assertTrue(delivery)
        compat.validate_picking(delivery)
        self.assertTrue(self.Job.search([('job_type', '=', 'fulfillment.push'),
                                         ('dedup_key', '=', 'fulfillment:%s' % delivery.id)]))
        self._clear_pushes()
        again = self._sync(record['id'], order)
        self.assertEqual(again.state, 'done', again.last_error)
        self.assertEqual(self.env['sale.order'].search_count([('eh_shopify_exchange_return_id', '=', returned.id)]), 1)

    def test_an_exchange_can_be_prepared_as_a_quotation_at_shopify_prices(self):
        self.store.exchange_order_mode = 'quotation'
        order, _binding = self._delivered_order(82)
        record = self._shopify_return(order, 821, 'OPEN', [(0, 1, [])])
        self._exchange(record, 8211)
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        exchange = self.Return.search([('shopify_gid', '=', record['id'])]).exchange_order_id
        self.assertIn(exchange.state, ('draft', 'sent'))
        self.assertAlmostEqual(exchange.order_line.price_unit, 10.0, places=2)

    def test_a_declined_exchange_cancels_the_order_not_shipped_yet(self):
        order, _binding = self._delivered_order(83)
        record = self._shopify_return(order, 831, 'OPEN', [(0, 1, [])])
        self._exchange(record, 8311)
        self._sync(record['id'], order)
        exchange = self.Return.search([('shopify_gid', '=', record['id'])]).exchange_order_id
        self.assertTrue(exchange)
        self.assertFalse(exchange._eh_shopify_cancel_needs_dialog(), 'cancelling it never cancels the Shopify order')
        self._clear_pushes()
        record['status'] = 'DECLINED'
        job = self._sync(record['id'], order)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(exchange.state, 'cancel')

    def test_an_exchange_waits_for_the_return_to_be_approved(self):
        order, _binding = self._delivered_order(85)
        record = self._shopify_return(order, 851, 'REQUESTED', [(0, 1, [])])
        self._exchange(record, 8511)
        self._sync(record['id'], order)
        returned = self.Return.search([('shopify_gid', '=', record['id'])])
        self.assertFalse(returned.exchange_order_id)
        self.assertIn('once the return is approved', returned.exchange_note)
