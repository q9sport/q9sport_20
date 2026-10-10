# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify customers and B2B companies kept up to date after the first order.

A customer update refreshes the Shopify side of a linked customer: email and
phone fill the contact when empty, and Shopify tags become contact tags under a
Shopify parent tag. Only tags that came from Shopify, and that no other live
link on the same contact still carries, are ever removed, and the contact is
written only when its tags really change.

With Mirror marketing consent, an email unsubscribed in Shopify is blacklisted
in Odoo once per change of state or address. The connector remembers which
address it blacklisted; a later subscription lifts that entry only when no
other linked store still has the address unsubscribed, and never lifts or
revives an entry made in Odoo.

When Shopify withholds protected customer data, only the fields it returned are
used and no contact is created. Customers not linked yet are created only when
the store asks for every new customer. A deleted customer keeps its Odoo
contact, loses its Shopify tags, and its link is marked removed; a merge moves
the link to the customer Shopify kept. Company updates refresh the name of
companies the connector created and the reference only where Odoo has none.
"""
from odoo import _, api, fields, models
from odoo.tools import email_normalize

from ..lib import documents, errors

SHOPIFY_TAG = 'Shopify'
UNSUBSCRIBED = 'UNSUBSCRIBED'


class ShopifyStoreCustomerSync(models.Model):
    _inherit = 'eh.shopify.store'

    mirror_consent = fields.Boolean(
        'Mirror marketing consent', default=False,
        help='When a customer unsubscribes from email marketing in Shopify, the address is added to the Odoo '
             'blacklist, which applies to every company in this database. A later subscription lifts only an entry '
             'the connector added and no other linked store still needs.')
    customers_sync_all = fields.Boolean(
        'Create contacts for new Shopify customers', default=False,
        help='Off: Odoo contacts are created with orders and imports only, and later Shopify changes update them.')


class ShopifyCustomerSync(models.Model):
    _inherit = 'eh.shopify.customer'

    removed_in_shopify = fields.Boolean(readonly=True)
    blacklisted_email = fields.Char('Blacklisted by the connector', readonly=True, copy=False)
    consent_mirrored = fields.Char(readonly=True, copy=False)
    redacted_at = fields.Datetime('Erased at the request of Shopify', readonly=True, copy=False)
    tag_ids = fields.Many2many('res.partner.category', 'eh_shopify_customer_tag_rel', 'customer_id', 'category_id',
                               string='Tags from Shopify', readonly=True)

    @api.model
    def _enqueue_sync(self, store, gid, kind='customer'):
        job_type = 'customer.sync' if kind == 'customer' else 'company.sync'
        return self.env['eh.shopify.job'].sudo()._enqueue_after_running(
            store, job_type, '%s:%s' % (job_type, gid), name=_('Update Shopify %(kind)s %(number)s') % {
                'kind': kind, 'number': gid.rsplit('/', 1)[-1]},
            payload={'gid': gid}, resource_key=gid, priority=6)

    @api.model
    def _binding(self, store, gid):
        return self.sudo().search([('store_id', '=', store.id), ('shopify_gid', '=', gid)], limit=1)

    def _redact(self):
        """Clear what the connector stored about this customer, and the deliveries that carried it. The Odoo
        contact is left to the merchant, who has records to keep."""
        events = self.env['eh.shopify.event'].sudo().search([('store_id', 'in', self.store_id.ids),
                                                             ('resource_gid', 'in', self.mapped('shopify_gid'))])
        events.write({'payload': {'handled': 'customers/redact'}})
        for binding in self:
            binding._apply_tags([])
            binding.write({'email': False, 'phone': False, 'locale': False, 'tags': False,
                           'email_marketing_state': False, 'sms_marketing_state': False,
                           'display_name_shopify': _('Erased at the request of Shopify'),
                           'redacted_at': fields.Datetime.now(), 'removed_in_shopify': True})
        return True

    def _mark_removed(self):
        for binding in self:
            binding._apply_tags([])
        self.write({'removed_in_shopify': True})

    # ------------------------------------------------------------------ customers
    def _job_customer_sync(self, job):
        store = job.store_id
        gid = (job.payload or {}).get('gid')
        binding = self._binding(store, gid)
        if not binding and not store.customers_sync_all:
            return {'skipped': 'the customer is not linked in Odoo'}
        response = store._client().execute(store._doc_text(documents.CUSTOMER), {'id': gid},
                                           tolerate=errors.is_protected_data_error)
        node = (response.data or {}).get('customer')
        withheld = bool(response.warnings)
        if not node:
            if withheld:
                # Without protected customer data approval Shopify hides the customer; it is not deleted.
                return {'skipped': 'Shopify withheld protected customer data'}
            if binding:
                binding._mark_removed()
            return {'missing': gid}
        if withheld:
            if not binding:
                return {'skipped': 'Shopify withheld protected customer data'}
            binding._update_visible(node)
            binding._apply_tags(node.get('tags') or [])
            return {'customer': gid, 'withheld': True}
        method = self._import_customer(store, node)
        binding = self._binding(store, gid)
        if binding.removed_in_shopify:
            binding.removed_in_shopify = False
        binding._apply_tags(node.get('tags') or [])
        if store.mirror_consent:
            binding._mirror_consent()
        return {'customer': gid, 'match': method}

    def _update_visible(self, node):
        """Protected fields come back null without approval; keep what Odoo already knows."""
        self.ensure_one()
        vals = {'tax_exempt': bool(node.get('taxExempt')), 'tags': ','.join(node.get('tags') or []),
                'removed_in_shopify': False}
        for key, field in (('displayName', 'display_name_shopify'), ('locale', 'locale')):
            if node.get(key):
                vals[field] = node[key]
        self.write(vals)

    def _apply_tags(self, tags):
        self.ensure_one()
        Category = self.env['res.partner.category'].sudo().with_context(active_test=False)
        names = sorted({tag.strip() for tag in tags if tag and tag.strip()})
        wanted = Category
        if names:
            parent = Category.search([('name', '=', SHOPIFY_TAG), ('parent_id', '=', False)], limit=1) or \
                Category.create({'name': SHOPIFY_TAG})
            for name in names:
                wanted |= Category.search([('name', '=', name), ('parent_id', '=', parent.id)], limit=1) or \
                    Category.create({'name': name, 'parent_id': parent.id})
        previous = self.with_context(active_test=False).tag_ids
        partner = self.partner_id.sudo().with_context(active_test=False)
        if partner:
            others = self.sudo().search([('partner_id', '=', partner.id), ('id', '!=', self.id),
                                         ('removed_in_shopify', '=', False)])
            still_wanted = others.with_context(active_test=False).mapped('tag_ids')
            current = partner.category_id
            stale = (previous - wanted - still_wanted) & current
            # An archived tag stays hidden: it is neither added again nor needed to be removed.
            missing = wanted.filtered('active') - current - previous
            commands = [(3, category.id) for category in stale] + [(4, category.id) for category in missing]
            if commands:
                partner.write({'category_id': commands})
        if set(previous.ids) != set(wanted.ids):
            self.tag_ids = [(6, 0, wanted.ids)]

    def _mirror_consent(self):
        self.ensure_one()
        email = email_normalize(self.email or '') or False
        state = (self.email_marketing_state or '').upper()
        mark = '%s|%s' % (state, email or '')
        if mark == self.consent_mirrored:
            # Mirror changes only, so an opt in or a removal made in Odoo is not undone by the next update.
            return
        if self.blacklisted_email and (state != UNSUBSCRIBED or self.blacklisted_email != email):
            self._release_blacklist()
        if state == UNSUBSCRIBED and email and self.blacklisted_email != email:
            # An active entry belongs to Odoo and is never claimed; an archived one is an older opt in that this
            # newer unsubscribe replaces.
            Blacklist = self.env['mail.blacklist'].sudo()
            if not Blacklist.search_count([('email', '=', email)]):
                Blacklist._add(email)
                self.blacklisted_email = email
        self.consent_mirrored = mark

    def _release_blacklist(self):
        self.ensure_one()
        address = self.blacklisted_email
        self.blacklisted_email = False
        heirs = self.sudo().search([('id', '!=', self.id), ('email', '=', address), ('removed_in_shopify', '=', False),
                                    ('blacklisted_email', '=', False)]).filtered(
            lambda b: b.store_id.mirror_consent and (b.email_marketing_state or '').upper() == UNSUBSCRIBED)
        if heirs:
            heirs[:1].blacklisted_email = address
            return
        Blacklist = self.env['mail.blacklist'].sudo()
        if Blacklist.search_count([('email', '=', address)]):
            Blacklist._remove(address)

    # ------------------------------------------------------------------ companies
    def _job_company_sync(self, job):
        store = job.store_id
        gid = (job.payload or {}).get('gid')
        binding = self.sudo().search([('store_id', '=', store.id), ('shopify_gid', '=', gid), ('kind', '=', 'company')],
                                     limit=1)
        if not binding:
            return {'skipped': 'the company is not linked in Odoo'}
        data = store._client().execute(store._doc_text(documents.COMPANY), {'id': gid}).data or {}
        node = data.get('company')
        if not node:
            binding._mark_removed()
            return {'missing': gid}
        binding.write({'display_name_shopify': node.get('name'), 'removed_in_shopify': False})
        partner = binding.partner_id.sudo()
        vals = {}
        external = node.get('externalId')
        if external and partner.ref != external and (binding.created_by_connector or not partner.ref):
            vals['ref'] = external
        if binding.created_by_connector and node.get('name') and partner.name != node['name']:
            vals['name'] = node['name']
        if vals and partner:
            partner.write(vals)
        return {'company': gid}
