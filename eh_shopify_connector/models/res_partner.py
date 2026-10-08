# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_shopify_address_key = fields.Char(index=True, copy=False, readonly=True,
                                         help='Identity of the Shopify address this contact was created from.')
