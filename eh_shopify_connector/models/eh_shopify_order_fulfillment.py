# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Send Odoo shipments to Shopify as fulfillments.

Fulfillment orders are read fresh from Shopify for every shipment, so what
Shopify still owes is always the current truth. Quantities this picking already
sent are subtracted first, and each created fulfillment is recorded in its own
committed transaction, so retrying a job can never fulfil the same units twice.
"""
import re

from odoo import SUPERUSER_ID, _, api, models

from .. import compat
from ..lib import documents, errors, fulfillment_plan


class ShopifyOrderFulfillment(models.Model):
    _inherit = 'eh.shopify.order'

    def _job_fulfillment_push(self, job):
        picking = self.env['stock.picking'].browse((job.payload or {}).get('picking_id')).exists()
        if not picking or picking.state != 'done':
            return {'skipped': 'the delivery is not done'}
        order = compat.picking_sale_order(picking)
        binding = order.eh_shopify_order_id if order else self.browse()
        if not binding:
            return {'skipped': 'not a Shopify order'}
        if binding.store_id != job.store_id:
            return {'skipped': 'the order belongs to another store'}
        return binding._push_fulfillment(picking)

    # ------------------------------------------------------------------ inputs
    def _shipped_quantities(self, picking):
        lines = {line.sale_line_id.id: line for line in self.line_ids
                 if line.sale_line_id and line.shopify_gid and line.kind in ('product', 'gift_card')}
        grouped, kits = {}, {}
        for move in picking.move_ids.filtered(lambda m: m.state == 'done' and m.sale_line_id):
            grouped.setdefault(move.sale_line_id.id, self.env['stock.move'])
            grouped[move.sale_line_id.id] |= move
        shipped = {}
        for sale_line_id, moves in grouped.items():
            line = lines.get(sale_line_id)
            if not line:
                continue
            sale_line = line.sale_line_id
            if all(move.product_id == sale_line.product_id for move in moves):
                quantity = 0.0
                for move in moves:
                    done = compat.move_done_qty(move)
                    if move.uom_id and move.product_id.uom_id and move.uom_id != move.product_id.uom_id:
                        done = move.uom_id._compute_quantity(done, move.product_id.uom_id)
                    quantity += done
                units = int(round(quantity))
            else:
                # A kit ships component by component and can be split across a backorder, so it is counted on the
                # whole order below instead of on this delivery.
                kits[line] = sale_line
                continue
            if units > 0:
                shipped[line.shopify_gid] = shipped.get(line.shopify_gid, 0) + units
        kit_gids = {line.shopify_gid for line in kits}
        for record in self.env['eh.shopify.fulfillment'].search([('picking_id', '=', picking.id)]):
            for gid, quantity in (record.quantities or {}).items():
                if gid not in kit_gids:
                    shipped[gid] = shipped.get(gid, 0) - int(quantity)
        if kits:
            recorded = {}
            for record in self.env['eh.shopify.fulfillment'].search([('order_binding_id', '=', self.id)]):
                for gid, quantity in (record.quantities or {}).items():
                    recorded[gid] = recorded.get(gid, 0) + int(quantity)
            for line, sale_line in kits.items():
                delivered = sale_line.move_ids.filtered(
                    lambda move: move.state == 'done' and move.picking_id.picking_type_code == 'outgoing')
                units = self._kit_units_shipped(sale_line, delivered) - recorded.get(line.shopify_gid, 0)
                if units > 0:
                    shipped[line.shopify_gid] = shipped.get(line.shopify_gid, 0) + units
        return {gid: quantity for gid, quantity in shipped.items() if quantity > 0}

    def _kit_units_shipped(self, sale_line, moves):
        """Whole kits shipped in these component moves. A kit counts only once
        every component of its bill of materials has been shipped, which is why
        the caller passes every delivered move of the line, not one delivery."""
        if 'bom_line_id' not in self.env['stock.move']._fields:
            return 0
        per_component = {}
        for move in moves:
            bom_line = move.bom_line_id
            if not bom_line:
                return 0
            done = compat.move_done_qty(move)
            if move.uom_id and bom_line.product_uom_id and move.uom_id != bom_line.product_uom_id:
                done = move.uom_id._compute_quantity(done, bom_line.product_uom_id)
            per_component[bom_line] = per_component.get(bom_line, 0.0) + done
        bom = next(iter(per_component)).bom_id
        components = bom.bom_line_ids
        if hasattr(components, '_skip_bom_line'):
            components = components.filtered(lambda component: not component._skip_bom_line(sale_line.product_id))
        if not components or any(component not in per_component for component in components):
            return 0
        units = min(per_component[component] / (component.product_qty / (bom.product_qty or 1.0))
                    for component in components if component.product_qty)
        return int(units + 1e-6)

    def _fulfillment_orders(self, client):
        version = self.store_id.api_version
        doc = documents.ORDER_FULFILLMENT_ORDERS
        response = client.execute(doc.text_for(version), {'id': self.shopify_gid, 'first': doc.page_size,
                                                          'after': None})
        node = (response.data or {}).get('node') or {}
        orders = client.complete_connection(node, 'fulfillmentOrders', doc.text_for(version), self.shopify_gid,
                                            ('node', 'fulfillmentOrders'), page_size=doc.page_size)
        line_doc = documents.FULFILLMENT_ORDER_LINE_ITEMS
        for fulfillment_order in orders:
            fulfillment_order['lineItems'] = {'nodes': client.complete_connection(
                fulfillment_order, 'lineItems', line_doc.text_for(version), fulfillment_order['id'],
                ('node', 'lineItems'), page_size=line_doc.page_size)}
        return orders

    def _shopify_location_for(self, picking):
        Location = self.env['eh.shopify.location']
        candidates = Location.search([('store_id', '=', self.store_id.id), ('removed_in_shopify', '=', False)])
        warehouse = picking.picking_type_id.warehouse_id
        match = candidates.filtered(lambda location: location.warehouse_id == warehouse)
        if match:
            return match[:1]
        return candidates[:1] if len(candidates) == 1 else Location

    def _tracking_for(self, picking):
        info = {}
        if not compat.carrier_fields_available(self.env):
            return info
        numbers = [n for n in re.split(r'[,;\s]+', picking.carrier_tracking_ref or '') if n]
        if numbers:
            info['numbers'] = numbers
        carrier = picking.carrier_id
        if carrier:
            mapping = self.env['eh.shopify.carrier'].search([('store_id', '=', self.store_id.id),
                                                             ('carrier_id', '=', carrier.id)], limit=1)
            info['company'] = (mapping.tracking_company if mapping else False) or carrier.name
        return info

    # ------------------------------------------------------------------ recording
    @api.model
    def _record_fulfillment(self, vals):
        """Persist a created fulfillment at once, outside the job transaction."""
        if compat.in_test_mode(self.env):
            return self.env['eh.shopify.fulfillment'].sudo().create(vals)
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record_id = env['eh.shopify.fulfillment'].create(vals).id
        return self.env['eh.shopify.fulfillment'].sudo().browse(record_id)

    def _refresh_fulfilled_quantities(self, extra=None):
        """``extra`` lists (fulfillment gid, quantities) just committed in another
        cursor, which this transaction cannot see yet."""
        by_fulfillment = {record.shopify_gid or record.id: record.quantities or {}
                          for record in self.env['eh.shopify.fulfillment'].search([('order_binding_id', '=', self.id)])}
        for gid, quantities in extra or []:
            by_fulfillment[gid] = quantities
        totals = {}
        for quantities in by_fulfillment.values():
            for gid, quantity in quantities.items():
                totals[gid] = totals.get(gid, 0) + int(quantity)
        for line in self.line_ids.filtered('shopify_gid'):
            line.fulfilled_quantity = totals.get(line.shopify_gid, 0)

    def _move_fulfillment_orders(self, client, orders, moves, location, picking):
        """Move Shopify fulfillment orders to the location this delivery shipped
        from. Not idempotent in Shopify: a retry re reads fulfillment orders first,
        so an order already moved is simply fulfilled where it now sits."""
        by_id = {fulfillment_order['id']: fulfillment_order for fulfillment_order in orders}
        document = documents.FULFILLMENT_ORDER_MOVE
        text = self.store_id._doc_text(document)
        moved, problems = [], []
        for move in moves:
            fulfillment_order = by_id.get(move['fulfillmentOrderId']) or {}
            number = move['fulfillmentOrderId'].rsplit('/', 1)[-1]
            if 'MOVE' not in {action.get('action') for action in fulfillment_order.get('supportedActions') or []}:
                problems.append(_('Shopify does not allow moving fulfillment order %s to another location.') % number)
                continue
            wanted = dict(move['lineItems'])
            items = []
            for line in (fulfillment_order.get('lineItems') or {}).get('nodes') or []:
                gid = (line.get('lineItem') or {}).get('id')
                quantity = min(int(line.get('remainingQuantity') or 0), wanted.get(gid, 0))
                if quantity > 0:
                    items.append({'id': line['id'], 'quantity': quantity})
                    wanted[gid] -= quantity
            if not items:
                continue
            try:
                client.mutate(text, {'id': fulfillment_order['id'], 'newLocationId': location.shopify_gid,
                                     'fulfillmentOrderLineItems': items}, document.payload_key)
            except errors.UserErrors as error:
                problems.append(_('Shopify refused to move fulfillment order %(number)s to %(location)s: %(error)s')
                                % {'number': number, 'location': location.name, 'error': error.message})
                continue
            moved.append(number)
        if moved:
            picking.message_post(body=_('Moved Shopify fulfillment order %(numbers)s to %(location)s, where this '
                                        'delivery shipped from.') % {'numbers': ', '.join(moved),
                                                                     'location': location.name})
        return moved, problems

    # ------------------------------------------------------------------ push
    def _push_fulfillment(self, picking):
        self.ensure_one()
        store = self.store_id
        location = self._shopify_location_for(picking)
        if location and location.delivery_mode == 'shopify':
            return {'skipped': _('Shopify or another app ships from %s.') % location.name}
        shipped = self._shipped_quantities(picking)
        if not shipped:
            return {'skipped': 'nothing new to send'}
        client = store._client()
        orders = self._fulfillment_orders(client)
        plan = fulfillment_plan.plan(orders, shipped, location_gid=location.shopify_gid if location else None)
        move_problems = []
        if plan['moves'] and location and store.move_fulfillment_orders:
            moved, move_problems = self._move_fulfillment_orders(client, orders, plan['moves'], location, picking)
            if moved:
                orders = self._fulfillment_orders(client)
                plan = fulfillment_plan.plan(orders, shipped, location_gid=location.shopify_gid)
        # A pickup is collected at the counter: no tracking, and Shopify already
        # told the customer when the order was marked ready.
        tracking = {} if self.is_pickup else self._tracking_for(picking)
        notify = bool(store.notify_customer_on_fulfillment) and not self.is_pickup
        created = []
        created_quantities = []
        for request in plan['requests']:
            variables = {'fulfillment': {'notifyCustomer': notify, 'lineItemsByFulfillmentOrder': [request]}}
            if tracking:
                variables['fulfillment']['trackingInfo'] = tracking
            payload = client.mutate(documents.FULFILLMENT_CREATE.text_for(store.api_version), variables,
                                    documents.FULFILLMENT_CREATE.payload_key)
            fulfillment = payload.get('fulfillment') or {}
            owner = next(fo for fo in orders if fo['id'] == request['fulfillmentOrderId'])
            gid_by_fo_line = {line['id']: (line.get('lineItem') or {}).get('id')
                              for line in owner['lineItems']['nodes']}
            quantities = {}
            for line in request['fulfillmentOrderLineItems']:
                gid = gid_by_fo_line.get(line['id'])
                quantities[gid] = quantities.get(gid, 0) + line['quantity']
            self._record_fulfillment({
                'store_id': store.id, 'order_binding_id': self.id, 'picking_id': picking.id,
                'shopify_gid': fulfillment.get('id'), 'name': fulfillment.get('name'),
                'status': fulfillment.get('status'), 'tracking_company': tracking.get('company'),
                'tracking_numbers': ', '.join(tracking.get('numbers') or []), 'quantities': quantities,
                'notified_customer': notify})
            created.append(fulfillment.get('name') or fulfillment.get('id'))
            created_quantities.append((fulfillment.get('id'), quantities))
        self._refresh_fulfilled_quantities(extra=created_quantities)
        problems = list(move_problems)
        for blocked in plan['blocked']:
            problems.append(_('Shopify fulfillment order %(id)s is %(reason)s%(detail)s.') % {
                'id': blocked['fulfillmentOrderId'].rsplit('/', 1)[-1], 'reason': blocked['reason'].replace('_', ' '),
                'detail': (' (%s)' % blocked['detail']) if blocked.get('detail') else ''})
        if plan['moves']:
            problems.append(_('Shopify expects part of this shipment from another location. Move the fulfillment '
                              'order in Shopify, or ship from the assigned location, then retry.'))
        if plan['unallocated'] and not plan['blocked'] and not plan['moves']:
            problems.append(_('Shopify has nothing left to fulfil for part of this shipment.'))
        if created:
            picking.message_post(body=_('Sent to Shopify as %s.') % ', '.join(created))
        if problems and not created:
            raise errors.MappingError(' '.join(problems))
        if problems:
            picking.message_post(body=' '.join(problems))
        return {'created': created, 'problems': problems}
