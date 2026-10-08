# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import BaseCase, tagged

from ..lib import client, documents, fulfillment_plan, inventory_plan, testing, throttle


def fo(gid, status='OPEN', location='gid://shopify/Location/1', lines=(), holds=()):
    return {'id': gid, 'status': status, 'assignedLocation': {'location': {'id': location}},
            'fulfillmentHolds': [{'reason': r} for r in holds],
            'lineItems': {'nodes': [{'id': '%s/line/%s' % (gid, i), 'remainingQuantity': qty,
                                     'lineItem': {'id': line_gid}} for i, (line_gid, qty) in enumerate(lines)]}}


@tagged('post_install', '-at_install', 'eh_shopify')
class TestFulfillmentPlan(BaseCase):

    def test_split_across_fulfillment_orders_without_overshipping(self):
        orders = [fo('FO/1', lines=[('L1', 2), ('L2', 1)]), fo('FO/2', lines=[('L1', 3)])]
        result = fulfillment_plan.plan(orders, {'L1': 4, 'L2': 1})
        self.assertEqual(result['requests'], [
            {'fulfillmentOrderId': 'FO/1', 'fulfillmentOrderLineItems': [{'id': 'FO/1/line/0', 'quantity': 2},
                                                                         {'id': 'FO/1/line/1', 'quantity': 1}]},
            {'fulfillmentOrderId': 'FO/2', 'fulfillmentOrderLineItems': [{'id': 'FO/2/line/0', 'quantity': 2}]}])
        self.assertEqual(result['unallocated'], {})

    def test_more_shipped_than_owed_is_reported(self):
        result = fulfillment_plan.plan([fo('FO/1', lines=[('L1', 1)])], {'L1': 3})
        self.assertEqual(result['unallocated'], {'L1': 2})

    def test_holds_and_schedules_block_with_reason(self):
        orders = [fo('FO/1', status='ON_HOLD', lines=[('L1', 1)], holds=['AWAITING_PAYMENT']),
                  fo('FO/2', status='SCHEDULED', lines=[('L2', 1)]), fo('FO/3', status='CLOSED', lines=[('L3', 1)])]
        result = fulfillment_plan.plan(orders, {'L1': 1, 'L2': 1, 'L3': 1})
        self.assertEqual([(b['fulfillmentOrderId'], b['reason']) for b in result['blocked']],
                         [('FO/1', 'on_hold'), ('FO/2', 'scheduled')])
        self.assertEqual(result['blocked'][0]['detail'], 'AWAITING_PAYMENT')
        self.assertFalse(result['requests'])
        self.assertEqual(result['unallocated'], {'L1': 1, 'L2': 1, 'L3': 1})

    def test_wrong_location_needs_a_move_not_a_silent_fulfilment(self):
        orders = [fo('FO/1', location='gid://shopify/Location/2', lines=[('L1', 2)]),
                  fo('FO/2', location='gid://shopify/Location/1', lines=[('L1', 1)])]
        result = fulfillment_plan.plan(orders, {'L1': 2}, location_gid='gid://shopify/Location/1')
        self.assertEqual(result['requests'][0]['fulfillmentOrderId'], 'FO/2')
        self.assertEqual(result['moves'], [{'fulfillmentOrderId': 'FO/1', 'fromLocationId': 'gid://shopify/Location/2',
                                            'toLocationId': 'gid://shopify/Location/1', 'lineItems': {'L1': 1}}])
        self.assertEqual(result['unallocated'], {'L1': 1})

    def test_fake_shop_accepts_the_plan_and_rejects_overshipping(self):
        transport = testing.FakeTransport()
        order = fo('gid://shopify/FulfillmentOrder/1', lines=[('L1', 2)])
        transport.shop.fulfillment_orders[order['id']] = order
        c = client.GraphQLClient(transport.shop.domain, lambda force_refresh=False: 't', transport=transport,
                                 sleep=transport.fake_sleep, bucket=throttle.CostBucket())
        result = fulfillment_plan.plan([order], {'L1': 2})
        payload = c.mutate(documents.FULFILLMENT_CREATE.text, {'fulfillment': {
            'notifyCustomer': True, 'trackingInfo': {'company': 'UPS', 'numbers': ['1Z1', '1Z2']},
            'lineItemsByFulfillmentOrder': result['requests']}}, 'fulfillmentCreate')
        self.assertEqual([t['number'] for t in payload['fulfillment']['trackingInfo']], ['1Z1', '1Z2'])
        self.assertEqual(order['status'], 'CLOSED')
        self.assertTrue(transport.shop.fulfillments[0]['notifyCustomer'])
        self.assertFalse(fulfillment_plan.plan([order], {'L1': 1})['requests'])


@tagged('post_install', '-at_install', 'eh_shopify')
class TestInventoryPlan(BaseCase):

    def _change(self, item, quantity, known, location='LOC'):
        return {'inventory_item_gid': item, 'location_gid': location, 'quantity': quantity, 'last_known': known}

    def test_prepare_dedups_skips_and_uses_compare_and_set(self):
        batches, needs_read, skipped = inventory_plan.prepare([
            self._change('I1', 5, 3), self._change('I1', 7, 3), self._change('I2', 4, 4),
            self._change('I3', 2, None), self._change('I4', -3, 1)], batch_size=10)
        self.assertEqual(batches, [[
            {'inventoryItemId': 'I1', 'locationId': 'LOC', 'quantity': 7, 'changeFromQuantity': 3},
            {'inventoryItemId': 'I4', 'locationId': 'LOC', 'quantity': 0, 'changeFromQuantity': 1}]])
        self.assertEqual([c['inventory_item_gid'] for c in needs_read], ['I3'])
        self.assertEqual([c['inventory_item_gid'] for c in skipped], ['I2'])

    def test_batches_split_by_size(self):
        batches, _read, _skip = inventory_plan.prepare([self._change('I%03d' % i, 1, 0) for i in range(250)])
        self.assertEqual([len(b) for b in batches], [100, 100, 50])

    def test_indexed_errors_isolate_only_the_bad_items(self):
        items = [{'inventoryItemId': 'I%s' % i} for i in range(7)]
        bad = {'I2', 'I5'}

        def send(batch):
            return [{'field': ['input', 'quantities', str(i), 'locationId'], 'code': 'X'}
                    for i, item in enumerate(batch) if item['inventoryItemId'] in bad]

        result = inventory_plan.send_all([items], send)
        self.assertEqual(sorted(i['inventoryItemId'] for i in result['applied']), ['I0', 'I1', 'I3', 'I4', 'I6'])
        self.assertEqual(sorted(i['inventoryItemId'] for i, _e in result['failed']), ['I2', 'I5'])
        self.assertEqual(result['calls'], 2)

    def test_unindexed_errors_are_narrowed_by_halving(self):
        items = [{'inventoryItemId': 'I%s' % i} for i in range(8)]

        def send(batch):
            if any(item['inventoryItemId'] == 'I6' for item in batch):
                return [{'field': ['input'], 'message': 'something is wrong', 'code': 'INVALID'}]
            return []

        result = inventory_plan.send_all([items], send)
        self.assertEqual([i['inventoryItemId'] for i, _e in result['failed']], ['I6'])
        self.assertEqual(len(result['applied']), 7)

    def test_fake_shop_compare_and_set_detects_concurrent_sales(self):
        transport = testing.FakeTransport()
        transport.shop.inventory[('I1', 'LOC')] = 5
        transport.shop.inventory[('I2', 'LOC')] = 9
        c = client.GraphQLClient(transport.shop.domain, lambda force_refresh=False: 't', transport=transport,
                                 sleep=transport.fake_sleep, bucket=throttle.CostBucket())

        def send(batch):
            response = c.execute(documents.INVENTORY_SET.text, {'input': {
                'name': 'available', 'reason': 'correction', 'quantities': batch}}, idempotency_key='k')
            return response.data['inventorySetQuantities']['userErrors']

        batches, _read, _skip = inventory_plan.prepare([self._change('I1', 8, 5), self._change('I2', 1, 7)])
        result = inventory_plan.send_all(batches, send)
        self.assertEqual([i['inventoryItemId'] for i in result['applied']], ['I1'])
        self.assertEqual(result['failed'][0][1]['code'], transport.shop.inventory_error_codes['stale'])
        self.assertEqual(transport.shop.inventory[('I1', 'LOC')], 8)
        self.assertEqual(transport.shop.inventory[('I2', 'LOC')], 9)
