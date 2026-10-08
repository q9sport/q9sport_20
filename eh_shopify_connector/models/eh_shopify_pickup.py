# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Local pickup orders.

Shopify only tells a customer to come and collect once the order is marked
ready. Odoo does that from the delivery (a button, or automatically when the
location says so once stock is reserved). Collection is then recorded by
validating the delivery, which creates the fulfillment without tracking and
without a second email.
"""
from odoo import _, api, fields, models

from ..lib import documents, errors

OPEN_STATUSES = ('OPEN', 'IN_PROGRESS')


class ShopifyOrderPickup(models.Model):
    _inherit = 'eh.shopify.order'

    is_pickup = fields.Boolean('Local pickup', readonly=True)

    def _upsert_binding(self, store, order, state, reason):
        binding = super()._upsert_binding(store, order, state, reason)
        nodes = (order.get('fulfillmentOrders') or {}).get('nodes') or []
        is_pickup = any(((node.get('deliveryMethod') or {}).get('methodType')) == 'PICK_UP' for node in nodes)
        if binding.is_pickup != is_pickup:
            binding.is_pickup = is_pickup
        return binding

    @api.model
    def _enqueue_pickup_ready(self, picking, binding):
        return self.env['eh.shopify.job'].sudo()._enqueue(
            binding.store_id, 'pickup.ready', name=_('Tell Shopify %s is ready for pickup') % picking.name,
            payload={'picking_id': picking.id}, dedup_key='pickup:%s' % picking.id, resource_key=binding.shopify_gid,
            priority=4, record=picking)

    def _job_pickup_ready(self, job):
        store = job.store_id
        picking = self.env['stock.picking'].browse((job.payload or {}).get('picking_id')).exists()
        if not picking or picking.state in ('done', 'cancel'):
            return {'skipped': 'the delivery is closed'}
        order = picking.sale_id if 'sale_id' in picking._fields else picking.move_ids.sale_line_id.order_id[:1]
        binding = order.sudo().eh_shopify_order_id
        if not binding or binding.store_id != store:
            return {'skipped': 'not an order of this store'}
        client = store._client()
        location = binding._shopify_location_for(picking)
        candidates = [fo for fo in binding._fulfillment_orders(client)
                      if (fo.get('deliveryMethod') or {}).get('methodType') == 'PICK_UP'
                      and fo.get('status') in OPEN_STATUSES]
        if location:
            at_location = [fo for fo in candidates
                           if ((fo.get('assignedLocation') or {}).get('location') or {}).get('id') == location.shopify_gid]
            candidates = at_location or candidates
        if not candidates:
            return {'skipped': 'Shopify has no pickup waiting for this order'}
        document = documents.FULFILLMENT_ORDER_PREPARED_FOR_PICKUP
        try:
            client.mutate(store._doc_text(document), {'input': {'lineItemsByFulfillmentOrder': [
                {'fulfillmentOrderId': fo['id']} for fo in candidates]}}, document.payload_key)
        except errors.UserErrors as error:
            raise errors.MappingError(_('Shopify refused to mark %(picking)s ready for pickup: %(error)s') % {
                'picking': picking.name, 'error': error.message})
        picking.write({'eh_shopify_pickup_ready_at': fields.Datetime.now()})
        picking.message_post(body=_('Shopify told the customer the order is ready for pickup.'))
        return {'ready': [fo['id'] for fo in candidates]}


class StockPickingPickup(models.Model):
    _inherit = 'stock.picking'

    eh_shopify_is_pickup = fields.Boolean('Shopify local pickup', compute='_compute_eh_shopify_is_pickup')
    eh_shopify_pickup_ready_at = fields.Datetime('Ready for pickup since', readonly=True, copy=False)

    def _eh_shopify_order_binding(self):
        self.ensure_one()
        order = self.sudo().move_ids.sale_line_id.order_id[:1]
        return order.eh_shopify_order_id

    def _compute_eh_shopify_is_pickup(self):
        for picking in self:
            binding = picking._eh_shopify_order_binding() if picking.id else False
            picking.eh_shopify_is_pickup = bool(binding and binding.is_pickup and picking.picking_type_code == 'outgoing')

    def action_eh_shopify_ready_for_pickup(self):
        for picking in self:
            binding = picking._eh_shopify_order_binding()
            if binding and binding.is_pickup and binding.store_id.state in ('connected', 'attention'):
                self.env['eh.shopify.order'].sudo()._enqueue_pickup_ready(picking, binding)
        return True

    def _eh_shopify_auto_pickup_ready(self):
        for picking in self.filtered(lambda p: p.state == 'assigned' and p.picking_type_code == 'outgoing'
                                     and not p.eh_shopify_pickup_ready_at):
            binding = picking._eh_shopify_order_binding()
            if not binding or not binding.is_pickup or binding.store_id.state not in ('connected', 'attention'):
                continue
            location = binding.sudo()._shopify_location_for(picking)
            if location and location.pickup_ready_when_reserved:
                self.env['eh.shopify.order'].sudo()._enqueue_pickup_ready(picking, binding)


class StockMovePickup(models.Model):
    _inherit = 'stock.move'

    def _action_assign(self, force_qty=False):
        result = super()._action_assign(force_qty=force_qty)
        self.mapped('picking_id')._eh_shopify_auto_pickup_ready()
        return result
