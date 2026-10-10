# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify returns become Odoo return transfers.

A requested or approved return prepares a return transfer for the units not
covered yet, from the deliveries that shipped them, whole units at a time and
never more than a delivery can still take back. Nothing moves yet. Each sync
compares what Shopify disposed as restocked with what Odoo received: with
automatic validation the difference is received and the rest keeps waiting,
and when Shopify closes the return the units that were not put back in stock
are cancelled. Units restocked at a Shopify location mapped to another warehouse
are received into that warehouse. When the store names a location for units not
restocked, units Shopify marks as not restocked or needing processing are
received there instead of being cancelled; missing units are always cancelled.
Kits are returned and received by their components. Declined or
cancelled returns cancel a transfer still waiting, a transfer already received
is flagged instead of being reversed silently, and a transfer cancelled in Odoo
is not created again unless Shopify reopens the return. A change that arrives
while a sync runs is synced again after it, a return that arrives before its
order is tried again, and every order update looks for returns Odoo missed.

Once a return with exchange items is approved, one exchange order is created
for the replacement goods, tied to the Shopify order so its shipment is sent to
Shopify like any other. By default it is confirmed at no charge, because the
price difference is paid or refunded in Shopify on the original order; a store
can choose a quotation at Shopify prices instead. An exchange order not shipped
yet is cancelled when the return is declined or cancelled, and cancelling it in
Odoo never cancels the Shopify order.
"""
import datetime

from odoo import _, api, fields, models

from .. import compat
from ..lib import documents, errors, order_math
from .eh_shopify_order_import import HISTORICAL

WAITING = ('REQUESTED', 'OPEN')
STOPPED = ('DECLINED', 'CANCELED')
ORDER_WAIT = datetime.timedelta(minutes=15)
MAX_ORDER_WAITS = 96
EPSILON = 1e-6
UNSELLABLE = ('NOT_RESTOCKED', 'PROCESSING_REQUIRED')


def _open(picking):
    return picking.state not in ('done', 'cancel')


def _product_qty(move, quantity):
    """``quantity`` in the move unit, expressed in the product unit."""
    return quantity * move.product_qty / move.product_uom_qty if move.product_uom_qty else quantity


def _move_qty(move, quantity):
    return quantity * move.product_uom_qty / move.product_qty if move.product_qty else quantity


class ShopifyReturn(models.Model):
    _name = 'eh.shopify.return'
    _description = 'Shopify Return'
    _order = 'id desc'
    _rec_name = 'name'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    order_binding_id = fields.Many2one('eh.shopify.order', 'Shopify order', ondelete='cascade', index=True)
    sale_order_id = fields.Many2one('sale.order', 'Sales order', related='order_binding_id.sale_order_id')
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    name = fields.Char(readonly=True)
    status = fields.Char('Status in Shopify', readonly=True)
    picking_ids = fields.Many2many('stock.picking', 'eh_shopify_return_picking_rel', 'return_id', 'picking_id',
                                   string='Return transfers', readonly=True)
    state = fields.Selection([
        ('waiting', 'Waiting for the goods'),
        ('received', 'Received'),
        ('cancelled', 'Cancelled'),
        ('error', 'Needs attention'),
    ], default='waiting', required=True, readonly=True, index=True)
    reason = fields.Char(readonly=True)
    exchange_note = fields.Char('Exchange', readonly=True)
    exchange_order_id = fields.Many2one('sale.order', 'Exchange order', readonly=True, copy=False)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_return_gid_uniq "
                            "ON eh_shopify_return (store_id, shopify_gid)")

    @api.model
    def _enqueue_return_sync(self, store, gid, order_gid=None, priority=5):
        return self.env['eh.shopify.job'].sudo()._enqueue_after_running(
            store, 'return.sync', 'return:%s' % gid, name=_('Sync return %s') % gid.rsplit('/', 1)[-1],
            payload={'gid': gid, 'order_gid': order_gid}, resource_key=order_gid or gid, priority=priority)

    # ------------------------------------------------------------------ reading
    def _read_return(self, store, gid):
        client = store._client()
        data = client.execute(store._doc_text(documents.RETURN), {'id': gid}).data or {}
        record = data.get('node')
        if not record:
            return None
        for key, document in (('returnLineItems', documents.RETURN_LINE_ITEMS),
                              ('reverseFulfillmentOrders', documents.RETURN_REVERSE_ORDERS),
                              ('exchangeLineItems', documents.RETURN_EXCHANGE_LINES)):
            record[key] = {'nodes': client.complete_connection(record, key, store._doc_text(document), gid,
                                                               ('node', key), page_size=document.page_size)}
        lines_document = documents.REVERSE_ORDER_LINES
        for reverse_order in record['reverseFulfillmentOrders']['nodes']:
            reverse_order['lineItems'] = {'nodes': client.complete_connection(
                reverse_order, 'lineItems', store._doc_text(lines_document), reverse_order['id'], ('node', 'lineItems'),
                page_size=lines_document.page_size)}
        return record

    # ------------------------------------------------------------------ job
    def _job_return_sync(self, job):
        store = job.store_id
        payload = job.payload or {}
        gid = payload.get('gid')
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', gid)], limit=1)
        record = self._read_return(store, gid)
        if record is None:
            if binding:
                binding.write({'state': 'error', 'reason': _('Shopify no longer has this return.')})
            return {'missing': gid}
        order_gid = (record.get('order') or {}).get('id') or payload.get('order_gid')
        Order = self.env['eh.shopify.order']
        order_binding = Order.search([('store_id', '=', store.id), ('shopify_gid', '=', order_gid)], limit=1)
        if not order_binding.sale_order_id:
            Order._enqueue_import(store, order_gid)
            waits = int(payload.get('waits') or 0)
            if waits < MAX_ORDER_WAITS:
                self.env['eh.shopify.job'].sudo()._enqueue(
                    store, 'return.sync', name=job.name, payload=dict(payload, waits=waits + 1),
                    dedup_key='return:%s:wait' % gid, resource_key=gid, priority=job.priority,
                    run_at=fields.Datetime.now() + ORDER_WAIT)
            return {'waiting': 'the order is not in Odoo yet'}
        previous = binding.status
        vals = {'store_id': store.id, 'order_binding_id': order_binding.id, 'shopify_gid': gid,
                'name': record.get('name') or gid.rsplit('/', 1)[-1], 'status': record.get('status')}
        if binding:
            binding.write(vals)
        else:
            binding = self.create(vals)
        status = record.get('status')
        if status in STOPPED:
            return binding._stop()
        plan = binding._plan(record, order_binding)
        reopened = previous == 'CLOSED' and status in WAITING
        binding._prepare_pickings(plan, order_binding, reopened)
        if store.return_auto_validate:
            binding._receive_restocked(plan)
            if status == 'CLOSED':
                binding.picking_ids.filtered(_open).action_cancel()
        binding._refresh_state(status)
        binding._sync_exchange(record, order_binding)
        return {'state': binding.state, 'pickings': binding.picking_ids.mapped('name'),
                'exchange_order': binding.exchange_order_id.name or False}

    # ------------------------------------------------------------------ figures
    def _sale_lines(self, order_binding):
        return {line.shopify_gid: line.sale_line_id for line in order_binding.line_ids
                if line.shopify_gid and line.sale_line_id}

    def _plan(self, record, order_binding):
        """Units requested and units to receive per sale line. ``targets`` maps
        a destination location, or False for the transfer's own destination, to
        the units Shopify disposed there."""
        sale_lines = self._sale_lines(order_binding)
        store = self.store_id
        warehouses = {location.shopify_gid: location.warehouse_id.lot_stock_id
                      for location in self.env['eh.shopify.location'].sudo().search(
                          [('store_id', '=', store.id), ('warehouse_id', '!=', False)])}
        unsellable = store.return_unsellable_location_id
        plan = {}

        def entry(sale_line):
            return plan.setdefault(sale_line, {'requested': 0, 'restocked': 0, 'targets': {}})

        for node in (record.get('returnLineItems') or {}).get('nodes') or []:
            sale_line = sale_lines.get(((node.get('fulfillmentLineItem') or {}).get('lineItem') or {}).get('id'))
            if sale_line:
                entry(sale_line)['requested'] += int(node.get('quantity') or 0)
        for reverse_order in (record.get('reverseFulfillmentOrders') or {}).get('nodes') or []:
            for node in (reverse_order.get('lineItems') or {}).get('nodes') or []:
                sale_line = sale_lines.get(((node.get('fulfillmentLineItem') or {}).get('lineItem') or {}).get('id'))
                if not sale_line:
                    continue
                for item in node.get('dispositions') or []:
                    units = int(item.get('quantity') or 0)
                    kind = item.get('type')
                    if units <= 0:
                        continue
                    if kind == 'RESTOCKED':
                        target = warehouses.get((item.get('location') or {}).get('id')) or False
                        entry(sale_line)['restocked'] += units
                    elif kind in UNSELLABLE and unsellable:
                        target = unsellable
                    else:
                        continue
                    targets = entry(sale_line)['targets']
                    targets[target] = targets.get(target, 0) + units
        return plan

    def _components(self, sale_line):
        """Component quantities in one unit of a kit sale line, or None for a
        normal product. Kits exist only with the manufacturing app."""
        if 'mrp.bom' not in self.env:
            return None
        product = sale_line.product_id
        found = self.env['mrp.bom'].sudo()._bom_find(product, company_id=sale_line.company_id.id, bom_type='phantom')
        bom = found.get(product) if hasattr(found, 'get') else found
        if not bom:
            return None
        unit = sale_line[compat.sale_line_uom_field()]
        factor = unit._compute_quantity(1.0, bom.product_uom_id) / (bom.product_qty or 1.0)
        _boms, lines = bom.explode(product, factor)
        components = {}
        for bom_line, data in lines:
            quantity = bom_line.product_uom_id._compute_quantity(data['qty'], bom_line.product_id.uom_id)
            if quantity > EPSILON:
                components[bom_line.product_id] = components.get(bom_line.product_id, 0.0) + quantity
        return components or None

    def _return_moves(self, sale_line):
        return self.picking_ids.move_ids.filtered(lambda move: move.origin_returned_move_id.sale_line_id == sale_line)

    def _units(self, sale_line, moves, done):
        """Sale line units carried by ``moves``: done quantities or planned ones."""
        def quantity(move):
            return _product_qty(move, compat.move_done_qty(move) if done else move.product_uom_qty)
        components = self._components(sale_line)
        if not components:
            return sum(quantity(move) for move in moves)
        totals = {}
        for move in moves:
            totals[move.product_id] = totals.get(move.product_id, 0.0) + quantity(move)
        return min(totals.get(product, 0.0) / per_unit for product, per_unit in components.items())

    def _needs(self, sale_line, units):
        components = self._components(sale_line)
        if not components:
            return {sale_line.product_id: units}
        return {product: units * per_unit for product, per_unit in components.items()}

    @staticmethod
    def _returnable(move):
        returned = sum(_product_qty(back, compat.move_done_qty(back) if back.state == 'done' else back.product_uom_qty)
                       for back in move.returned_move_ids.filtered(lambda back: back.state != 'cancel'))
        return max(0.0, _product_qty(move, compat.move_done_qty(move)) - returned)

    # ------------------------------------------------------------------ Odoo side
    def _prepare_pickings(self, plan, order_binding, reopened=False):
        """Return transfers for requested units not received, waiting or, unless
        the return was reopened, cancelled already."""
        sale_order = order_binding.sale_order_id
        deliveries = sale_order.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and p.state == 'done').move_ids.filtered(
            lambda m: m.state == 'done' and not m.origin_returned_move_id)
        by_picking, short = {}, False
        for sale_line, figures in plan.items():
            moves = self._return_moves(sale_line)
            covered = self._units(sale_line, moves.filtered(lambda m: m.state == 'done'), True)
            covered += self._units(sale_line, moves.filtered(lambda m: m.state not in ('done', 'cancel')), False)
            if not reopened:
                covered += self._units(sale_line, moves.filtered(lambda m: m.state == 'cancel'), False)
            missing = figures['requested'] - covered
            if missing <= EPSILON:
                continue
            for product, need in self._needs(sale_line, missing).items():
                for move in deliveries.filtered(lambda m, product=product: m.sale_line_id == sale_line
                                                and m.product_id == product).sorted('id'):
                    take = min(need, self._returnable(move))
                    if take > EPSILON:
                        # Quantities are in the product unit; the return transfer converts them to its move unit.
                        quantities = by_picking.setdefault(move.picking_id, {})
                        quantities[move.id] = quantities.get(move.id, 0.0) + take
                        need -= take
                    if need <= EPSILON:
                        break
                short = short or need > EPSILON
        if not by_picking:
            if short:
                raise errors.MappingError(_('Return %(name)s asks for goods that Odoo has not delivered for %(order)s.')
                                          % {'name': self.name, 'order': sale_order.name})
            return self.env['stock.picking']
        created = self.env['stock.picking']
        for picking, quantities in by_picking.items():
            created |= compat.create_return_picking(picking, quantities)
        self.write({'picking_ids': [(4, picking.id) for picking in created]})
        return created

    def _receive_restocked(self, plan):
        """Receive the units Shopify disposed that Odoo has not received yet,
        each where Shopify put it. A unit for the transfer's own destination and
        a unit for a mapped warehouse whose stock is that same location count
        together, and units validated by hand elsewhere count for the transfer's
        own destination."""
        wanted = {}
        for sale_line, figures in plan.items():
            default = self._return_moves(sale_line).mapped('picking_id.location_dest_id')[:1]
            per_location = {}
            for target, units in figures['targets'].items():
                location = target or default
                if location:
                    per_location[location] = per_location.get(location, 0) + units
            wanted[sale_line] = (per_location, default)
        defaults = {default for _per_location, default in wanted.values() if default}
        destinations = []
        for per_location, _default in wanted.values():
            for location in per_location:
                if location not in destinations:
                    destinations.append(location)
        destinations.sort(key=lambda location: location not in defaults)
        for location in destinations:
            todo = {}
            for sale_line, (per_location, default) in wanted.items():
                units = per_location.get(location, 0)
                if not units:
                    continue
                moves = self._return_moves(sale_line)
                keys = set(per_location)
                done = moves.filtered(lambda m, location=location, default=default, keys=keys: m.state == 'done' and (
                    m.location_dest_id == location
                    or (location == default and m.location_dest_id not in keys)))
                delta = units - self._units(sale_line, done, True)
                if delta <= EPSILON:
                    continue
                waiting = moves.filtered(lambda m: m.state not in ('done', 'cancel'))
                for product, need in self._needs(sale_line, delta).items():
                    for move in waiting.filtered(lambda m, product=product: m.product_id == product).sorted('id'):
                        take = min(need, _product_qty(move, move.product_uom_qty))
                        if take > EPSILON:
                            quantities = todo.setdefault(move.picking_id, {})
                            quantities[move.id] = quantities.get(move.id, 0.0) + _move_qty(move, take)
                            need -= take
                        if need <= EPSILON:
                            break
            for picking, quantities in todo.items():
                moves = picking.move_ids.filtered(lambda m, ids=quantities: m.id in ids)
                elsewhere = moves.filtered(lambda m, location=location: m.location_dest_id != location)
                if elsewhere:
                    elsewhere.write({'location_dest_id': location.id})
                    elsewhere.move_line_ids.write({'location_dest_id': location.id})
                compat.validate_picking(picking, quantities=quantities, backorder=True)
            self._track_backorders()
            # The units left waiting go back to their transfer's own destination.
            for picking in self.picking_ids.filtered(_open):
                stray = picking.move_ids.filtered(lambda m, p=picking: m.location_dest_id != p.location_dest_id
                                                  and m.state not in ('done', 'cancel'))
                if stray:
                    stray.write({'location_dest_id': picking.location_dest_id.id})
                    stray.move_line_ids.write({'location_dest_id': picking.location_dest_id.id})

    def _track_backorders(self):
        for binding in self:
            backorders = binding.picking_ids.mapped('backorder_ids') - binding.picking_ids
            if backorders:
                binding.write({'picking_ids': [(4, picking.id) for picking in backorders]})

    def _refresh_state(self, status=None):
        for binding in self:
            if binding.state == 'error' and (status or binding.status) in STOPPED:
                continue
            pickings = binding.picking_ids
            if pickings.filtered(_open):
                reason = binding.reason
                if (status or binding.status) == 'CLOSED':
                    reason = _('Shopify closed this return. Validate the return transfer for the units put back in '
                               'stock.')
                binding.write({'state': 'waiting', 'reason': reason})
            elif pickings.filtered(lambda p: p.state == 'done'):
                binding.write({'state': 'received', 'reason': False})
            elif pickings:
                binding.write({'state': 'cancelled', 'reason': _('Nothing from this return was put back in stock.')})

    # ------------------------------------------------------------------ exchanges
    def _sync_exchange(self, record, order_binding):
        """One exchange order for the replacement goods, once the return is approved."""
        self.ensure_one()
        exchanges = (record.get('exchangeLineItems') or {}).get('nodes') or []
        if not exchanges:
            self.exchange_note = False
            return
        if record.get('status') not in ('OPEN', 'CLOSED'):
            self.exchange_note = _('%s items to send in exchange once the return is approved.') % sum(
                int(item.get('quantity') or 0) for item in exchanges)
            return
        if self.exchange_order_id:
            return
        store = self.store_id
        sale_order = order_binding.sale_order_id
        ship = store.exchange_order_mode == 'ship'
        key = 'presentmentMoney' if order_binding.currency_mode == 'presentment' else 'shopMoney'
        company_ids = compat.company_lookup_ids(store.company_id)
        Variant = self.env['eh.shopify.variant']
        lines, problems = [], []
        for exchange in exchanges:
            items = exchange.get('lineItems') or [{'variant': {'id': exchange.get('variantId')},
                                                   'quantity': exchange.get('quantity'), 'name': _('Exchange item')}]
            for item in items:
                product, reason = Variant._resolve_line(store, {'variant': item.get('variant') or {},
                                                                'sku': item.get('sku'), 'name': item.get('name')})
                quantity = int(item.get('quantity') or exchange.get('quantity') or 0)
                if not product:
                    problems.append(reason)
                    continue
                if quantity <= 0:
                    continue
                price, taxes = 0.0, self.env['account.tax']
                if not ship:
                    shopify_price = float(order_math.dec(
                        ((item.get('originalUnitPriceSet') or {}).get(key) or {}).get('amount')))
                    converted = store._odoo_price(product, shopify_price)
                    price = shopify_price if converted is None else converted
                    taxes = product.taxes_id.filtered(lambda tax: tax.company_id.id in company_ids)
                lines.append((item, compat.sale_line_vals(self.env, product, quantity, price, taxes,
                                                          name=item.get('name'))))
        if not lines:
            self.exchange_note = ' '.join(problems) or _('No exchange item matches an Odoo product.')
            return
        vals = {'partner_id': sale_order.partner_id.id, 'partner_invoice_id': sale_order.partner_invoice_id.id,
                'partner_shipping_id': sale_order.partner_shipping_id.id, 'company_id': sale_order.company_id.id,
                'pricelist_id': sale_order.pricelist_id.id, 'client_order_ref': sale_order.client_order_ref,
                'origin': _('Exchange for %(order)s, return %(return)s') % {'order': sale_order.name,
                                                                             'return': self.name},
                'order_line': [(0, 0, line_vals) for _item, line_vals in lines],
                'eh_shopify_order_id': order_binding.id, 'eh_shopify_exchange_return_id': self.id}
        if 'warehouse_id' in sale_order._fields and sale_order.warehouse_id:
            vals['warehouse_id'] = sale_order.warehouse_id.id
        for field in ('team_id', 'user_id', 'fiscal_position_id'):
            if sale_order[field]:
                vals[field] = sale_order[field].id
        exchange_order = self.env['sale.order'].sudo().with_company(store.company_id).create(vals)
        OrderLine = self.env['eh.shopify.order.line'].sudo()
        for (item, _line_vals), sale_line in zip(lines, exchange_order.order_line.sorted('id')):
            if item.get('id') and not OrderLine.search_count([('order_binding_id', '=', order_binding.id),
                                                              ('shopify_gid', '=', item['id'])]):
                OrderLine.create({'order_binding_id': order_binding.id, 'kind': 'product', 'shopify_gid': item['id'],
                                  'sale_line_id': sale_line.id, 'variant_gid': (item.get('variant') or {}).get('id'),
                                  'sku': item.get('sku') or False, 'quantity': int(item.get('quantity') or 0)})
        if ship:
            compat.so_confirm(exchange_order, keep_date_order=False)
        self.write({'exchange_order_id': exchange_order.id, 'exchange_note': ' '.join(problems) or False})
        sale_order.message_post(body=_('Exchange order %(exchange)s was created for return %(return)s.') % {
            'exchange': exchange_order.name, 'return': self.name})

    def _cancel_exchange(self):
        exchange = self.exchange_order_id
        if not exchange or exchange.state == 'cancel':
            return
        if exchange.picking_ids.filtered(lambda p: p.state == 'done'):
            self.exchange_note = _('Exchange order %s was already shipped, so it was kept.') % exchange.name
            return
        compat.so_cancel(exchange)

    def _stop(self):
        self._cancel_exchange()
        done = self.picking_ids.filtered(lambda p: p.state == 'done')
        self.picking_ids.filtered(_open).action_cancel()
        if done:
            self.write({'state': 'error', 'reason': _('Shopify %(status)s this return after Odoo received %(pickings)s. '
                                                     'Reverse the transfer if the goods leave again.') % {
                'status': (self.status or '').lower(), 'pickings': ', '.join(done.mapped('name'))}})
            return {'error': self.shopify_gid}
        self.write({'state': 'cancelled', 'reason': False})
        return {'cancelled': self.shopify_gid}


class ShopifyOrderReturns(models.Model):
    _inherit = 'eh.shopify.order'

    def _apply_to_odoo(self, order):
        result = super()._apply_to_odoo(order)
        if (self.sale_order_id and (order.get('returnStatus') or 'NO_RETURN') != 'NO_RETURN'
                and not self.env.context.get(HISTORICAL)):
            self._queue_return_syncs()
        return result

    def _queue_return_syncs(self):
        """Sync the order's returns that Odoo does not have or has an older status for."""
        store = self.store_id
        try:
            data = store._client().execute(store._doc_text(documents.ORDER_RETURN_IDS), {'id': self.shopify_gid}).data
        except errors.ShopifyError:
            return
        Return = self.env['eh.shopify.return'].sudo()
        for node in (((data or {}).get('order') or {}).get('returns') or {}).get('nodes') or []:
            binding = Return.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1)
            if not binding or binding.status != node.get('status'):
                Return._enqueue_return_sync(store, node['id'], self.shopify_gid)


class StockPickingReturns(models.Model):
    _inherit = 'stock.picking'

    def _action_done(self):
        result = super()._action_done()
        bindings = self.env['eh.shopify.return'].sudo().search([('picking_ids', 'in', self.ids)])
        if bindings:
            bindings._track_backorders()
            bindings._refresh_state()
        return result


class ShopifyStoreExchanges(models.Model):
    _inherit = 'eh.shopify.store'

    return_unsellable_location_id = fields.Many2one(
        'stock.location', 'Receive units not restocked into', domain=[('usage', '=', 'internal')],
        help='Units Shopify marks as not restocked or needing processing are received here, for example a quarantine '
             'or damaged goods location. Empty: they are not received.')
    exchange_order_mode = fields.Selection([
        ('ship', 'Confirm at no charge so the warehouse ships'),
        ('quotation', 'Prepare a quotation at Shopify prices'),
    ], string='Exchange orders', default='ship', required=True,
        help='The price difference of an exchange is paid or refunded in Shopify on the original order, so by default '
             'the replacement ships at no charge in Odoo.')


class SaleOrderExchanges(models.Model):
    _inherit = 'sale.order'

    eh_shopify_exchange_return_id = fields.Many2one('eh.shopify.return', 'Shopify exchange for', readonly=True,
                                                    copy=False, index=True)

    def _eh_shopify_cancel_needs_dialog(self):
        # An exchange order is Odoo's own, so cancelling it never cancels the Shopify order.
        if self.eh_shopify_exchange_return_id:
            return False
        return super()._eh_shopify_cancel_needs_dialog()
