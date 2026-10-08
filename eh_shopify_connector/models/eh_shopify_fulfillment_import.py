# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Bring shipments made in Shopify into Odoo.

For locations shipped by Shopify or another app (a logistics partner or a
fulfilment service), each successful Shopify fulfillment validates the
matching Odoo delivery for exactly the shipped quantities, and the rest stays
on a backorder. The validation and the fulfillment record are written in the
same job transaction, so importing twice never ships twice.
"""
import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .. import compat
from ..lib import documents, errors
from .eh_shopify_order_import import HISTORICAL

SHIPPED_STATUSES = ('FULFILLED', 'PARTIALLY_FULFILLED', 'IN_PROGRESS')


class ShopifyOrderFulfillmentImport(models.Model):
    _inherit = 'eh.shopify.order'

    # ------------------------------------------------------------------ triggers
    @api.model
    def _enqueue_fulfillment_import(self, store, gid, priority=5):
        return self.env['eh.shopify.job'].sudo()._enqueue(
            store, 'fulfillment.import', name=_('Import shipments of order %s') % gid.rsplit('/', 1)[-1],
            payload={'gid': gid}, dedup_key='fulfillment-import:%s' % gid, resource_key=gid, priority=priority)

    def _till_location(self):
        """The till a point of sale order was sold at. Its goods left the shop at the
        sale, so its Shopify fulfillment validates the Odoo delivery whatever the
        location's delivery setting."""
        Location = self.env['eh.shopify.location'].sudo()
        if not self or not self.is_pos or not self.retail_location_gid:
            return Location
        return Location.search([('store_id', '=', self.store_id.id), ('shopify_gid', '=', self.retail_location_gid)],
                               limit=1)

    @api.model
    def _shopify_ship_locations(self, store):
        return self.env['eh.shopify.location'].sudo().search([('store_id', '=', store.id),
                                                              ('delivery_mode', '=', 'shopify')])

    def _apply_to_odoo(self, order):
        result = super()._apply_to_odoo(order)
        sale_order = self.sale_order_id
        if (sale_order and sale_order.state in ('sale', 'done') and not order.get('cancelledAt')
                and not self.env.context.get(HISTORICAL)
                and order.get('displayFulfillmentStatus') in SHIPPED_STATUSES
                and (self._shopify_ship_locations(self.store_id) or self._till_location())):
            self._enqueue_fulfillment_import(self.store_id, self.shopify_gid)
        return result

    def _job_fulfillment_import(self, job):
        store = job.store_id
        gid = (job.payload or {}).get('gid')
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', gid)], limit=1)
        if not binding or not binding.sale_order_id:
            return {'skipped': 'the order is not in Odoo yet'}
        if binding.sale_order_id.state not in ('sale', 'done'):
            return {'skipped': 'the sales order is not confirmed'}
        locations = self._shopify_ship_locations(store) | binding._till_location()
        if not locations and not binding.fulfillment_ids:
            return {'skipped': 'every location is shipped by Odoo'}
        return binding._import_fulfillments(locations)

    # ------------------------------------------------------------------ reading
    def _read_fulfillments(self, client):
        store = self.store_id
        data = client.execute(store._doc_text(documents.ORDER_FULFILLMENTS), {'id': self.shopify_gid}).data or {}
        order = data.get('order')
        if not order:
            raise errors.NotFound(_('Shopify order %s no longer exists.') % self.shopify_gid)
        fulfillments = order.get('fulfillments') or []
        count = (order.get('fulfillmentsCount') or {}).get('count')
        if count is not None and count > len(fulfillments):
            raise errors.IncompleteData(_('Order %(order)s has %(count)s shipments in Shopify, more than can be read '
                                          'at once. Record the older ones by hand.') % {
                'order': self.name or self.shopify_gid, 'count': count})
        line_doc = documents.FULFILLMENT_LINE_ITEMS
        text = store._doc_text(line_doc)
        for fulfillment in fulfillments:
            fulfillment['fulfillmentLineItems'] = {'nodes': client.complete_connection(
                fulfillment, 'fulfillmentLineItems', text, fulfillment['id'], ('node', 'fulfillmentLineItems'),
                page_size=line_doc.page_size)}
        return fulfillments

    # ------------------------------------------------------------------ import
    def _import_fulfillments(self, locations):
        self.ensure_one()
        store = self.store_id
        fulfillments = self._read_fulfillments(store._client())
        by_location = {location.shopify_gid: location for location in locations}
        Record = self.env['eh.shopify.fulfillment'].sudo()
        known = {record.shopify_gid: record for record in Record.search(
            [('store_id', '=', store.id), ('shopify_gid', 'in', [f['id'] for f in fulfillments])])}
        imported, problems, notes = [], [], []
        for fulfillment in sorted(fulfillments, key=lambda f: (f.get('createdAt') or '', f['id'])):
            record = known.get(fulfillment['id'])
            if record:
                if fulfillment.get('status') == 'CANCELLED' and record.status != 'CANCELLED':
                    notes.append(self._fulfillment_cancelled(record))
                continue
            location = by_location.get((fulfillment.get('location') or {}).get('id'))
            if fulfillment.get('status') != 'SUCCESS' or not location:
                continue
            try:
                with self.env.cr.savepoint():
                    self._validate_for_fulfillment(fulfillment, location)
            except (errors.MappingError, UserError, ValidationError) as error:
                problems.append(getattr(error, 'message', None) or str(error))
                continue
            imported.append(fulfillment.get('name') or fulfillment['id'])
        if imported:
            self._refresh_fulfilled_quantities()
        if problems and not imported:
            raise errors.MappingError(' '.join(problems))
        if problems:
            # The shipments that worked are kept; the others are tried again
            # later, so they reach the exceptions inbox if they keep failing.
            self.sale_order_id.message_post(body=' '.join(problems))
            self.env['eh.shopify.job'].sudo()._enqueue(
                store, 'fulfillment.import', name=_('Import shipments of order %s again') % (self.name or self.shopify_gid),
                payload={'gid': self.shopify_gid}, dedup_key='fulfillment-import-retry:%s' % self.shopify_gid,
                resource_key=self.shopify_gid, priority=7,
                run_at=fields.Datetime.now() + datetime.timedelta(hours=1))
        return {'imported': imported, 'problems': problems, 'notes': notes}

    def _validate_for_fulfillment(self, fulfillment, location):
        """Validate the Odoo deliveries for one Shopify fulfillment, all or nothing."""
        sale_order = self.sale_order_id
        label = fulfillment.get('name') or fulfillment['id'].rsplit('/', 1)[-1]
        pickings = sale_order.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and p.state not in ('draft', 'done', 'cancel'))
        if location.warehouse_id:
            in_warehouse = pickings.filtered(lambda p: p.picking_type_id.warehouse_id == location.warehouse_id)
            if pickings and not in_warehouse:
                raise errors.MappingError(_('Shopify shipment %(name)s left from %(location)s, but the open deliveries of '
                                            '%(order)s are in another warehouse.') % {
                    'name': label, 'location': location.name, 'order': sale_order.name})
            pickings = in_warehouse
        if not pickings:
            raise errors.MappingError(_('Shopify shipment %(name)s has no open delivery left in %(order)s.') % {
                'name': label, 'order': sale_order.name})
        shipped = {}
        for line in fulfillment['fulfillmentLineItems']['nodes']:
            gid = (line.get('lineItem') or {}).get('id')
            if gid:
                shipped[gid] = shipped.get(gid, 0) + int(line.get('quantity') or 0)
        planned, missing = self._plan_move_quantities(pickings.sorted('id'), shipped, label)
        if missing:
            raise errors.MappingError(_('Part of Shopify shipment %(name)s (%(items)s) has no open delivery line in '
                                        'Odoo, so nothing was recorded.') % {'name': label, 'items': ', '.join(missing)})
        if not planned:
            raise errors.MappingError(_('Nothing in Shopify shipment %s matches an open delivery line.') % label)
        touched = pickings.filtered(lambda p: any(move.id in planned for move in p.move_ids))
        numbers, company = self._fulfillment_tracking(fulfillment)
        vals = {}
        if compat.carrier_fields_available(self.env):
            if numbers:
                vals['carrier_tracking_ref'] = ', '.join(numbers)
            if company and not touched[:1].carrier_id:
                carrier = self._carrier_for_tracking_company(company)
                if carrier:
                    vals['carrier_id'] = carrier.id
        for picking in touched:
            if vals:
                picking.write(vals)
            quantities = {move.id: planned[move.id] for move in picking.move_ids if move.id in planned}
            compat.validate_picking(picking.with_context(eh_shopify_from_shopify=True), quantities=quantities,
                                    backorder=True)
            if picking.state != 'done':
                raise errors.MappingError(_('Odoo did not validate %(picking)s for Shopify shipment %(name)s. Open the '
                                            'delivery to finish it.') % {'picking': picking.name, 'name': label})
        self.env['eh.shopify.fulfillment'].sudo().create({
            'store_id': self.store_id.id, 'order_binding_id': self.id, 'picking_id': touched[:1].id,
            'shopify_gid': fulfillment['id'], 'name': fulfillment.get('name'), 'status': fulfillment.get('status'),
            'tracking_company': company, 'tracking_numbers': ', '.join(numbers), 'quantities': shipped,
            'notified_customer': False, 'origin': 'shopify'})
        for picking in touched:
            picking.message_post(body=_('Shipped in Shopify as %(name)s from %(location)s.') % {
                'name': label, 'location': location.name})
        return touched

    def _plan_move_quantities(self, pickings, shipped, label):
        """Done quantity per move id across the open deliveries, and the Shopify
        lines that have no open delivery line at all."""
        lines = {line.shopify_gid: line for line in self.line_ids if line.shopify_gid and line.sale_line_id}
        uom_field = compat.sale_line_uom_field()
        planned = {}
        missing = []
        for gid, units in shipped.items():
            line = lines.get(gid)
            if not line or units <= 0:
                continue
            sale_line = line.sale_line_id
            moves = pickings.move_ids.filtered(
                lambda move, sale_line=sale_line: move.sale_line_id == sale_line and move.state not in ('done', 'cancel'))
            if not moves:
                missing.append(line.sku or sale_line.name)
                continue
            tracked = moves.filtered(lambda move: move.product_id.tracking in ('lot', 'serial'))
            if tracked:
                raise errors.MappingError(_(
                    '%(product)s needs lot or serial numbers, so Shopify shipment %(name)s cannot be recorded '
                    'automatically. Validate the delivery by hand.') % {
                    'product': tracked[0].product_id.display_name, 'name': label})
            if all(move.product_id == sale_line.product_id for move in moves):
                product_uom = sale_line.product_id.uom_id
                remaining = sale_line[uom_field]._compute_quantity(units, product_uom)
                for move in moves:
                    capacity = move.uom_id._compute_quantity(move.product_uom_qty, product_uom)
                    take = remaining if move == moves[-1] else min(remaining, capacity)
                    planned[move.id] = planned.get(move.id, 0.0) + product_uom._compute_quantity(take, move.uom_id)
                    remaining -= take
                    if remaining <= 0:
                        break
            else:
                # Kit: components move in proportion to the kit units shipped.
                open_units = sale_line.product_uom_qty - sale_line.qty_delivered
                fraction = min(1.0, units / open_units) if open_units > 0 else 1.0
                for move in moves:
                    planned[move.id] = planned.get(move.id, 0.0) + move.product_uom_qty * fraction
        Move = self.env['stock.move']
        rounded = {move_id: Move.browse(move_id).uom_id.round(quantity) for move_id, quantity in planned.items()}
        return rounded, missing

    def _carrier_for_tracking_company(self, company):
        """Odoo carrier for a Shopify tracking company, created once when allowed."""
        store = self.store_id
        Mapping = self.env['eh.shopify.carrier'].sudo()
        Carrier = self.env['delivery.carrier'].sudo()
        mapping = Mapping.search([('store_id', '=', store.id), ('tracking_company', '=ilike', company),
                                  ('carrier_id', '!=', False)], limit=1)
        if mapping:
            return mapping.carrier_id
        if not store.auto_create_carriers:
            return Carrier
        mapping = Mapping.search([('store_id', '=', store.id), ('tracking_company', '=ilike', company)], limit=1)
        if not mapping:
            mapping = Mapping.create({'store_id': store.id, 'title': company, 'code': 'tracking',
                                      'source': 'fulfillment', 'tracking_company': company})
        carrier = Carrier.search([('name', '=ilike', company), ('company_id', 'in', (False, store.company_id.id))],
                                 limit=1)
        if not carrier:
            carrier = Carrier.create({'name': company, 'delivery_type': 'fixed',
                                      'product_id': self._extra_product('shipping').id})
        mapping.carrier_id = carrier
        return carrier

    @staticmethod
    def _fulfillment_tracking(fulfillment):
        tracking = fulfillment.get('trackingInfo') or []
        numbers = []
        for info in tracking:
            number = (info.get('number') or '').strip()
            if number and number not in numbers:
                numbers.append(number)
        company = next((info.get('company') for info in tracking if info.get('company')), None)
        return numbers, company

    def _fulfillment_cancelled(self, record):
        record.status = 'CANCELLED'
        note = _('Shopify cancelled shipment %(name)s after Odoo recorded delivery %(picking)s. If the goods came '
                 'back, create the return in Odoo.') % {'name': record.name or record.shopify_gid,
                                                        'picking': record.picking_id.name or _('none')}
        sale_order = self.sale_order_id
        if sale_order:
            sale_order.activity_schedule('mail.mail_activity_data_todo', summary=_('Shopify shipment cancelled'),
                                         note=note, user_id=(sale_order.user_id or self.env.user).id)
        return note
