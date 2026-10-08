# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Turn a Shopify order into an Odoo sales order that matches it to the cent.

Shopify's amounts win. Prices, discounts and taxes come from the order; the
Odoo total is then compared with Shopify's. Rounding noise inside the tolerance
is absorbed by one visible rounding line; anything larger leaves the order as a
quotation with a written explanation, and nothing is invoiced.
"""
import datetime
from decimal import Decimal

from odoo import _, models

from .. import compat
from ..lib import errors, order_math
from .eh_shopify_order_import import HISTORICAL

EXTRA_PRODUCTS = {
    'shipping': ('shipping_product_id', 'Shipping (Shopify)'),
    'tip': ('tip_product_id', 'Tip (Shopify)'),
    'fee': ('fee_product_id', 'Additional fee (Shopify)'),
    'duty': ('duty_product_id', 'Duties (Shopify)'),
    'gift_card': ('gift_card_product_id', 'Gift card (Shopify)'),
    'rounding': ('rounding_product_id', 'Rounding (Shopify)'),
    'refund_adjustment': ('refund_adjustment_product_id', 'Refund adjustment (Shopify)'),
}
INVOICE_WHEN = {'PAID', 'PARTIALLY_REFUNDED', 'REFUNDED'}


def _dt(value):
    if not value:
        return False
    try:
        return datetime.datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return False


def _dec_to_float(value):
    return float(value or 0)


class ShopifyOrderSale(models.Model):
    _inherit = 'eh.shopify.order'

    # ------------------------------------------------------------------ helpers
    def _extra_product(self, kind):
        store = self.store_id
        field, name = EXTRA_PRODUCTS[kind]
        product = store[field]
        if product:
            return product
        template = self.env['product.template'].sudo().create(
            compat.product_create_vals(self.env, name, 'service', company=store.company_id))
        store.sudo().write({field: template.product_variant_id.id})
        return template.product_variant_id

    def _taxes_for(self, tax_lines, include):
        store = self.store_id
        taxes = self.env['account.tax']
        for tax in tax_lines or []:
            if not tax['amount']:
                continue
            rate = _dec_to_float(tax['rate_percentage']) or _dec_to_float(tax['rate']) * 100
            rate = round(rate, 4)
            found = compat.find_sale_tax(self.env, store.company_id, rate, include, tax.get('title'))
            if found and found in taxes:
                # Another line of this order already uses it, so this rate needs a tax of its own.
                found = compat.find_sale_tax(self.env, store.company_id, rate, include, tax.get('title'),
                                             exclude=taxes)
            if not found:
                if not store.auto_create_taxes:
                    raise errors.MappingError(_(
                        'No active %(rate)s%% sales tax with prices %(mode)s exists in %(company)s. Create it, or turn '
                        'on "Create missing taxes" on the store.') % {
                        'rate': '%g' % rate, 'mode': _('including tax') if include else _('excluding tax'),
                        'company': store.company_id.name})
                found = compat.create_sale_tax(self.env, store.company_id,
                                               '%s %s%%' % (tax.get('title') or _('Tax'), '%g' % rate), rate, include)
                if not found:
                    raise errors.MappingError(_('Set the country of %s so missing taxes can be created.')
                                              % store.company_id.name)
            taxes |= found
        return taxes

    def _create_product_for(self, raw, item):
        store = self.store_id
        variant = raw.get('variant') or {}
        vals = compat.product_create_vals(self.env, raw.get('name') or item['name'], 'storable',
                                          list_price=_dec_to_float(item['unit_price']))
        vals['default_code'] = item['sku'] or False
        barcode = variant.get('barcode')
        if barcode and not self.env['product.product'].with_context(active_test=False).search_count(
                [('barcode', '=', barcode)]):
            vals['barcode'] = barcode
        product = self.env['product.template'].sudo().create(vals).product_variant_id
        self.env['eh.shopify.variant'].sudo().create({
            'store_id': store.id, 'shopify_gid': variant['id'], 'product_gid': (raw.get('product') or {}).get('id'),
            'inventory_item_gid': (variant.get('inventoryItem') or {}).get('id'), 'product_id': product.id,
            'title': raw.get('name'), 'sku': item['sku'] or False, 'barcode': barcode or False,
            'match_method': 'created'})
        return product

    def _line_discount(self, gross, discount, currency):
        digits = self.env['decimal.precision'].precision_get('Discount')
        return order_math.odoo_line_discount(gross, discount, discount_digits=digits,
                                             currency_digits=currency.decimal_places)

    # ------------------------------------------------------------------ lines
    def _build_line_commands(self, order, amounts, currency):
        env = self.env
        store = self.store_id
        include = amounts['taxes_included']
        raw_lines = {line['id']: line for line in (order.get('lineItems') or {}).get('nodes') or []}
        rounding_lost = order_math.ZERO
        commands = []
        meta = []
        problems = []
        sequence = 10

        def add(product, quantity, price, taxes, name, kind, gid=None, sku=None, variant_gid=None, discount=0.0,
                is_delivery=False):
            nonlocal sequence
            commands.append(Command_create(compat.sale_line_vals(
                env, product, quantity, price, taxes, discount=discount, name=name, sequence=sequence,
                is_delivery=is_delivery)))
            meta.append({'sequence': sequence, 'kind': kind, 'shopify_gid': gid, 'sku': sku,
                         'variant_gid': variant_gid, 'quantity': quantity})
            sequence += 1

        for item in amounts['lines']:
            raw = raw_lines.get(item['id']) or {}
            if item['kind'] == 'gift_card':
                product = self._extra_product('gift_card')
            else:
                product, reason = env['eh.shopify.variant']._resolve_line(store, raw)
                if not product:
                    if store.unmatched_product == 'create' and (raw.get('variant') or {}).get('id'):
                        product = self._create_product_for(raw, item)
                    else:
                        problems.append(reason)
                        continue
            percent, lost = self._line_discount(item['gross'], item['discount'], currency)
            rounding_lost += lost
            add(product, item['quantity'], _dec_to_float(item['unit_price']), self._taxes_for(item['taxes'], include),
                raw.get('name') or item['name'], item['kind'], gid=item['id'], sku=item['sku'],
                variant_gid=item['variant_id'], discount=float(percent))
            if not amounts['duties_included']:
                for duty in item['duties']:
                    if duty['amount']:
                        add(self._extra_product('duty'), 1, _dec_to_float(duty['amount']),
                            self._taxes_for(duty['taxes'], include),
                            _('Duties for %s') % (raw.get('name') or item['name']), 'duty', gid=duty['id'])
        if problems:
            raise errors.MappingError(' '.join(problems))
        for ship in amounts['shipping']:
            carrier_map = env['eh.shopify.carrier']._for(store, ship)
            product = carrier_map.carrier_id.product_id or self._extra_product('shipping')
            percent, lost = self._line_discount(ship['gross'], ship['discount'], currency)
            rounding_lost += lost
            add(product, 1, _dec_to_float(ship['gross']), self._taxes_for(ship['taxes'], include),
                ship['title'] or _('Shipping'), 'shipping', gid=ship['id'], discount=float(percent), is_delivery=True)
        tips = amounts['tip_lines'] or ([{'id': None, 'amount': amounts['tip']}]
                                        if amounts['tip'] and amounts['tip_in_total'] else [])
        for tip in tips:
            add(self._extra_product('tip'), 1, _dec_to_float(tip['amount']), env['account.tax'], _('Tip'), 'tip',
                gid=tip.get('id'))
        for fee in amounts['fee_lines']:
            add(self._extra_product('fee'), 1, _dec_to_float(fee['net']), self._taxes_for(fee['taxes'], include),
                fee['name'] or _('Fee'), 'fee', gid=fee['id'])
        if not amounts['fee_lines'] and amounts['fees'] and amounts['fees_in_total']:
            add(self._extra_product('fee'), 1, _dec_to_float(amounts['fees']), env['account.tax'], _('Fees'), 'fee')
        return commands, meta, rounding_lost

    # ------------------------------------------------------------------ sales order
    def _warehouse_for_order(self, order):
        """Warehouse that delivers the order: the one mapped to the Shopify
        location of its fulfillment orders, so stock is reserved where Shopify
        committed it. Orders Shopify splits over several warehouses, or over a
        location with no warehouse, use the store default and say so."""
        store = self.store_id
        nodes = [node for node in (order.get('fulfillmentOrders') or {}).get('nodes') or []
                 if node.get('status') != 'CANCELLED']
        assigned = {}
        for node in nodes:
            location = (node.get('assignedLocation') or {})
            gid = (location.get('location') or {}).get('id')
            if gid:
                assigned[gid] = location.get('name') or gid
        if not assigned:
            return store.warehouse_id, None
        bindings = self.env['eh.shopify.location'].sudo().search([('store_id', '=', store.id),
                                                                  ('shopify_gid', 'in', list(assigned))])
        warehouses = bindings.mapped('warehouse_id')
        if len(bindings) == len(assigned) and all(bindings.mapped('warehouse_id')) and len(warehouses) == 1 \
                and warehouses.company_id in (store.company_id, self.env['res.company']):
            return warehouses, None
        if not store.warehouse_id:
            return store.warehouse_id, None
        return store.warehouse_id, _('Shopify fulfils this order from %(locations)s. Odoo delivers it from %(warehouse)s; '
                                     'map each Shopify location to its warehouse to deliver from the right place.') % {
            'locations': ', '.join(sorted(assigned.values())), 'warehouse': store.warehouse_id.name}

    def _create_sale_order(self, order, amounts, partners):
        store = self.store_id
        company = store.company_id
        currency = compat.ensure_currency(self.env, amounts['currency'])
        if not currency:
            raise errors.MappingError(_('Odoo has no currency %s.') % amounts['currency'])
        pricelist = compat.pricelist_for_currency(self.env, currency, company)
        commands, meta, rounding_lost = self._build_line_commands(order, amounts, currency)
        SaleOrder = self.env['sale.order'].with_company(company)
        vals = {
            'partner_id': partners['partner'].id,
            'partner_invoice_id': partners['invoice'].id,
            'partner_shipping_id': partners['shipping'].id,
            'company_id': company.id,
            'pricelist_id': pricelist.id,
            'client_order_ref': order.get('poNumber') or order.get('name'),
            'origin': _('Shopify %s') % order.get('name'),
            'order_line': commands,
            'eh_shopify_order_id': self.id,
        }
        ordered_at = _dt(order.get('processedAt') or order.get('createdAt'))
        if ordered_at:
            vals['date_order'] = ordered_at
        if partners['fiscal_position']:
            vals['fiscal_position_id'] = partners['fiscal_position'].id
        warehouse, routing_note = self._warehouse_for_order(order)
        if warehouse and 'warehouse_id' in SaleOrder._fields:
            vals['warehouse_id'] = warehouse.id
        if store.order_team_id:
            vals['team_id'] = store.order_team_id.id
        if store.order_user_id:
            vals['user_id'] = store.order_user_id.id
        if order.get('note'):
            vals['note'] = order['note']
        term, term_note = self._payment_term(order)
        if term:
            vals['payment_term_id'] = term.id
        sale_order = SaleOrder.create(vals)
        if term_note:
            sale_order.message_post(body=term_note)
        if routing_note:
            sale_order.message_post(body=routing_note)
        self._check_parity(sale_order, amounts, currency, rounding_lost)
        lines_by_sequence = {line.sequence: line for line in sale_order.order_line}
        OrderLine = self.env['eh.shopify.order.line']
        for row in meta:
            sale_line = lines_by_sequence.get(row['sequence'])
            OrderLine.create({'order_binding_id': self.id, 'kind': row['kind'], 'shopify_gid': row['shopify_gid'],
                              'sku': row['sku'], 'variant_gid': row['variant_gid'], 'quantity': row['quantity'],
                              'sale_line_id': sale_line.id if sale_line else False})
        return sale_order

    def _payment_term(self, order):
        """Odoo payment term for B2B payment terms: due on receipt, or net a number
        of days. Terms due on a fixed date or on fulfilment are noted for a person."""
        terms = order.get('paymentTerms') or {}
        kind = (terms.get('paymentTermsType') or '').upper()
        Term = self.env['account.payment.term']
        if kind == 'RECEIPT':
            days = 0
        elif kind == 'NET' and terms.get('dueInDays') is not None:
            days = int(terms['dueInDays'])
        elif kind:
            return Term, _('Shopify payment terms: %s. Set the matching payment terms in Odoo.') % (
                terms.get('paymentTermsName') or kind.lower())
        else:
            return Term, None
        store = self.store_id
        term = compat.payment_term_for_days(self.env, store.company_id, days, create=store.auto_create_payment_terms)
        if not term:
            return Term, _('Shopify payment terms: %s. No Odoo payment term matches; create one or turn on "Create '
                           'missing payment terms" on the store.') % (terms.get('paymentTermsName') or kind.lower())
        return term, None

    def _check_parity(self, sale_order, amounts, currency, rounding_lost=None):
        """``rounding_lost`` is the money the rounded discount percentages of
        this order lose, which the connector itself introduced, so it widens the
        difference that still counts as rounding."""
        target = amounts['target_total']
        actual = Decimal(str(sale_order.amount_total))
        difference = target - actual
        allowed = amounts['tolerance'] + abs(rounding_lost or order_math.ZERO)
        vals = {'sale_order_id': sale_order.id, 'currency_code': currency.name, 'target_total': float(target)}
        if currency.is_zero(float(difference)):
            vals.update(parity_state='ok')
        elif abs(difference) <= allowed:
            sale_order.write({'order_line': [Command_create(compat.sale_line_vals(
                self.env, self._extra_product('rounding'), 1, float(difference), self.env['account.tax'],
                name=_('Rounding to match Shopify'), sequence=9999))]})
            vals.update(parity_state='adjusted')
        else:
            vals.update(parity_state='mismatch', state='error', reason=_(
                'The Odoo total %(odoo)s differs from the Shopify total %(shopify)s by %(diff)s, more than the '
                'rounding tolerance. Check the taxes and products on %(order)s, then retry the import.') % {
                'odoo': sale_order.amount_total, 'shopify': float(target), 'diff': float(difference),
                'order': sale_order.name})
        vals.update(odoo_total=sale_order.amount_total, residual=float(target - Decimal(str(sale_order.amount_total))))
        self.write(vals)

    # ------------------------------------------------------------------ order edits
    def _apply_edits(self, order, amounts, sale_order):
        """Follow items added, removed or changed in Shopify after the import.

        Each Shopify line link keeps the quantity it was imported with, on the
        as sold basis, so an edit is the difference to the new sold quantity and
        units already removed by refunds are never counted twice. The sales
        order follows only while nothing is shipped or invoiced; after that a
        to-do lists the changes and no Odoo document is touched."""
        self.ensure_one()
        raw = {line['id']: line for line in (order.get('lineItems') or {}).get('nodes') or []}
        known = {line.shopify_gid: line for line in self.line_ids
                 if line.kind in ('product', 'gift_card') and line.shopify_gid
                 and (not line.sale_line_id or line.sale_line_id.order_id == sale_order)}
        changes, added, notes = [], [], []
        for item in amounts['lines']:
            name = (raw.get(item['id']) or {}).get('name') or item['name']
            line = known.get(item['id'])
            if line:
                delta = int(item['quantity']) - int(line.quantity or 0)
                if delta:
                    changes.append((line, delta))
                    notes.append(_('%(item)s from %(before)s to %(after)s') % {
                        'item': name, 'before': int(line.quantity or 0), 'after': int(item['quantity'])})
            elif int(item['quantity']) > 0:
                added.append(item)
                notes.append(_('%(item)s added (%(quantity)s)') % {'item': name, 'quantity': int(item['quantity'])})
        present = {item['id'] for item in amounts['lines']}
        for gid, line in known.items():
            if gid not in present and line.quantity:
                changes.append((line, -int(line.quantity)))
                notes.append(_('%s removed') % (line.sale_line_id.name or line.sku or gid))
        if not notes:
            return None
        shipped = sale_order.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state == 'done')
        invoiced = sale_order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state == 'posted')
        if shipped or invoiced:
            summary = _('Shopify order edited')
            reason = _('Changed in Shopify after it was shipped or invoiced in Odoo: %s. The Odoo documents were not '
                       'changed; adjust them by hand.') % '; '.join(notes)
            self.reason = reason
            if not sale_order.activity_ids.filtered(lambda activity: activity.summary == summary):
                sale_order.activity_schedule('mail.mail_activity_data_todo', summary=summary, note=reason,
                                             user_id=(sale_order.user_id or self.env.user).id)
            return 'manual'
        relock = compat.unlock_orders(sale_order)
        rounding_lost = order_math.ZERO
        for line, delta in changes:
            if line.sale_line_id:
                compat.set_sale_line_quantity(line.sale_line_id, max(0.0, line.sale_line_id.product_uom_qty + delta))
            line.quantity = (line.quantity or 0) + delta
        if added:
            currency = compat.ensure_currency(self.env, amounts['currency'])
            subset = dict(amounts, lines=added, shipping=[], tip_lines=[], tip=0, fee_lines=[], fees=0)
            commands, meta, rounding_lost = self._build_line_commands(order, subset, currency)
            before = sale_order.order_line
            start = max(before.mapped('sequence') or [0]) + 1
            for offset, (command, row) in enumerate(zip(commands, meta)):
                command[2]['sequence'] = start + offset
                row['sequence'] = start + offset
            sale_order.write({'order_line': commands})
            by_sequence = {line.sequence: line for line in sale_order.order_line - before}
            OrderLine = self.env['eh.shopify.order.line']
            for row in meta:
                sale_line = by_sequence.get(row['sequence'])
                OrderLine.create({'order_binding_id': self.id, 'kind': row['kind'], 'shopify_gid': row['shopify_gid'],
                                  'sku': row['sku'], 'variant_gid': row['variant_gid'], 'quantity': row['quantity'],
                                  'sale_line_id': sale_line.id if sale_line else False})
        compat.lock_orders(relock)
        self._check_parity(sale_order, amounts, compat.ensure_currency(self.env, amounts['currency']), rounding_lost)
        sale_order.message_post(body=_('Updated from a change in Shopify: %s.') % '; '.join(notes))
        return 'applied'

    # ------------------------------------------------------------------ invoices and payments
    @staticmethod
    def _invoice_date(order):
        stamp = _dt(order.get('processedAt') or order.get('createdAt'))
        return stamp.date() if stamp else None

    def _sync_invoice_and_payments(self, order, sale_order):
        store = self.store_id
        if sale_order.state not in ('sale', 'done'):
            return
        invoices = sale_order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state != 'cancel')
        if store.invoice_policy == 'paid' and (order.get('displayFinancialStatus') or '').upper() in INVOICE_WHEN \
                and not invoices and sale_order.invoice_status == 'to invoice':
            invoices = compat.create_invoices(sale_order, invoice_date=self._invoice_date(order))
        # The customer paid in Shopify whatever the invoice policy, so the payment is recorded against any
        # posted invoice, including one the merchant posted by hand.
        self._record_payments(invoices.filtered(lambda m: m.state == 'posted'))

    def _enqueue_invoice(self):
        self.ensure_one()
        return self.env['eh.shopify.job'].sudo()._enqueue_after_running(
            self.store_id, 'order.invoice', 'order.invoice:%s' % self.id,
            name=_('Invoice and payments of %s') % (self.name or self.shopify_gid),
            payload={'order_binding_id': self.id}, resource_key=self.shopify_gid, priority=5, record=self)

    def _job_order_invoice(self, job):
        """Invoice a delivered order and record what the customer paid in Shopify."""
        binding = self.browse((job.payload or {}).get('order_binding_id')).exists()
        sale_order = binding.sale_order_id
        if not sale_order or sale_order.state not in ('sale', 'done'):
            return {'skipped': 'the order is not confirmed in Odoo'}
        invoices = sale_order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state != 'cancel')
        if not invoices and binding.store_id.invoice_policy == 'delivered' \
                and sale_order.invoice_status == 'to invoice':
            invoices = compat.create_invoices(
                sale_order, invoice_date=sale_order.date_order.date() if sale_order.date_order else None)
        binding._record_payments(invoices.filtered(lambda m: m.state == 'posted'))
        return {'invoices': invoices.mapped('name')}

    def _record_payments(self, invoices):
        # Cash tendered at a till comes with a CHANGE transaction; only what was kept is a payment.
        change = {}
        for given_back in self.transaction_ids.filtered(lambda t: t.kind == 'CHANGE' and t.status == 'SUCCESS'):
            change[given_back.gateway] = change.get(given_back.gateway, 0.0) + abs(given_back.amount)
        for transaction in self.transaction_ids.filtered(
                lambda t: t.state in ('pending', 'error') and t.kind in ('SALE', 'CAPTURE') and t.status == 'SUCCESS'):
            amount = transaction.amount
            if change.get(transaction.gateway):
                kept_back = min(change[transaction.gateway], amount)
                change[transaction.gateway] -= kept_back
                amount -= kept_back
                if amount <= 0:
                    transaction.write({'state': 'ignored', 'note': _('The whole amount was given back as change.')})
                    continue
            gateway = transaction.gateway_id
            if gateway and not gateway.register_payments:
                transaction.write({'state': 'ignored', 'note': _('Payments of this method are not recorded.')})
                continue
            if not gateway.journal_id:
                transaction.write({'state': 'error', 'note': _(
                    'Choose the journal for payment method %s in Shopify, Configuration, Payment Methods. The '
                    'payment is recorded on the next update of this order.') % (gateway.name or transaction.gateway)})
                continue
            open_invoices = invoices.filtered(lambda m: m.payment_state not in ('paid', 'in_payment', 'reversed'))
            if not open_invoices:
                continue
            payments = compat.register_invoice_payment(
                open_invoices, gateway.journal_id, amount=amount,
                date=transaction.processed_at.date() if transaction.processed_at else None,
                memo='%s %s' % (self.name, transaction.shopify_gid.rsplit('/', 1)[-1]),
                method_line=gateway.payment_method_line_id or None)
            transaction.write({'state': 'recorded', 'note': False, 'payment_id': payments[:1].id})

    # ------------------------------------------------------------------ cancellation
    def _apply_cancellation(self, sale_order):
        if not sale_order or sale_order.state == 'cancel':
            self.write({'state': 'cancelled', 'reason': False})
            return {'cancelled': self.shopify_gid}
        shipped = sale_order.picking_ids.filtered(lambda p: p.state == 'done')
        invoiced = sale_order.invoice_ids.filtered(lambda m: m.state == 'posted')
        if shipped or invoiced:
            reason = _('Cancelled in Shopify after it was shipped or invoiced in Odoo. Create the return or the '
                       'credit note in Odoo.')
            self.write({'state': 'error', 'reason': reason})
            sale_order.activity_schedule('mail.mail_activity_data_todo', summary=_('Shopify order cancelled'),
                                         note=reason, user_id=(sale_order.user_id or self.env.user).id)
            return {'needs_attention': sale_order.name}
        compat.so_cancel(sale_order)
        self.write({'state': 'cancelled', 'reason': False})
        return {'cancelled': sale_order.name}

    # ------------------------------------------------------------------ entry point
    def _apply_to_odoo(self, order):
        self.ensure_one()
        store = self.store_id
        amounts = order_math.OrderAmounts(order, currency=self.currency_mode or 'shop',
                                          refunded_quantities=order.get('_refundedQuantities')).compute()
        sale_order = self.sale_order_id
        if order.get('cancelledAt'):
            return self._apply_cancellation(sale_order)
        if sale_order and sale_order.state != 'cancel':
            self._apply_edits(order, amounts, sale_order)
        if not sale_order:
            partners = self.env['eh.shopify.customer']._resolve(store, order)
            if partners.get('binding'):
                self.customer_binding_id = partners['binding']
            sale_order = self._create_sale_order(order, amounts, partners)
            historical = bool(self.env.context.get(HISTORICAL))
            if self.parity_state != 'mismatch' and (store.order_confirm or historical):
                compat.so_confirm(sale_order)
            if historical:
                sale_order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')).action_cancel()
                sale_order.message_post(body=_('Imported from the Shopify order history. No stock was moved.'))
        if self.parity_state != 'mismatch':
            self._sync_invoice_and_payments(order, sale_order)
        if self.env.context.get(HISTORICAL) and sale_order.state == 'sale':
            compat.lock_orders(sale_order.filtered(lambda so: not so.locked) if 'locked' in sale_order._fields
                               else sale_order)
        return {'sale_order': sale_order.name, 'parity': self.parity_state, 'total': float(amounts['target_total'])}


def Command_create(vals):
    return (0, 0, vals)
