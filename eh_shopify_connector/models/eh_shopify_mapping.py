# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models


class ShopifyGateway(models.Model):
    """How payments from one Shopify payment method are recorded in Odoo."""

    _name = 'eh.shopify.gateway'
    _description = 'Shopify Payment Method Mapping'
    _order = 'store_id, name'
    _check_company_auto = True

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    name = fields.Char('Shopify payment method', required=True, readonly=True)
    tender_type = fields.Selection([
        ('payment', 'Payment'),
        ('manual', 'Manual payment'),
        ('gift_card', 'Gift card'),
        ('store_credit', 'Store credit'),
    ], default='payment', required=True)
    journal_id = fields.Many2one('account.journal', string='Record in journal', check_company=True,
                                 domain="[('type', 'in', ('bank', 'cash'))]")
    payment_method_line_id = fields.Many2one('account.payment.method.line', string='Payment method',
                                             domain="[('journal_id', '=', journal_id), "
                                                    "('payment_type', '=', 'inbound')]")
    register_payments = fields.Boolean('Record payments', default=True)
    active = fields.Boolean(default=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_gateway_name_uniq "
                            "ON eh_shopify_gateway (store_id, name)")

    @api.model
    def _for(self, store, name, manual=False):
        name = name or 'unknown'
        gateway = self.with_context(active_test=False).search([('store_id', '=', store.id), ('name', '=', name)],
                                                              limit=1)
        if gateway:
            return gateway
        lowered = name.lower()
        tender = 'gift_card' if lowered == 'gift_card' else 'store_credit' if 'store_credit' in lowered \
            else 'manual' if manual else 'payment'
        return self.create({'store_id': store.id, 'name': name, 'tender_type': tender})


class ShopifyCarrier(models.Model):
    """How a Shopify shipping method maps to an Odoo carrier, both ways."""

    _name = 'eh.shopify.carrier'
    _description = 'Shopify Shipping Method Mapping'
    _order = 'store_id, title'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    title = fields.Char('Shopify shipping method', required=True, readonly=True)
    code = fields.Char(readonly=True)
    source = fields.Char(readonly=True)
    carrier_id = fields.Many2one('delivery.carrier', string='Odoo carrier')
    tracking_company = fields.Char('Tracking company sent to Shopify',
                                   help='Carrier name Shopify uses to build tracking links, for example UPS, DHL '
                                        'Express or Australia Post.')

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_carrier_title_uniq "
                            "ON eh_shopify_carrier (store_id, lower(title), coalesce(code, ''))")

    @api.model
    def _for(self, store, shipping_line):
        title = shipping_line.get('title') or shipping_line.get('code') or 'Shipping'
        code = shipping_line.get('code') or False
        for carrier in self.search([('store_id', '=', store.id)]):
            if carrier.title.lower() == title.lower() and (carrier.code or '') == (code or ''):
                return carrier
        return self.create({'store_id': store.id, 'title': title, 'code': code,
                            'source': shipping_line.get('source') or False})
