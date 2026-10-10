# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify refunds become Odoo credit notes.

Orders are imported as sold, so each refund becomes credit notes against the
posted invoices of its order: product lines against the invoice that billed
them, net of earlier credit notes, and shipping, duties and adjustments against
the latest invoice. The credit notes must add up to Shopify's refunded total;
a rounding difference inside the order tolerance becomes a visible line, a
larger one leaves the credit notes in draft with the figures until someone posts
them. Refund payments become outbound payments on the payment method's journal,
also when Shopify confirms them later. Only units refunded before shipping, or
returned, are tied to the sales order line, so money refunded on goods the
customer keeps never makes the order invoiceable again. One refund is recorded
once, whatever sends it: the webhook, the order update, the poller, or a push
from an Odoo credit note that stopped before recording it.
"""
import datetime

from odoo import Command, _, api, fields, models

from .. import compat
from ..lib import documents, errors, order_math


def _parse_dt(value):
    if not value:
        return False
    try:
        return datetime.datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except (TypeError, ValueError):
        return False


class ShopifyRefund(models.Model):
    _name = 'eh.shopify.refund'
    _description = 'Shopify Refund'
    _order = 'id desc'
    _rec_name = 'name'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    order_binding_id = fields.Many2one('eh.shopify.order', 'Shopify order', ondelete='cascade', index=True)
    sale_order_id = fields.Many2one('sale.order', 'Sales order', related='order_binding_id.sale_order_id')
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    name = fields.Char(readonly=True)
    processed_at = fields.Datetime('Processed', readonly=True)
    note = fields.Char(readonly=True)
    currency_code = fields.Char(readonly=True)
    total_refunded = fields.Float('Refunded in Shopify', digits=(16, 4), readonly=True)
    credit_note_ids = fields.Many2many('account.move', 'eh_shopify_refund_move_rel', 'refund_id', 'move_id',
                                      string='Credit notes', readonly=True)
    state = fields.Selection([
        ('pending_invoice', 'Waiting for the invoice'),
        ('draft', 'Needs review'),
        ('done', 'Recorded'),
        ('error', 'Needs attention'),
    ], default='pending_invoice', required=True, readonly=True, index=True)
    reason = fields.Char(readonly=True)
    restock_note = fields.Char('Stock', readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_refund_gid_uniq "
                            "ON eh_shopify_refund (store_id, shopify_gid)")

    # ------------------------------------------------------------------ triggers
    @api.model
    def _enqueue_refund_import(self, store, gid, order_gid, priority=5):
        return self.env['eh.shopify.job'].sudo()._enqueue_after_running(
            store, 'refund.import', 'refund:%s' % gid, name=_('Import refund %s') % gid.rsplit('/', 1)[-1],
            payload={'gid': gid, 'order_gid': order_gid}, resource_key=order_gid, priority=priority)

    # ------------------------------------------------------------------ reading
    def _read_refund(self, store, gid):
        client = store._client()
        data = client.execute(store._doc_text(documents.REFUND), {'id': gid}).data or {}
        refund = data.get('node')
        if not refund:
            return None
        nested = documents.REFUND_LINE_ITEMS
        refund['refundLineItems'] = {'nodes': client.complete_connection(
            refund, 'refundLineItems', store._doc_text(nested), gid, ('node', 'refundLineItems'),
            page_size=nested.page_size)}
        for key in ('refundShippingLines', 'orderAdjustments', 'transactions'):
            page = refund.get(key) or {}
            if (page.get('pageInfo') or {}).get('hasNextPage'):
                raise errors.IncompleteData(_('Refund %(refund)s has more %(part)s than can be read at once.') % {
                    'refund': gid, 'part': key})
            refund[key] = {'nodes': page.get('nodes') or []}
        return refund

    # ------------------------------------------------------------------ job
    def _payments_waiting(self):
        """Refund payments of the order that are not recorded yet."""
        self.ensure_one()
        return self.env['eh.shopify.transaction'].search_count([
            ('order_binding_id', '=', self.order_binding_id.id), ('kind', '=', 'REFUND'),
            ('state', 'in', ('pending', 'error'))])

    def _job_refund_import(self, job):
        store = job.store_id
        payload = job.payload or {}
        gid = payload.get('gid')
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', gid)], limit=1)
        if binding.state == 'done' and not binding._payments_waiting():
            return {'unchanged': gid}
        refund = self._read_refund(store, gid)
        if refund is None:
            if binding:
                binding.write({'state': 'error', 'reason': _('Shopify no longer has this refund. Anything already '
                                                             'recorded in Odoo is kept.')})
            return {'missing': gid}
        if binding.state == 'done':
            notes = binding._record_refund_payments(refund, binding.order_binding_id,
                                                    binding.credit_note_ids.filtered(lambda m: m.state == 'posted'))
            binding.reason = ' '.join(notes) or False
            return {'payments': gid}
        order_gid = (refund.get('order') or {}).get('id') or payload.get('order_gid')
        Order = self.env['eh.shopify.order']
        order_binding = Order.search([('store_id', '=', store.id), ('shopify_gid', '=', order_gid)], limit=1)
        if not order_binding.sale_order_id:
            Order._enqueue_import(store, order_gid)
            return {'waiting': 'the order is not in Odoo yet'}
        key = 'presentmentMoney' if order_binding.currency_mode == 'presentment' else 'shopMoney'
        total = ((refund.get('totalRefundedSet') or {}).get(key) or {})
        vals = {'store_id': store.id, 'order_binding_id': order_binding.id, 'shopify_gid': gid,
                'name': _('Refund %s') % (refund.get('legacyResourceId') or gid.rsplit('/', 1)[-1]),
                'processed_at': _parse_dt(refund.get('processedAt') or refund.get('createdAt')),
                'note': (refund.get('note') or '')[:250] or False, 'currency_code': total.get('currencyCode'),
                'total_refunded': float(order_math.dec(total.get('amount')))}
        if binding:
            binding.write(vals)
        else:
            binding = self.create(vals)
        return binding._record_in_odoo(refund, order_binding, key)

    # ------------------------------------------------------------------ credit notes
    def _money(self, bag, key):
        return float(order_math.dec(((bag or {}).get(key) or {}).get('amount')))

    def _split_by_invoice(self, invoices, sale_line, quantity):
        remaining = quantity
        shares = []
        Move = self.env['account.move']
        for invoice in invoices:
            invoiced = sum(invoice.invoice_line_ids.filtered(lambda l: sale_line in l.sale_line_ids).mapped('quantity'))
            credited = sum(Move.search([('reversed_entry_id', '=', invoice.id), ('move_type', '=', 'out_refund'),
                                        ('state', '!=', 'cancel')]).mapped('invoice_line_ids').filtered(
                lambda l: sale_line in l.sale_line_ids).mapped('quantity'))
            take = min(max(0.0, invoiced - credited), remaining)
            if take > 0:
                shares.append((invoice, take))
                remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            shares.append((invoices[-1], remaining))
        return shares

    def _credit_line(self, name, quantity, price_unit, sale_line=None, product=None, link=False):
        """Credit note line. Taxes and product follow ``sale_line``; the line is
        tied to it only with ``link``, for units that leave the order or come
        back, so money refunded on goods the customer keeps never makes the
        order invoiceable again."""
        tax_field = compat.sale_line_names(self.env)[1]
        product = product or (sale_line.product_id if sale_line else self.env['product.product'])
        vals = {'name': name, 'quantity': quantity, 'price_unit': price_unit}
        if product:
            vals['product_id'] = product.id
        if sale_line:
            vals['tax_ids'] = [Command.set(sale_line[tax_field].ids)]
            if link:
                vals['sale_line_ids'] = [Command.set(sale_line.ids)]
        else:
            vals['tax_ids'] = [Command.set([])]
        return vals

    def _line_plan(self, refund, order_binding):
        """Refunded items with their sale line, the units that leave the order
        (refunded before shipping), and the units restocked in Shopify."""
        lines = {line.shopify_gid: line for line in order_binding.line_ids if line.shopify_gid and line.sale_line_id}
        plan, notes = [], []
        for node in (refund.get('refundLineItems') or {}).get('nodes') or []:
            quantity = int(node.get('quantity') or 0)
            if quantity <= 0:
                continue
            line = lines.get((node.get('lineItem') or {}).get('id'))
            if not line:
                notes.append(_('A refunded item is not on the Odoo order, so it was left out.'))
                continue
            sale_line = line.sale_line_id
            restock = node.get('restockType')
            unshipped = max(0.0, sale_line.product_uom_qty - sale_line.qty_delivered)
            if restock == 'CANCEL':
                leaving = quantity
            elif restock == 'NO_RESTOCK':
                leaving = min(quantity, unshipped)
            else:
                leaving = 0
            plan.append({'node': node, 'sale_line': sale_line, 'quantity': quantity, 'leaving': leaving,
                         'linked': quantity if restock in ('CANCEL', 'RETURN', 'LEGACY_RESTOCK') else leaving,
                         'restocked': quantity if restock in ('RETURN', 'LEGACY_RESTOCK') and node.get('restocked')
                         else 0})
        return plan, notes

    def _stock_changes(self, refund, order_binding):
        plan, _notes = self._line_plan(refund, order_binding)
        cancelled = [(item['sale_line'], item['leaving']) for item in plan if item['leaving']]
        restocked = [(item['sale_line'], item['restocked']) for item in plan if item['restocked']]
        return cancelled, restocked

    def _pushed_credit_note(self, refund, order_binding):
        """A posted Odoo credit note this refund was sent from, found by its name
        in the refund note, when the push did not finish recording it."""
        Move = self.env['account.move'].sudo()
        note = (refund.get('note') or '').strip()
        if not note:
            return Move
        candidates = Move.search([('move_type', '=', 'out_refund'), ('state', '=', 'posted'), ('name', '=', note),
                                  ('company_id', '=', self.store_id.company_id.id)])
        candidates = candidates.filtered(lambda move: move._eh_shopify_order_binding() == order_binding)
        if not candidates or self.search_count([('credit_note_ids', 'in', candidates.ids), ('id', '!=', self.id)]):
            return Move
        return candidates[:1]

    def _record_in_odoo(self, refund, order_binding, key):
        self.ensure_one()
        store = self.store_id
        sale_order = order_binding.sale_order_id
        cancelled, restocked = self._stock_changes(refund, order_binding)
        existing = self.credit_note_ids.filtered(lambda move: move.state != 'cancel')
        if existing:
            if existing.filtered(lambda move: move.state == 'draft'):
                return {'draft': self.shopify_gid}
            return self._finish(refund, order_binding, existing, [], cancelled, restocked)
        pushed = self._pushed_credit_note(refund, order_binding)
        if pushed:
            self.credit_note_ids = [Command.set(pushed.ids)]
            pushed.write({'eh_shopify_refund_state': 'done', 'eh_shopify_refund_note': False})
            return self._finish(refund, order_binding, pushed, [], cancelled, restocked)
        invoices = sale_order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted').sorted(lambda m: (m.date, m.id))
        if not invoices:
            self.write({'state': 'pending_invoice', 'reason': _(
                'The order is not invoiced in Odoo yet. The credit note is created when the invoice is posted.')})
            return {'pending_invoice': self.shopify_gid}
        lines = {line.shopify_gid: line for line in order_binding.line_ids if line.shopify_gid and line.sale_line_id}
        by_invoice = {invoice.id: [] for invoice in invoices}
        plan, notes = self._line_plan(refund, order_binding)
        line_count = 0
        for item in plan:
            node, sale_line, quantity = item['node'], item['sale_line'], item['quantity']
            unit = self._money(node.get('subtotalSet'), key) / quantity
            if item['linked']:
                for invoice, share in self._split_by_invoice(invoices, sale_line, item['linked']):
                    by_invoice[invoice.id].append(self._credit_line(sale_line.name, share, unit, sale_line=sale_line,
                                                                    link=True))
                    line_count += 1
            if quantity - item['linked'] > 0:
                by_invoice[invoices[-1].id].append(self._credit_line(sale_line.name, quantity - item['linked'], unit,
                                                                     sale_line=sale_line))
                line_count += 1
        last = invoices[-1]
        for node in refund['refundShippingLines']['nodes']:
            amount = self._money(node.get('subtotalAmountSet'), key)
            if not amount:
                continue
            line = lines.get((node.get('shippingLine') or {}).get('id'))
            by_invoice[last.id].append(self._credit_line(
                _('Shipping refund'), 1, amount, sale_line=line.sale_line_id if line else None,
                product=None if line else order_binding._extra_product('shipping')))
            line_count += 1
        for duty in refund.get('duties') or []:
            amount = self._money(duty.get('amountSet'), key)
            if not amount:
                continue
            line = lines.get((duty.get('originalDuty') or {}).get('id'))
            by_invoice[last.id].append(self._credit_line(
                _('Duties refund'), 1, amount, sale_line=line.sale_line_id if line else None,
                product=None if line else order_binding._extra_product('duty')))
            line_count += 1
        for node in refund['orderAdjustments']['nodes']:
            amount = self._money(node.get('amountSet'), key)
            if not amount:
                continue
            reason = (node.get('reason') or 'OTHER').replace('_', ' ').lower()
            by_invoice[last.id].append(self._credit_line(
                _('Refund adjustment (%s)') % reason, 1, amount, product=order_binding._extra_product('refund_adjustment')))
            line_count += 1
        Move = self.env['account.move']
        credit_notes = Move
        date = (self.processed_at or fields.Datetime.now()).date()
        for invoice in invoices:
            if not by_invoice[invoice.id]:
                continue
            credit_notes |= Move.with_company(invoice.company_id).create({
                'move_type': 'out_refund', 'partner_id': invoice.partner_id.id, 'reversed_entry_id': invoice.id,
                'currency_id': invoice.currency_id.id, 'journal_id': invoice.journal_id.id,
                'fiscal_position_id': invoice.fiscal_position_id.id, 'invoice_origin': sale_order.name,
                'invoice_date': date, 'ref': '%s %s' % (order_binding.name or '', self.name),
                'invoice_line_ids': [Command.create(vals) for vals in by_invoice[invoice.id]]})
        self.credit_note_ids = [Command.set(credit_notes.ids)]
        if not credit_notes:
            self.write({'state': 'error', 'reason': ' '.join(notes) or _('Nothing in this refund matches the Odoo order.')})
            return {'error': self.shopify_gid}
        currency = credit_notes[:1].currency_id
        residual = currency.round(self.total_refunded - sum(credit_notes.mapped('amount_total')))
        tolerance = float(order_math.tolerance(max(1, line_count)))
        if not currency.is_zero(residual):
            if abs(residual) <= tolerance:
                credit_notes[-1].write({'invoice_line_ids': [Command.create(self._credit_line(
                    _('Rounding'), 1, residual, product=order_binding._extra_product('rounding')))]})
            else:
                notes.append(_('The credit notes total %(odoo)s but Shopify refunded %(shopify)s, so they stay in draft.')
                             % {'odoo': currency.round(self.total_refunded - residual), 'shopify': self.total_refunded})
                self.write({'state': 'draft', 'reason': ' '.join(notes)})
                return {'draft': self.shopify_gid, 'residual': residual}
        if not store.refund_auto_post:
            self.write({'state': 'draft', 'reason': ' '.join(notes) or _('Review and post the credit notes.')})
            return {'draft': self.shopify_gid}
        credit_notes.action_post()
        return self._finish(refund, order_binding, credit_notes, notes, cancelled, restocked)

    def _finish(self, refund, order_binding, credit_notes, notes, cancelled, restocked):
        notes = list(notes) + self._record_refund_payments(refund, order_binding, credit_notes)
        self._apply_stock(cancelled, restocked, refund)
        self.write({'state': 'done', 'reason': ' '.join(notes) or False})
        return {'done': self.shopify_gid, 'credit_notes': credit_notes.ids}

    def _record_refund_payments(self, refund, order_binding, credit_notes):
        nodes = [node for node in refund['transactions']['nodes']
                 if node.get('kind') == 'REFUND' and node.get('status') == 'SUCCESS']
        if not nodes:
            return []
        order_binding._record_transactions({'transactions': nodes})
        notes = []
        Transaction = self.env['eh.shopify.transaction']
        for transaction in Transaction.search([('order_binding_id', '=', order_binding.id),
                                               ('shopify_gid', 'in', [node['id'] for node in nodes]),
                                               ('state', 'in', ('pending', 'error'))]):
            gateway = transaction.gateway_id
            if gateway and not gateway.register_payments:
                transaction.write({'state': 'ignored', 'note': _('Payments of this method are not recorded.')})
                continue
            if not gateway.journal_id:
                transaction.write({'state': 'error', 'note': _('Choose the journal for payment method %s.') % (
                    gateway.name or transaction.gateway)})
                notes.append(_('The refund payment is not recorded until payment method %s has a journal.') % (
                    gateway.name or transaction.gateway))
                continue
            open_notes = credit_notes.filtered(lambda m: m.payment_state not in ('paid', 'in_payment', 'reversed'))
            if not open_notes:
                continue
            payments = compat.register_invoice_payment(
                open_notes, gateway.journal_id, amount=transaction.amount,
                date=transaction.processed_at.date() if transaction.processed_at else None,
                memo='%s %s' % (order_binding.name, transaction.shopify_gid.rsplit('/', 1)[-1]),
                method_line=gateway.payment_method_line_id or None)
            transaction.write({'state': 'recorded', 'note': False, 'payment_id': payments[:1].id})
        return notes

    def _apply_stock(self, cancelled, restocked, refund=None):
        cancelled = [(sale_line, quantity) for sale_line, quantity in cancelled if quantity]
        if cancelled:
            relock = compat.unlock_orders(self.env['sale.order'].union(*[line.order_id for line, _q in cancelled]))
            for sale_line, quantity in cancelled:
                remaining = max(sale_line.qty_delivered, sale_line.product_uom_qty - quantity)
                if remaining < sale_line.product_uom_qty:
                    compat.set_sale_line_quantity(sale_line, remaining)
            compat.lock_orders(relock)
        return_gid = ((refund or {}).get('return') or {}).get('id')
        if return_gid and self.env['eh.shopify.return'].search_count([
                ('store_id', '=', self.store_id.id), ('shopify_gid', '=', return_gid),
                ('state', 'in', ('waiting', 'received'))]):
            return
        if restocked:
            self.restock_note = _('%s units were restocked in Shopify. Record the return in Odoo.') % int(
                sum(quantity for _line, quantity in restocked))


class ShopifyOrderRefunds(models.Model):
    _inherit = 'eh.shopify.order'

    def _apply_to_odoo(self, order):
        result = super()._apply_to_odoo(order)
        refund_ids = order.get('_refundIds') or []
        if self.sale_order_id and refund_ids:
            Refund = self.env['eh.shopify.refund'].sudo()
            bindings = {refund.shopify_gid: refund for refund in Refund.search([
                ('store_id', '=', self.store_id.id), ('shopify_gid', 'in', refund_ids)])}
            payments_waiting = self.env['eh.shopify.transaction'].sudo().search_count([
                ('order_binding_id', '=', self.id), ('kind', '=', 'REFUND'), ('state', 'in', ('pending', 'error'))])
            for gid in refund_ids:
                binding = bindings.get(gid)
                if binding and binding.state == 'draft' and binding.credit_note_ids:
                    continue  # posting the credit notes finishes it
                if binding and binding.state == 'done' and not payments_waiting:
                    continue
                Refund._enqueue_refund_import(self.store_id, gid, self.shopify_gid)
        return result


class AccountMoveShopifyRefunds(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        credit_notes = posted.filtered(lambda move: move.move_type == 'out_refund')
        if credit_notes:
            Refund = self.env['eh.shopify.refund'].sudo()
            for refund in Refund.search([('credit_note_ids', 'in', credit_notes.ids), ('state', '=', 'draft')]):
                Refund._enqueue_refund_import(refund.store_id, refund.shopify_gid, refund.order_binding_id.shopify_gid)
        invoices = posted.filtered(lambda move: move.move_type == 'out_invoice')
        if invoices:
            bindings = invoices.mapped('invoice_line_ids.sale_line_ids.order_id.eh_shopify_order_id')
            if bindings:
                Refund = self.env['eh.shopify.refund'].sudo()
                for refund in Refund.search([('order_binding_id', 'in', bindings.ids), ('state', '=', 'pending_invoice')]):
                    Refund._enqueue_refund_import(refund.store_id, refund.shopify_gid, refund.order_binding_id.shopify_gid)
                if not self.env.context.get('eh_shopify_posting'):
                    for binding in bindings.sudo():
                        # An invoice posted by hand is what a Shopify payment was waiting for. The work is queued
                        # only when a payment is actually waiting, so nothing queues behind it for nothing.
                        waiting = binding.transaction_ids.filtered(
                            lambda transaction: transaction.kind in ('SALE', 'CAPTURE')
                            and transaction.state in ('pending', 'error'))
                        if waiting:
                            binding._enqueue_invoice()
        return posted
