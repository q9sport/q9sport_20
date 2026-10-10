# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import _, models

from .. import compat


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def write(self, vals):
        watched = {'carrier_tracking_ref', 'carrier_id'} & set(vals) & set(self._fields)
        before = {}
        if watched and not self.env.context.get('eh_shopify_from_shopify'):
            before = {picking.id: (picking.carrier_tracking_ref, picking.carrier_id.id)
                      for picking in self if picking.state == 'done'}
        result = super().write(vals)
        if before:
            changed = self.filtered(lambda picking: picking.id in before and
                                    before[picking.id] != (picking.carrier_tracking_ref, picking.carrier_id.id))
            changed._eh_shopify_queue_tracking()
        return result

    def _eh_shopify_queue_tracking(self):
        """Queue a tracking update for deliveries Odoo already sent to Shopify."""
        Record = self.env['eh.shopify.fulfillment'].sudo()
        Job = self.env['eh.shopify.job'].sudo()
        for picking in self:
            records = Record.search([('picking_id', '=', picking.id), ('origin', '=', 'odoo'),
                                     ('shopify_gid', '!=', False), ('status', '!=', 'CANCELLED')])
            binding = records[:1].order_binding_id
            if not binding or binding.store_id.state not in ('connected', 'attention'):
                continue
            Job._enqueue_after_running(binding.store_id, 'fulfillment.tracking', 'tracking:%s' % picking.id,
                                       name=_('Send new tracking of %s to Shopify') % picking.name,
                                       payload={'picking_id': picking.id}, resource_key=binding.shopify_gid,
                                       priority=5, record=picking)

    def _action_done(self):
        result = super()._action_done()
        self._eh_shopify_queue_fulfillments()
        return result

    def _eh_shopify_queue_fulfillments(self):
        """Queue the Shopify fulfillment of every validated Shopify delivery. The
        job only exists if the validation itself is committed."""
        if self.env.context.get('eh_shopify_from_shopify'):
            return
        Job = self.env['eh.shopify.job'].sudo()
        for picking in self.filtered(lambda p: p.state == 'done' and p.picking_type_code == 'outgoing'):
            # Read as the connector: a warehouse user who ships is not always allowed to read Shopify records.
            order = compat.picking_sale_order(picking.sudo())
            binding = order.eh_shopify_order_id if order else False
            if not binding or binding.store_id.state not in ('connected', 'attention'):
                continue
            Job._enqueue(binding.store_id, 'fulfillment.push', name=_('Send shipment %s to Shopify') % picking.name,
                         payload={'picking_id': picking.id}, dedup_key='fulfillment:%s' % picking.id,
                         resource_key=binding.shopify_gid, priority=4, record=picking)
            if binding.store_id.invoice_policy == 'delivered':
                binding._enqueue_invoice()
