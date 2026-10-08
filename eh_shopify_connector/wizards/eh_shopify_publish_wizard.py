# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

LIVE_STATES = ('connected', 'attention')


class ShopifyPublishWizard(models.TransientModel):
    _name = 'eh.shopify.publish.wizard'
    _description = 'Publish Products to Shopify'

    store_id = fields.Many2one('eh.shopify.store', required=True,
                               domain="[('products_enabled', '=', True), ('state', 'in', ('connected', 'attention'))]")
    product_tmpl_ids = fields.Many2many('product.template', string='Products', required=True)
    status = fields.Selection([
        ('DRAFT', 'Draft, reviewed in Shopify before selling'),
        ('ACTIVE', 'Active, on sale at once'),
    ], string='Start as', default='DRAFT', required=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        context = self.env.context
        if context.get('active_model') == 'product.template' and context.get('active_ids') \
                and 'product_tmpl_ids' in fields_list:
            values['product_tmpl_ids'] = [(6, 0, context['active_ids'])]
        stores = self.env['eh.shopify.store'].search([('products_enabled', '=', True), ('state', 'in', LIVE_STATES)],
                                                     limit=2)
        if len(stores) == 1 and 'store_id' in fields_list and not values.get('store_id'):
            values['store_id'] = stores.id
        return values

    def action_publish(self):
        self.ensure_one()
        if not self.env.user.has_group('eh_shopify_connector.group_shopify_manager'):
            raise AccessError(_('Only a Shopify manager can publish products.'))
        store = self.store_id
        if not store.products_enabled or store.state not in LIVE_STATES:
            raise UserError(_('Choose a connected store with Sync products switched on.'))
        Product = self.env['eh.shopify.product'].sudo()
        for template in self.product_tmpl_ids:
            Product._enqueue_publish(store, template, self.status)
        notification = store._notify(_('Publishing queued'), _(
            '%s products are sent to Shopify in the background. Progress shows under Sync Jobs.')
            % len(self.product_tmpl_ids))
        # Chain a close, or the dialog stays open as if nothing happened.
        return dict(notification, params=dict(notification.get('params') or {},
                                              next={'type': 'ir.actions.act_window_close'}))
