# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import fields, models


class ShopifyWebhook(models.Model):
    _name = 'eh.shopify.webhook'
    _description = 'Shopify Webhook Subscription'
    _order = 'store_id, topic'
    _rec_name = 'topic'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    topic = fields.Char(required=True)
    subscription_gid = fields.Char('Shopify ID', readonly=True)
    uri = fields.Char('Address', readonly=True)
    api_version = fields.Char('API version', readonly=True)
    state = fields.Selection([
        ('active', 'Active'),
        ('removed', 'Removed'),
    ], default='active', required=True)
    last_verified_at = fields.Datetime('Last verified', readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_webhook_topic_uniq "
                            "ON eh_shopify_webhook (store_id, topic)")
