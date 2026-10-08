# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Order history and customers from Shopify, in bulk.

A manager picks the date to import order history from. Shopify exports the ids
of the orders created since then, and each order is read and imported by the
normal order import job, so history follows the same rules as new orders,
except the import start date. A historical order is confirmed, invoiced and
paid as the store is set up, then locked, but it moves no stock and nobody is
asked to ship it: its transfers are cancelled and nothing is sent to Shopify.
Customers are exported the same way and linked to Odoo contacts by email or
phone, or created, whether or not they ever ordered.
"""
import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .. import compat
from ..lib import util
from .eh_shopify_bulk import ACTIVE_STATES


class ShopifyStoreHistory(models.Model):
    _inherit = 'eh.shopify.store'

    history_orders_from = fields.Date('Import order history from',
                                      help='Orders created in Shopify since this date are imported without moving '
                                           'stock. Orders already in Odoo are left as they are.')

    def _start_history_export(self, kind, name, payload):
        self.ensure_one()
        compat.check_access(self, 'write')
        if self.env['eh.shopify.bulk'].search_count([('store_id', '=', self.id), ('kind', '=', kind),
                                                     ('state', 'in', ACTIVE_STATES)]):
            return False
        self.env['eh.shopify.job'].sudo()._enqueue(self, 'bulk.start', name=name, payload=dict(payload, kind=kind),
                                                   dedup_key='bulk.start:%s' % kind, priority=8)
        return True

    def action_import_order_history(self):
        self.ensure_one()
        if not self.history_orders_from:
            raise UserError(_('Choose the date to import order history from.'))
        started = self._start_history_export('order_ids', _('Export order history from Shopify'),
                                             {'since': fields.Date.to_string(self.history_orders_from)})
        if not started:
            return self._notify(_('Order history import already running'),
                                _('Progress shows under Activity, Shopify Exports.'))
        return self._notify(_('Order history import started'), _(
            'Shopify lists the orders created since %s, then each one is imported like a new order, without moving '
            'stock.') % self.history_orders_from)

    def action_import_customers(self):
        self.ensure_one()
        if not self._start_history_export('customers', _('Export customers from Shopify'), {}):
            return self._notify(_('Customer import already running'),
                                _('Progress shows under Activity, Shopify Exports.'))
        return self._notify(_('Customer import started'),
                            _('Each Shopify customer is linked to an Odoo contact by email or phone, or created.'))


class ShopifyBulkHistory(models.Model):
    _inherit = 'eh.shopify.bulk'

    @api.model
    def _bulk_query(self, store, kind, payload):
        query = super()._bulk_query(store, kind, payload)
        if kind == 'order_ids':
            since = payload.get('since') or '2000-01-01'
            datetime.datetime.strptime(since, '%Y-%m-%d')
            query = query.replace("created_at:>='2000-01-01'", "created_at:>='%s'" % since, 1)
        return query

    def _job_bulk_chunk(self, job):
        payload = job.payload or {}
        kind = payload.get('kind')
        if kind not in ('order_ids', 'customers'):
            return super()._job_bulk_chunk(job)
        store = job.store_id
        bulk = self.browse(payload.get('bulk_id')).exists()
        roots = payload.get('roots') or []
        if kind == 'order_ids':
            Order = self.env['eh.shopify.order']
            gids = [root['id'] for root in roots if root.get('id')]
            in_odoo = set(Order.search([('store_id', '=', store.id), ('shopify_gid', 'in', gids),
                                        ('sale_order_id', '!=', False)]).mapped('shopify_gid'))
            queued = 0
            for gid in gids:
                if gid not in in_odoo:
                    Order._enqueue_import(store, gid, priority=9, historical=True)
                    queued += 1
            result = {'queued': queued, 'already_in_odoo': len(in_odoo)}
        else:
            Customer = self.env['eh.shopify.customer']
            result = {'linked': 0, 'created': 0}
            for root in roots:
                with self.env.cr.savepoint():
                    method = Customer._import_customer(store, root)
                result['created' if method == 'created' else 'linked'] += 1
        if bulk and bulk.state == 'importing':
            others = self.env['eh.shopify.job'].sudo().search_count(
                bulk._chunk_domain() + [('state', 'in', ('pending', 'running')), ('id', '!=', job.id)])
            if not others:
                bulk._close_import()
        return result


class ShopifyCustomerHistory(models.Model):
    _inherit = 'eh.shopify.customer'

    @api.model
    def _import_customer(self, store, node):
        """Link or create the Odoo contact of a Shopify customer from an export. Returns how it matched."""
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1)
        email = util.normalize_email((node.get('defaultEmailAddress') or {}).get('emailAddress'))
        phone = (node.get('defaultPhoneNumber') or {}).get('phoneNumber') or None
        address = node.get('defaultAddress') or {}
        Partner = self.env['res.partner']
        if binding.partner_id:
            partner, method = binding.partner_id, 'binding'
        else:
            partner, method = self._find_partner(store, email, phone)
        created = False
        if not partner:
            vals = self._address_vals(address)
            vals.update({'name': util.display_name(node, address, email, phone, fallback=_('Shopify customer %s') % (
                node['id'].rsplit('/', 1)[-1])), 'email': email or False, 'company_id': store.company_id.id,
                'customer_rank': 1})
            if phone:
                vals['phone'] = phone
            partner = Partner.create({key: value for key, value in vals.items() if key in Partner._fields})
            method, created = 'created', True
        else:
            fill = {}
            if email and not partner.email:
                fill['email'] = email
            if phone and not partner.phone:
                fill['phone'] = phone
            if fill:
                partner.write(fill)
        vals = {'store_id': store.id, 'kind': 'customer', 'shopify_gid': node['id'], 'partner_id': partner.id,
                'display_name_shopify': node.get('displayName'), 'email': email, 'phone': phone,
                'email_marketing_state': (node.get('defaultEmailAddress') or {}).get('marketingState'),
                'sms_marketing_state': (node.get('defaultPhoneNumber') or {}).get('marketingState'),
                'tax_exempt': bool(node.get('taxExempt')), 'tags': ','.join(node.get('tags') or []),
                'locale': node.get('locale'), 'match_method': method}
        if binding:
            binding.write(vals)
        else:
            vals['created_by_connector'] = created
            self.create(vals)
        return method
