# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..lib import errors, hmac_util, versions


class ShopifySetupWizard(models.TransientModel):
    _name = 'eh.shopify.setup.wizard'
    _description = 'Connect a Shopify Store'

    step = fields.Selection([('connect', 'Connect'), ('review', 'Review'), ('done', 'Done')],
                            default='connect', required=True)
    store_id = fields.Many2one('eh.shopify.store')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    shop_domain = fields.Char('Store address', help='yourstore.myshopify.com or the admin link of the store.')
    auth_mode = fields.Selection([
        ('client_credentials', 'App in my Shopify organisation'),
        ('authorization_code', 'Install link'),
        ('legacy_token', 'Existing admin API token'),
    ], string='Connection method', default='client_credentials', required=True)
    client_id = fields.Char('Client ID')
    # Secrets typed in the dialog never reach the database in clear text: the
    # client saves the dialog before the button runs, so the inverse seals each
    # value with the credential vault straight away.
    client_secret = fields.Char('Client secret', compute='_compute_secret_inputs', inverse='_inverse_client_secret')
    access_token = fields.Char('Admin API access token', compute='_compute_secret_inputs',
                               inverse='_inverse_access_token')
    webhook_secret = fields.Char('API secret key', compute='_compute_secret_inputs', inverse='_inverse_webhook_secret')
    client_secret_sealed = fields.Char(copy=False)
    access_token_sealed = fields.Char(copy=False)
    webhook_secret_sealed = fields.Char(copy=False)
    api_version = fields.Selection(selection=lambda self: self.env['eh.shopify.store']._selection_api_version(),
                                   default=lambda self: versions.DEFAULT, required=True, string='API version')

    shop_name = fields.Char(related='store_id.shop_name')
    currency_code = fields.Char(related='store_id.currency_code')
    plan_name = fields.Char(related='store_id.plan_name')
    taxes_included = fields.Boolean(related='store_id.taxes_included')
    missing_required_scopes = fields.Text(related='store_id.missing_required_scopes')
    missing_optional_scopes = fields.Text(related='store_id.missing_optional_scopes')
    location_count = fields.Integer(related='store_id.location_count')
    webhook_url = fields.Char(related='store_id.webhook_url')
    warehouse_id = fields.Many2one('stock.warehouse', string='Default warehouse',
                                   domain="[('company_id', '=', company_id)]")
    register_webhooks = fields.Boolean('Turn on real time updates', default=True)
    # Finishing the wizard is the moment orders start reaching the real books, so the
    # answers that decide what lands there are asked here rather than assumed.
    import_orders = fields.Boolean('Start importing orders', default=True,
                                   help='Leave this off to connect the store and set everything up first. '
                                        'Turn it on later on the store.')
    order_import_from = fields.Datetime('Import orders placed from', default=fields.Datetime.now,
                                        help='Orders placed before this moment stay in Shopify. Use the '
                                             'historical import to bring older orders in once you are ready.')
    order_confirm = fields.Boolean('Confirm imported orders', default=True)
    invoice_policy = fields.Selection([
        ('paid', 'When paid in Shopify'),
        ('delivered', 'When delivered'),
        ('manual', 'Manually'),
    ], string='Create invoices', default='paid', required=True)
    required_scope_list = fields.Text(compute='_compute_scope_lists')
    optional_scope_list = fields.Text(compute='_compute_scope_lists')
    result_note = fields.Text(readonly=True)

    def _compute_scope_lists(self):
        """Read the permissions out of the table the connector actually checks."""
        scopes = self.env['eh.shopify.store']._scope_table()
        required = ', '.join(scope for scope, needed, _why in scopes if needed)
        optional = ', '.join(scope for scope, needed, _why in scopes if not needed)
        for wizard in self:
            wizard.required_scope_list = required
            wizard.optional_scope_list = optional

    def _compute_secret_inputs(self):
        for wizard in self:
            wizard.client_secret = False
            wizard.access_token = False
            wizard.webhook_secret = False

    def _seal(self, name):
        vault = self.env['eh.shopify.credential']._vault()
        for wizard in self:
            value = wizard[name]
            if value:
                wizard[name + '_sealed'] = vault.encrypt(value.strip())

    def _inverse_client_secret(self):
        self._seal('client_secret')

    def _inverse_access_token(self):
        self._seal('access_token')

    def _inverse_webhook_secret(self):
        self._seal('webhook_secret')

    def _unsealed(self, name):
        self.ensure_one()
        stored = self[name + '_sealed']
        return self.env['eh.shopify.credential']._vault().decrypt(stored) if stored else None

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if 'warehouse_id' in fields_list and not values.get('warehouse_id'):
            company_id = values.get('company_id') or self.env.company.id
            warehouse = self.env['stock.warehouse'].search([('company_id', '=', company_id)], limit=1)
            values['warehouse_id'] = warehouse.id
        return values

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
                'target': 'new', 'name': _('Connect a Shopify store')}

    def action_connect(self):
        self.ensure_one()
        domain = hmac_util.normalize_shop_domain(self.shop_domain or '')
        if not domain:
            raise UserError(_('Enter the store address as yourstore.myshopify.com, or paste the admin link '
                              'https://admin.shopify.com/store/yourstore.'))
        if self.auth_mode in ('client_credentials', 'authorization_code') and not self.client_id:
            raise UserError(_('Enter the client ID shown in the Shopify Dev Dashboard for your app.'))
        Store = self.env['eh.shopify.store']
        store = self.store_id or Store.with_context(active_test=False).search([('shop_domain', '=', domain)], limit=1)
        if not store:
            elsewhere = Store.sudo().with_context(active_test=False).search([('shop_domain', '=', domain)], limit=1)
            if elsewhere:
                raise UserError(_('%(domain)s is already connected in %(company)s. Ask someone who works in that '
                                  'company to open it, or connect a different store.')
                                % {'domain': domain, 'company': elsewhere.company_id.display_name})
        vals = {'shop_domain': domain, 'company_id': self.company_id.id, 'auth_mode': self.auth_mode,
                'client_id': self.client_id or False, 'api_version': self.api_version, 'active': True,
                'warehouse_id': self.warehouse_id.id or False}
        if store:
            store.write(vals)
        else:
            store = Store.create(dict(vals, name=domain.split('.')[0]))
        cred = store._credential()
        secrets = {}
        for name in ('client_secret', 'access_token', 'webhook_secret'):
            value = self._unsealed(name)
            if value:
                secrets[name] = value
        cred._set_secrets(**secrets)
        self.write({'client_secret_sealed': False, 'access_token_sealed': False, 'webhook_secret_sealed': False,
                    'store_id': store.id})
        if self.auth_mode in ('client_credentials', 'authorization_code') and not cred._has('client_secret'):
            raise UserError(_('Enter the client secret shown in the Shopify Dev Dashboard for your app.'))
        if self.auth_mode == 'legacy_token' and not cred._has('access_token'):
            raise UserError(_('Enter the admin API access token of the existing app.'))
        if self.auth_mode == 'authorization_code' and not cred._has('access_token'):
            return store.action_start_oauth()
        try:
            store.with_context(eh_shopify_inline=True)._check_connection()
            counts = store._sync_locations()
        except errors.ShopifyError as error:
            raise UserError(store._explain(error))
        self.write({'step': 'review', 'result_note': _('%(created)s locations found.') % counts})
        return self._reopen()

    def action_back(self):
        self.ensure_one()
        self.step = 'connect'
        return self._reopen()

    def action_finish(self):
        self.ensure_one()
        store = self.store_id
        if not store:
            raise UserError(_('Connect the store first.'))
        if self.warehouse_id:
            store.warehouse_id = self.warehouse_id
            for location in store.env['eh.shopify.location'].search([('store_id', '=', store.id),
                                                                     ('warehouse_id', '=', False)]):
                location.write({'warehouse_id': self.warehouse_id.id,
                                'location_ids': [(6, 0, self.warehouse_id.lot_stock_id.ids)]})
        store.write({
            'webhooks_enabled': self.register_webhooks,
            'orders_enabled': self.import_orders,
            'order_import_from': self.order_import_from,
            'order_confirm': self.order_confirm,
            'invoice_policy': self.invoice_policy,
        })
        note = False
        if self.register_webhooks:
            try:
                result = store._register_webhooks()
                note = result.get('skipped')
            except errors.ShopifyError as error:
                store._record_failure(error)
                note = store._explain(error)
        if note:
            store.message_post(body=note)
        return {'type': 'ir.actions.act_window', 'res_model': 'eh.shopify.store', 'res_id': store.id,
                'view_mode': 'form', 'target': 'current'}
