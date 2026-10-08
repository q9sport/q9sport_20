# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Customer and company matching.

Order of precedence: an existing link for the Shopify id, then the exact
normalised email, then the exact phone, then a new contact. Addresses become
child contacts reused by their address identity. No external lookups: a
country or state that cannot be resolved from its ISO code is left empty and
noted, never guessed through a third party service.
"""
from odoo import _, api, fields, models

from ..lib import util


class ShopifyCustomer(models.Model):
    _name = 'eh.shopify.customer'
    _description = 'Shopify Customer'
    _order = 'id desc'
    _rec_name = 'display_name_shopify'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    kind = fields.Selection([('customer', 'Customer'), ('company', 'B2B company')], default='customer', required=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    partner_id = fields.Many2one('res.partner', ondelete='cascade', index=True)
    display_name_shopify = fields.Char('Name in Shopify', readonly=True)
    email = fields.Char(readonly=True)
    phone = fields.Char(readonly=True)
    email_marketing_state = fields.Char('Email marketing', readonly=True)
    sms_marketing_state = fields.Char('SMS marketing', readonly=True)
    tax_exempt = fields.Boolean(readonly=True)
    tags = fields.Char(readonly=True)
    locale = fields.Char(readonly=True)
    created_by_connector = fields.Boolean(readonly=True)
    match_method = fields.Selection([
        ('binding', 'Linked'), ('email', 'Email'), ('phone', 'Phone'), ('created', 'Created'), ('manual', 'Manual'),
    ], readonly=True)
    shopify_updated_at = fields.Datetime('Updated in Shopify', readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_customer_gid_uniq "
                            "ON eh_shopify_customer (store_id, shopify_gid)")

    # ------------------------------------------------------------------ geography
    @api.model
    def _country_state(self, address):
        country = state = self.env['res.country']
        code = (address or {}).get('countryCodeV2') or (address or {}).get('countryCode')
        if code:
            country = self.env['res.country'].search([('code', '=', code.upper())], limit=1)
        province = (address or {}).get('provinceCode')
        if country and province:
            state = self.env['res.country.state'].search([('country_id', '=', country.id),
                                                          ('code', '=', province.upper())], limit=1)
        return country, state

    @api.model
    def _address_vals(self, address):
        country, state = self._country_state(address)
        vals = {
            'street': address.get('address1') or False,
            'street2': address.get('address2') or False,
            'city': address.get('city') or False,
            'zip': address.get('zip') or False,
            'country_id': country.id or False,
            'state_id': state.id or False,
        }
        if address.get('phone'):
            vals['phone'] = address['phone']
        return vals

    # ------------------------------------------------------------------ matching
    def _partner_domain(self, store):
        return ['|', ('company_id', '=', False), ('company_id', '=', store.company_id.id)]

    @api.model
    def _find_partner(self, store, email, phone):
        Partner = self.env['res.partner'].with_context(active_test=True)
        if email:
            field = 'email_normalized' if 'email_normalized' in Partner._fields else 'email'
            partner = Partner.search(self._partner_domain(store) + [(field, '=', email), ('parent_id', '=', False)],
                                     order='id', limit=1)
            if partner:
                return partner, 'email'
        if phone:
            partner = Partner.search(self._partner_domain(store) + [('phone', '=', phone), ('parent_id', '=', False)],
                                     order='id', limit=1)
            if partner:
                return partner, 'phone'
        return Partner.browse(), None

    @staticmethod
    def _company_address(address):
        """A B2B company location address in the shape of an order address."""
        if not address:
            return {}
        person = ' '.join(part for part in (address.get('firstName'), address.get('lastName')) if part)
        return {'name': address.get('recipient') or person or None, 'firstName': address.get('firstName'),
                'lastName': address.get('lastName'), 'company': address.get('companyName'),
                'address1': address.get('address1'), 'address2': address.get('address2'), 'city': address.get('city'),
                'zip': address.get('zip'), 'provinceCode': address.get('zoneCode'),
                'countryCodeV2': address.get('countryCode'), 'phone': address.get('phone')}

    @api.model
    def _child_for_address(self, parent, address, kind, name):
        """Reuse or create the invoice or delivery contact for an address."""
        if not address:
            return parent
        key = util.address_key(address, name=name)
        if not key:
            return parent
        if parent.eh_shopify_address_key == key:
            return parent
        Partner = self.env['res.partner']
        child = Partner.search([('parent_id', '=', parent.id), ('eh_shopify_address_key', '=', key)], limit=1)
        if child:
            return child
        if not parent.eh_shopify_address_key and not parent.street and not parent.child_ids:
            parent.write(dict(self._address_vals(address), eh_shopify_address_key=key))
            return parent
        vals = dict(self._address_vals(address), parent_id=parent.id, type=kind, eh_shopify_address_key=key,
                    name=util.display_name(address=address, fallback=parent.name))
        return Partner.create(vals)

    @api.model
    def _resolve_company(self, store, purchasing):
        company = (purchasing or {}).get('company') or {}
        if not company.get('id'):
            return self.env['res.partner']
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', company['id'])], limit=1)
        if binding and binding.partner_id:
            return binding.partner_id
        Partner = self.env['res.partner']
        partner = Partner.search(self._partner_domain(store) + [('is_company', '=', True),
                                                                 ('name', '=', company.get('name'))], limit=1)
        method = 'binding' if partner else 'created'
        if not partner:
            partner = Partner.create({'name': company.get('name') or _('Shopify company'), 'is_company': True,
                                      'company_id': store.company_id.id,
                                      'ref': company.get('externalId') or False})
        vals = {'store_id': store.id, 'kind': 'company', 'shopify_gid': company['id'], 'partner_id': partner.id,
                'display_name_shopify': company.get('name'), 'match_method': method,
                'created_by_connector': method == 'created'}
        if binding:
            binding.write(vals)
        else:
            self.create(vals)
        location = (purchasing or {}).get('location') or {}
        # taxRegistrationId directly on the location is deprecated on every supported version.
        tax_id = (location.get('taxSettings') or {}).get('taxRegistrationId') or location.get('taxRegistrationId')
        if tax_id and not partner.vat:
            partner.vat = tax_id
        return partner

    @api.model
    def _resolve(self, store, order):
        """Return partners and fiscal position for an order payload."""
        # Tax ids and addresses come from the checkout, where nobody can be asked to correct them: they are
        # kept as Shopify sends them instead of stopping the order on the tax id format check of the contact.
        self = self.with_context(no_vat_validation=True)
        customer = order.get('customer') or {}
        billing = order.get('billingAddress') or {}
        shipping = order.get('shippingAddress') or {}
        email = util.normalize_email(((customer.get('defaultEmailAddress') or {}).get('emailAddress'))
                                     or order.get('email'))
        phone = ((customer.get('defaultPhoneNumber') or {}).get('phoneNumber')) or order.get('phone') or None
        Partner = self.env['res.partner']
        binding = self.browse()
        partner = Partner.browse()
        method = None
        if customer.get('id'):
            binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', customer['id'])], limit=1)
            if binding.partner_id:
                partner, method = binding.partner_id, 'binding'
        is_pos = bool(order.get('retailLocation')) or (order.get('sourceName') or '').lower() == 'pos'
        if not partner and is_pos and not customer.get('id') and not email and not phone:
            # A till sale without a customer goes to one walk-in partner, not a new contact per sale.
            partner, method = store._pos_walk_in_partner(), 'walk_in'
        if not partner:
            partner, method = self._find_partner(store, email, phone)
        created = False
        if not partner:
            name = util.display_name(customer, billing or shipping, email, phone,
                                     fallback=_('Shopify customer %s') % (order.get('name') or ''))
            vals = {'name': name, 'email': email or False, 'phone': phone or False,
                    'company_id': store.company_id.id, 'customer_rank': 1}
            partner = Partner.create({k: v for k, v in vals.items() if k in Partner._fields})
            method, created = 'created', True
        else:
            fill = {}
            if email and not partner.email:
                fill['email'] = email
            if phone and not partner.phone:
                fill['phone'] = phone
            if fill:
                partner.write(fill)
        if customer.get('id'):
            vals = {
                'store_id': store.id, 'kind': 'customer', 'shopify_gid': customer['id'], 'partner_id': partner.id,
                'display_name_shopify': customer.get('displayName'), 'email': email, 'phone': phone,
                'email_marketing_state': (customer.get('defaultEmailAddress') or {}).get('marketingState'),
                'sms_marketing_state': (customer.get('defaultPhoneNumber') or {}).get('marketingState'),
                'tax_exempt': bool(customer.get('taxExempt')), 'tags': ','.join(customer.get('tags') or []),
                'locale': customer.get('locale'), 'match_method': method,
            }
            if binding:
                binding.write(vals)
            else:
                vals['created_by_connector'] = created
                binding = self.create(vals)
        commercial = partner
        purchasing = order.get('purchasingEntity') or {}
        if purchasing.get('__typename') == 'PurchasingCompany':
            company_partner = self._resolve_company(store, purchasing)
            if company_partner and partner != company_partner and not partner.parent_id:
                if partner.create_date == partner.write_date or created:
                    partner.write({'parent_id': company_partner.id, 'type': 'contact'})
            commercial = company_partner or partner
        location = (purchasing.get('location') or {}) if purchasing.get('__typename') == 'PurchasingCompany' else {}
        if location:
            # The company location invoices; the order keeps its own delivery address when it has one.
            billing = self._company_address(location.get('billingAddress')) or billing
            shipping = shipping or self._company_address(location.get('shippingAddress'))
        billing_name = util.display_name(address=billing, fallback=partner.name)
        shipping_name = util.display_name(address=shipping, fallback=partner.name)
        invoice_partner = self._child_for_address(commercial, billing, 'invoice', billing_name)
        shipping_partner = self._child_for_address(commercial, shipping, 'delivery', shipping_name) \
            if shipping else invoice_partner
        fiscal_position = self.env['account.fiscal.position']
        location_exempt = bool((location.get('taxSettings') or {}).get('taxExempt'))
        if (customer.get('taxExempt') or order.get('taxExempt') or location_exempt) and store.tax_exempt_fiscal_position_id:
            fiscal_position = store.tax_exempt_fiscal_position_id
        return {'partner': commercial, 'contact': partner, 'invoice': invoice_partner, 'shipping': shipping_partner,
                'fiscal_position': fiscal_position, 'binding': binding, 'match_method': method}
