# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Quantity changes made in Odoo sent to Shopify as an order edit.

Until an order ships or is invoiced, a manager can make the Shopify order hold
the quantities of the Odoo sales order. The proposal is built from the order as
Shopify holds it now, so units already refunded are not proposed again: linked
lines take the Odoo quantity, Odoo lines whose product is a Shopify variant are
added, an Odoo line that matches a Shopify line nobody linked yet is linked
instead of added, and lines Shopify cannot take are listed and left out.

The job reads the order again and rebuilds the proposal, so a change made while
an earlier edit runs is sent after it. Nothing is saved in Shopify before the
commit, so any failure before it is tried again. When the answer to the commit
never arrives, the order is read again: if Shopify holds the edit, it is
recorded as if the answer had arrived; otherwise nothing is recorded. After
the commit the stored Shopify quantities stay on the as sold basis the import
compares with, and the new Shopify lines are linked to their Odoo lines, so the
order that comes back adds nothing twice. An order with a refund not yet
recorded in Odoo is edited in Shopify only, because its Odoo quantities still
include units Shopify already removed.
"""
from odoo import _, api, fields, models

from .. import compat
from ..lib import documents, errors


def _number(gid):
    return (gid or '').rsplit('/', 1)[-1]


def _current(node):
    value = node.get('currentQuantity')
    return int(value if value is not None else node.get('quantity') or 0)


class ShopifyOrderEditPush(models.Model):
    _inherit = 'eh.shopify.order'

    def _edit_blocker(self):
        self.ensure_one()
        sale_order = self.sale_order_id
        store = self.store_id
        if not sale_order or sale_order.state == 'cancel' or self.state == 'cancelled':
            return _('This order is cancelled.')
        if store.state not in ('connected', 'attention'):
            return _('The Shopify store is not connected.')
        if store.granted_scopes and not documents.scope_granted('write_order_edits', store._granted_scope_set()):
            return _('Add the write_order_edits permission to the Shopify app to send order edits.')
        if sale_order.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state == 'done'):
            return _('Part of this order is already shipped. Edit it in Shopify.')
        if sale_order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state == 'posted'):
            return _('This order is already invoiced. Edit it in Shopify.')
        if self.sudo().refund_ids.filtered(lambda refund: refund.state != 'done'):
            # Shopify already removed the refunded units while the Odoo order still shows them, so an edit from
            # here would send them back to the customer's order.
            return _('A refund on this order is not recorded in Odoo yet. Edit the order in Shopify.')
        return False

    @staticmethod
    def _whole_units(sale_line):
        quantity = sale_line[compat.sale_line_uom_field()]._compute_quantity(sale_line.product_uom_qty,
                                                                            sale_line.product_id.uom_id)
        return int(round(quantity)) if abs(quantity - round(quantity)) < 1e-6 else None

    def _edit_proposal(self, order):
        """``set`` for linked lines whose current Shopify quantity differs,
        ``add`` for Odoo lines whose variant is not on the order, ``link`` for
        Odoo lines matching a Shopify line nobody linked, and ``skipped``."""
        self.ensure_one()
        sale_order = self.sale_order_id
        nodes = {node['id']: node for node in ((order or {}).get('lineItems') or {}).get('nodes') or []}
        known = set(self.line_ids.mapped('shopify_gid'))
        proposal = {'set': [], 'add': [], 'link': [], 'skipped': []}
        for line in self.line_ids.filtered(lambda l: l.kind == 'product' and l.shopify_gid):
            node = nodes.get(line.shopify_gid)
            if not node:
                continue
            sale_line = line.sale_line_id if line.sale_line_id.order_id == sale_order else line.sale_line_id.browse()
            name = sale_line.name or line.sku or line.shopify_gid
            wanted = self._whole_units(sale_line) if sale_line else 0
            if wanted is None:
                proposal['skipped'].append((name, _('Shopify takes whole units only.')))
            elif wanted != _current(node):
                proposal['set'].append({'line_gid': line.shopify_gid, 'name': name, 'before': _current(node),
                                        'after': wanted})
        Variant = self.env['eh.shopify.variant'].sudo()
        linked = self.line_ids.mapped('sale_line_id')
        claimed = set()
        for sale_line in sale_order.order_line - linked:
            if sale_line.display_type or not sale_line.product_id or sale_line.product_uom_qty <= 0 \
                    or getattr(sale_line, 'is_delivery', False) or getattr(sale_line, 'is_downpayment', False):
                continue
            variant = Variant.search([('store_id', '=', self.store_id.id), ('product_id', '=', sale_line.product_id.id),
                                      ('active_in_shopify', '=', True)], limit=1)
            wanted = self._whole_units(sale_line)
            if not variant:
                proposal['skipped'].append((sale_line.name, _('Not sold in Shopify.')))
                continue
            if wanted is None:
                proposal['skipped'].append((sale_line.name, _('Shopify takes whole units only.')))
                continue
            same = [node for node in nodes.values() if (node.get('variant') or {}).get('id') == variant.shopify_gid]
            if not same:
                proposal['add'].append({'variant_gid': variant.shopify_gid, 'sale_line_id': sale_line.id,
                                        'name': sale_line.name, 'before': 0, 'after': wanted})
                continue
            free = next((node for node in same if node['id'] not in known and node['id'] not in claimed
                         and _current(node) == wanted), None)
            if free:
                claimed.add(free['id'])
                proposal['link'].append({'line_gid': free['id'], 'variant_gid': variant.shopify_gid,
                                         'sale_line_id': sale_line.id, 'name': sale_line.name, 'before': wanted,
                                         'after': wanted})
            else:
                proposal['skipped'].append((sale_line.name, _('Already on the Shopify order. Change the quantity of '
                                                              'that line instead.')))
        return proposal

    def _edit_landed(self, order, proposal):
        """Whether ``order``, read after a commit whose answer never arrived, holds the edit."""
        nodes = ((order or {}).get('lineItems') or {}).get('nodes') or []
        by_gid = {node['id']: node for node in nodes}
        for change in proposal['set']:
            node = by_gid.get(change['line_gid'])
            if node and _current(node) == change['after']:
                return True
        known = set(self.line_ids.mapped('shopify_gid'))
        added = {change['variant_gid'] for change in proposal['add']}
        return any(node['id'] not in known and (node.get('variant') or {}).get('id') in added for node in nodes)

    def _record_edit(self, nodes, proposal, refunded):
        """Store what Shopify holds now: quantities on the as sold basis and the
        links of new Shopify lines. Returns the additions and links recorded."""
        self.ensure_one()
        for change in proposal['set']:
            self.line_ids.filtered(lambda l, gid=change['line_gid']: l.shopify_gid == gid).write(
                {'quantity': change['after'] + int(refunded.get(change['line_gid'], 0))})
        known = set(self.line_ids.mapped('shopify_gid'))
        OrderLine = self.env['eh.shopify.order.line'].sudo()
        recorded = []
        for change in proposal['link'] + proposal['add']:
            if change in proposal['link']:
                node = {'id': change['line_gid']} if change['line_gid'] not in known else None
            else:
                node = next((n for n in nodes if n['id'] not in known
                             and (n.get('variant') or {}).get('id') == change['variant_gid']), None)
            if not node:
                continue
            sale_line = self.env['sale.order.line'].browse(change['sale_line_id']).exists()
            OrderLine.create({'order_binding_id': self.id, 'kind': 'product', 'shopify_gid': node['id'],
                              'variant_gid': change['variant_gid'], 'quantity': change['after'],
                              'sku': sale_line.product_id.default_code or False, 'sale_line_id': sale_line.id or False})
            known.add(node['id'])
            recorded.append(change)
        return recorded

    def _enqueue_edit_push(self, notify, note):
        self.ensure_one()
        return self.env['eh.shopify.job'].sudo()._enqueue_after_running(
            self.store_id, 'order.edit.push', 'order.edit.push:%s' % self.id,
            name=_('Send edits of %s to Shopify') % (self.name or self.shopify_gid),
            payload={'order_binding_id': self.id, 'notify': bool(notify), 'note': note or False},
            resource_key=self.shopify_gid, priority=4, record=self)

    def _job_order_edit_push(self, job):
        payload = job.payload or {}
        binding = self.browse(payload.get('order_binding_id')).exists()
        if not binding:
            return {'skipped': 'the order link is gone'}
        blocker = binding._edit_blocker()
        if blocker:
            binding.reason = blocker
            return {'skipped': blocker}
        store = binding.store_id
        gid = binding.shopify_gid
        order = self._fetch_order(store, gid)
        proposal = binding._edit_proposal(order)
        refunded = order.get('_refundedQuantities') or {}
        if not proposal['set'] and not proposal['add']:
            if proposal['link']:
                binding._record_edit([], proposal, refunded)
                self._enqueue_import(store, gid)
                return {'linked': len(proposal['link'])}
            return {'skipped': 'Shopify already holds the quantities of this order'}
        client = store._client()
        try:
            begun = client.mutate(store._doc_text(documents.ORDER_EDIT_BEGIN), {'id': gid},
                                  documents.ORDER_EDIT_BEGIN.payload_key)
            calculated = begun.get('calculatedOrder') or {}
            by_number = {_number(node['id']): node for node in (calculated.get('lineItems') or {}).get('nodes') or []}
            for change in proposal['set']:
                node = by_number.get(_number(change['line_gid']))
                if not node:
                    raise errors.UserErrors(_('%s is no longer on the Shopify order.') % change['name'])
                if int(node.get('quantity') or 0) != change['after']:
                    client.mutate(store._doc_text(documents.ORDER_EDIT_SET_QUANTITY),
                                  {'id': calculated['id'], 'lineItemId': node['id'], 'quantity': change['after']},
                                  documents.ORDER_EDIT_SET_QUANTITY.payload_key)
            for change in proposal['add']:
                client.mutate(store._doc_text(documents.ORDER_EDIT_ADD_VARIANT),
                              {'id': calculated['id'], 'variantId': change['variant_gid'], 'quantity': change['after']},
                              documents.ORDER_EDIT_ADD_VARIANT.payload_key)
        except errors.UserErrors as error:
            binding.reason = _('Shopify refused the edit: %s') % error.message
            return {'refused': error.message}
        except errors.TransientError as error:
            if error.ambiguous:
                # Nothing is saved before the commit, so the whole edit is begun again later.
                raise errors.TransientError(error.message, retry_after=30)
            raise
        try:
            committed = client.mutate(store._doc_text(documents.ORDER_EDIT_COMMIT),
                                      {'id': calculated['id'], 'notifyCustomer': bool(payload.get('notify')),
                                       'staffNote': payload.get('note') or None},
                                      documents.ORDER_EDIT_COMMIT.payload_key)
            nodes = ((committed.get('order') or {}).get('lineItems') or {}).get('nodes') or []
        except errors.UserErrors as error:
            binding.reason = _('Shopify refused the edit: %s') % error.message
            return {'refused': error.message}
        except errors.TransientError as error:
            if not error.ambiguous:
                raise
            after = self._fetch_order(store, gid)
            if not binding._edit_landed(after, proposal):
                binding.reason = _('Shopify did not save the edit. Send the changes again.')
                return {'not_saved': gid}
            nodes = (after.get('lineItems') or {}).get('nodes') or []
        recorded = binding._record_edit(nodes, proposal, refunded)
        summary = ['%s: %s' % (change['name'], change['after']) for change in proposal['set'] + recorded]
        binding.sale_order_id.message_post(body=_('Edit saved in Shopify: %s.') % '; '.join(summary))
        binding.reason = False
        self._enqueue_import(store, gid)
        return {'committed': gid, 'changed': len(proposal['set']), 'added': len(recorded)}


class SaleOrderEditPush(models.Model):
    _inherit = 'sale.order'

    eh_shopify_can_edit = fields.Boolean(compute='_compute_eh_shopify_can_edit')

    @api.depends('eh_shopify_order_id', 'state')
    def _compute_eh_shopify_can_edit(self):
        for order in self:
            order.eh_shopify_can_edit = bool(order.sudo().eh_shopify_order_id) and order.state != 'cancel'

    def action_eh_shopify_send_edits(self):
        self.ensure_one()
        return self.env['eh.shopify.order.edit.wizard']._open_for(self)
