# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime
import logging
import time
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .. import compat
from ..lib import client as gql
from ..lib import documents, errors, grants, hmac_util, testing, util, versions

_logger = logging.getLogger(__name__)
# A failed poll is tried again after this long, because polling catches up by itself.
POLL_RETRY_AFTER = datetime.timedelta(hours=1)

# (scope handle, required from the first release, why the connector needs it)
SCOPES = [
    ('read_locations', True, 'Map Shopify locations to your warehouses.'),
    ('read_products', True, 'Match and import your catalogue.'),
    ('write_products', False, 'Publish product changes from Odoo.'),
    ('read_inventory', True, 'Read stock levels per location.'),
    ('write_inventory', True, 'Send sellable stock from Odoo.'),
    ('read_orders', True, 'Import orders.'),
    ('write_orders', False, 'Cancel orders and write the Odoo reference back.'),
    ('read_customers', True, 'Create and update customers.'),
    ('read_fulfillments', True, 'Import shipments made in Shopify.'),
    ('read_merchant_managed_fulfillment_orders', True, 'See what each location must ship.'),
    ('write_merchant_managed_fulfillment_orders', True, 'Send shipments and tracking from Odoo.'),
    ('read_third_party_fulfillment_orders', False, 'See orders shipped by other apps.'),
    ('write_files', False, 'Send product images from Odoo.'),
    ('read_files', False, 'Reuse product images already sent to Shopify.'),
    ('read_returns', False, 'Bring returns into Odoo as return transfers.'),
    ('read_shopify_payments_payouts', False, 'Reconcile Shopify Payments payouts and fees.'),
    ('read_shopify_payments_accounts', False, 'Read the Shopify Payments balance that payouts come from.'),
    ('read_shopify_payments_disputes', False, 'Follow chargebacks and their evidence deadlines.'),
    ('read_all_orders', False, 'Import orders older than 60 days.'),
    ('read_payment_terms', False, 'Import B2B payment terms on orders.'),
    ('read_gift_cards', False, 'Book balances of gift cards sold before Odoo followed the store.'),
    ('write_order_edits', False, 'Send quantity changes made in Odoo to the Shopify order.'),
]

# Shopify grants these only to an approved app, or on a plan that carries them. Asking for
# one the app does not have makes Shopify refuse the whole install link.
PROTECTED_SCOPES = {
    'read_all_orders',
    'read_gift_cards',
    'read_shopify_payments_payouts',
    'read_shopify_payments_accounts',
    'read_shopify_payments_disputes',
}

# A person is waiting at a screen, so a call made outside a job gives up rather than hang.
USER_BUDGET = 25.0

WEBHOOK_PATH = '/eh_shopify/webhook/%s'
OAUTH_CALLBACK_PATH = '/eh_shopify/oauth/callback'

BASE_TOPICS = [
    'APP_UNINSTALLED',
    'SHOP_UPDATE',
    'LOCATIONS_CREATE',
    'LOCATIONS_UPDATE',
    'LOCATIONS_DELETE',
    'BULK_OPERATIONS_FINISH',
]

ORDER_TOPICS = [
    'ORDERS_CREATE',
    'ORDERS_UPDATED',
    'ORDERS_PAID',
    'ORDERS_CANCELLED',
    'ORDERS_FULFILLED',
    'ORDERS_PARTIALLY_FULFILLED',
    'ORDERS_EDITED',
    'FULFILLMENTS_CREATE',
    'FULFILLMENTS_UPDATE',
    'FULFILLMENT_ORDERS_HOLD_RELEASED',
    'FULFILLMENT_ORDERS_SCHEDULED_FULFILLMENT_ORDER_READY',
    'FULFILLMENT_ORDERS_LINE_ITEMS_PREPARED_FOR_PICKUP',
    'REFUNDS_CREATE',
    'RETURNS_REQUEST',
    'RETURNS_APPROVE',
    'RETURNS_PROCESS',
    'RETURNS_CLOSE',
    'RETURNS_DECLINE',
    'RETURNS_CANCEL',
    'RETURNS_REOPEN',
    'RETURNS_UPDATE',
    'DISPUTES_CREATE',
    'DISPUTES_UPDATE',
]

CUSTOMER_TOPICS = [
    'CUSTOMERS_CREATE',
    'CUSTOMERS_UPDATE',
    'CUSTOMERS_DELETE',
    'CUSTOMERS_MERGE',
    'COMPANIES_CREATE',
    'COMPANIES_UPDATE',
    'COMPANIES_DELETE',
]

PRODUCT_TOPICS = [
    'PRODUCTS_CREATE',
    'PRODUCTS_UPDATE',
    'PRODUCTS_DELETE',
]

INVENTORY_TOPICS = [
    'INVENTORY_LEVELS_UPDATE',
]

# Topics Shopify refuses unless one of these scopes is granted. Asking for one
# anyway would abort the whole subscription run, so they are left out and the
# scheduled checks cover them until the permission is added.
FULFILLMENT_ORDER_SCOPES = ('read_merchant_managed_fulfillment_orders', 'read_assigned_fulfillment_orders',
                            'read_third_party_fulfillment_orders', 'read_marketplace_fulfillment_orders')
# Reading a return needs read_returns, whatever the topic itself would accept.
RETURN_SCOPES = ('read_returns', 'read_marketplace_returns')
TOPIC_SCOPES = {
    'FULFILLMENTS_CREATE': ('read_fulfillments', 'read_marketplace_orders'),
    'FULFILLMENTS_UPDATE': ('read_fulfillments', 'read_marketplace_orders'),
    'FULFILLMENT_ORDERS_HOLD_RELEASED': FULFILLMENT_ORDER_SCOPES,
    'FULFILLMENT_ORDERS_SCHEDULED_FULFILLMENT_ORDER_READY': FULFILLMENT_ORDER_SCOPES,
    'FULFILLMENT_ORDERS_LINE_ITEMS_PREPARED_FOR_PICKUP': FULFILLMENT_ORDER_SCOPES,
    'INVENTORY_LEVELS_UPDATE': ('read_inventory',),
    'REFUNDS_CREATE': ('read_orders', 'read_marketplace_orders'),
    'RETURNS_REQUEST': RETURN_SCOPES,
    'RETURNS_APPROVE': RETURN_SCOPES,
    'RETURNS_PROCESS': RETURN_SCOPES,
    'RETURNS_DECLINE': RETURN_SCOPES,
    'RETURNS_CLOSE': RETURN_SCOPES,
    'RETURNS_CANCEL': RETURN_SCOPES,
    'RETURNS_REOPEN': RETURN_SCOPES,
    'RETURNS_UPDATE': RETURN_SCOPES,
    'DISPUTES_CREATE': ('read_shopify_payments_disputes',),
    'CUSTOMERS_CREATE': ('read_customers',),
    'CUSTOMERS_UPDATE': ('read_customers',),
    'CUSTOMERS_DELETE': ('read_customers',),
    'CUSTOMERS_MERGE': ('read_customers',),
    'COMPANIES_CREATE': ('read_customers',),
    'COMPANIES_UPDATE': ('read_customers',),
    'COMPANIES_DELETE': ('read_customers',),
    'DISPUTES_UPDATE': ('read_shopify_payments_disputes',),
    'PRODUCTS_CREATE': ('read_products',),
    'PRODUCTS_UPDATE': ('read_products',),
    'PRODUCTS_DELETE': ('read_products',),
}

ERROR_HELP = {
    'auth': 'Shopify rejected the connection. Check the credentials or reconnect the store.',
    'scope': 'The Shopify app is missing permissions. Add them to the app in Shopify, release the new '
             'version, approve it on the store, then check the connection again.',
    'shop_unavailable': 'The Shopify store is paused, frozen or unreachable at this address.',
    'throttled': 'Shopify asked us to slow down. Work continues automatically.',
    'transient': 'Shopify or the network was unavailable. Work continues automatically.',
    'query': 'This API version rejected a request. Choose a newer API version or contact support.',
    'version': 'The API version is outside the supported window. Choose a supported version.',
    'user_errors': 'Shopify refused the change.',
}

_TRANSPORTS = {}


class ShopifyStore(models.Model):
    _name = 'eh.shopify.store'
    _description = 'Shopify Store'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, name, id'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, index=True, tracking=True,
                                 default=lambda self: self.env.company)
    shop_domain = fields.Char('Store address', required=True, tracking=True,
                              help='The myshopify.com address of the store, for example yourstore.myshopify.com.')
    store_uuid = fields.Char('Store key', required=True, readonly=True, copy=False, index=True,
                             default=lambda self: uuid.uuid4().hex)
    state = fields.Selection([
        ('draft', 'Not connected'),
        ('connected', 'Connected'),
        ('attention', 'Needs attention'),
        ('disconnected', 'Disconnected'),
    ], default='draft', required=True, tracking=True, copy=False)
    auth_mode = fields.Selection([
        ('client_credentials', 'App in my Shopify organisation'),
        ('authorization_code', 'Install link'),
        ('legacy_token', 'Existing admin API token'),
    ], string='Connection method', required=True, default='client_credentials', tracking=True)
    client_id = fields.Char('Client ID', tracking=True, copy=False)
    api_version = fields.Selection(selection='_selection_api_version', string='API version', required=True,
                                   default=lambda self: versions.DEFAULT, tracking=True)
    warehouse_id = fields.Many2one('stock.warehouse', string='Default warehouse', check_company=True)
    webhooks_enabled = fields.Boolean('Real time updates', default=True,
                                      help='Receive changes from Shopify as they happen. Scheduled checks keep '
                                           'running either way, so nothing is missed if a webhook is lost.')
    request_protected_scopes = fields.Boolean(
        'App is approved for protected data', default=False,
        help='Tick this once Shopify has approved the app for protected customer data, payouts, disputes, '
             'gift cards or orders older than 60 days. Until then the install link leaves those '
             'permissions out, because asking for one the app does not have makes Shopify refuse the '
             'whole request.')
    webhook_base_url = fields.Char('Public address',
                                   help='HTTPS address Shopify uses to reach this Odoo. Leave empty to use the '
                                        'system address.')
    notify_customer_on_fulfillment = fields.Boolean('Email customers when Odoo ships', default=True)

    # Discovered from Shopify
    shop_gid = fields.Char('Shopify shop', readonly=True, copy=False)
    shop_name = fields.Char(readonly=True, copy=False)
    shop_email = fields.Char(readonly=True, copy=False)
    currency_code = fields.Char('Store currency', readonly=True, copy=False)
    presentment_currencies = fields.Char(readonly=True, copy=False)
    taxes_included = fields.Boolean('Prices include tax', readonly=True, copy=False)
    tax_shipping = fields.Boolean('Shipping is taxed', readonly=True, copy=False)
    iana_timezone = fields.Char('Store timezone', readonly=True, copy=False)
    weight_unit = fields.Char(readonly=True, copy=False)
    plan_name = fields.Char('Shopify plan', readonly=True, copy=False)
    is_plus = fields.Boolean('Shopify Plus', readonly=True, copy=False)
    is_development = fields.Boolean('Development store', readonly=True, copy=False)
    granted_scopes = fields.Text(readonly=True, copy=False)
    served_api_version = fields.Char('API version served', readonly=True, copy=False)
    api_deprecations = fields.Text('Deprecation notices', readonly=True, copy=False)
    last_check_at = fields.Datetime('Last checked', readonly=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)
    last_selfheal_at = fields.Datetime('Webhooks last verified', readonly=True, copy=False)
    webhook_note = fields.Char(readonly=True, copy=False)

    # Write only secret inputs, stored encrypted on the credential
    client_secret_input = fields.Char('Client secret', compute='_compute_secret_inputs',
                                      inverse='_inverse_client_secret',
                                      groups='eh_shopify_connector.group_shopify_manager')
    access_token_input = fields.Char('Admin API access token', compute='_compute_secret_inputs',
                                     inverse='_inverse_access_token',
                                     groups='eh_shopify_connector.group_shopify_manager')
    webhook_secret_input = fields.Char('API secret key', compute='_compute_secret_inputs',
                                       inverse='_inverse_webhook_secret',
                                       groups='eh_shopify_connector.group_shopify_manager',
                                       help='Signs webhooks for an existing admin API token app. Not needed for '
                                            'the other connection methods.')
    has_client_secret = fields.Boolean(compute='_compute_credential_state')
    has_access_token = fields.Boolean(compute='_compute_credential_state')
    has_webhook_secret = fields.Boolean(compute='_compute_credential_state')
    token_expires_at = fields.Datetime('Token renews at', compute='_compute_credential_state')
    vault_key_source = fields.Selection([
        ('server_option', 'Server key'),
        ('database_secret', 'Database secret'),
    ], compute='_compute_credential_state', string='Encryption key')

    missing_required_scopes = fields.Text(compute='_compute_scopes')
    missing_optional_scopes = fields.Text(compute='_compute_scopes')
    api_version_state = fields.Selection([
        ('ok', 'Supported'),
        ('warning', 'Ends soon'),
        ('expired', 'No longer supported'),
        ('below_floor', 'Too old for this connector'),
        ('unreleased', 'Not released yet'),
        ('unknown', 'Unknown'),
    ], string='API version status', compute='_compute_api_version_state')
    api_version_days_left = fields.Integer('Days of API support left', compute='_compute_api_version_state')
    webhook_url = fields.Char('Webhook address', compute='_compute_webhook_url')
    health = fields.Selection([
        ('ok', 'Healthy'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('off', 'Not connected'),
    ], compute='_compute_health')
    health_notes = fields.Text(compute='_compute_health')
    job_pending_count = fields.Integer(compute='_compute_counts')
    job_attention_count = fields.Integer(compute='_compute_counts')
    location_count = fields.Integer(compute='_compute_counts')
    event_count_24h = fields.Integer('Webhooks in 24 hours', compute='_compute_counts')
    last_webhook_at = fields.Datetime('Last webhook', compute='_compute_counts')

    def init(self):
        cr = self.env.cr
        cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_store_domain_uniq "
                   "ON eh_shopify_store (lower(shop_domain)) WHERE active")
        cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_store_uuid_uniq ON eh_shopify_store (store_uuid)")

    # ------------------------------------------------------------------ selection and computes
    @api.model
    def _scope_table(self):
        """The permissions this connector asks for, so screens never drift from the code."""
        return list(SCOPES)

    @api.model
    def _selection_api_version(self):
        handles = sorted((v for v in versions.VERSIONS if versions._key(v) >= versions._key(versions.FLOOR)),
                         key=versions._key)
        return [(v, v) for v in handles]

    def _compute_secret_inputs(self):
        for store in self:
            store.client_secret_input = False
            store.access_token_input = False
            store.webhook_secret_input = False

    def _inverse_client_secret(self):
        for store in self.filtered('client_secret_input'):
            store._credential()._set_secrets(client_secret=store.client_secret_input)

    def _inverse_access_token(self):
        for store in self.filtered('access_token_input'):
            store._credential()._set_secrets(access_token=store.access_token_input)

    def _inverse_webhook_secret(self):
        for store in self.filtered('webhook_secret_input'):
            store._credential()._set_secrets(webhook_secret=store.webhook_secret_input)

    def _compute_credential_state(self):
        Credential = self.env['eh.shopify.credential'].sudo()
        source = Credential._key_source()
        for store in self:
            cred = Credential.search([('store_id', '=', store.id)], limit=1) if store.id else Credential
            store.has_client_secret = bool(cred and cred.client_secret)
            store.has_access_token = bool(cred and cred.access_token)
            store.has_webhook_secret = bool(cred and cred.webhook_secret)
            store.token_expires_at = cred.token_expires_at if cred else False
            store.vault_key_source = source

    @api.depends('granted_scopes')
    def _compute_scopes(self):
        for store in self:
            granted = set((store.granted_scopes or '').split(','))
            granted |= {'read_' + s[len('write_'):] for s in granted if s.startswith('write_')}
            store.missing_required_scopes = ', '.join(s for s, req, _why in SCOPES if req and s not in granted)
            store.missing_optional_scopes = ', '.join(s for s, req, _why in SCOPES if not req and s not in granted)

    @api.depends('api_version')
    def _compute_api_version_state(self):
        for store in self:
            state, days = versions.status(store.api_version) if store.api_version else ('unknown', None)
            store.api_version_state = state
            store.api_version_days_left = days or 0

    @api.depends('store_uuid', 'webhook_base_url')
    def _compute_webhook_url(self):
        for store in self:
            base = (store.webhook_base_url or compat.base_url(self.env) or '').rstrip('/')
            store.webhook_url = base + WEBHOOK_PATH % store.store_uuid if base and store.store_uuid else False

    def _compute_counts(self):
        Job = self.env['eh.shopify.job'].sudo()
        Event = self.env['eh.shopify.event'].sudo()
        Location = self.env['eh.shopify.location'].sudo()
        since = fields.Datetime.now() - datetime.timedelta(hours=24)
        for store in self:
            if not store.id:
                store.job_pending_count = store.job_attention_count = store.location_count = 0
                store.event_count_24h = 0
                store.last_webhook_at = False
                continue
            store.job_pending_count = Job.search_count([('store_id', '=', store.id),
                                                        ('state', 'in', ('pending', 'running'))])
            store.job_attention_count = Job.search_count([('store_id', '=', store.id),
                                                          ('state', 'in', ('failed', 'dead'))])
            store.location_count = Location.search_count([('store_id', '=', store.id)])
            store.event_count_24h = Event.search_count([('store_id', '=', store.id), ('received_at', '>=', since)])
            last = Event.search([('store_id', '=', store.id)], order='id desc', limit=1)
            store.last_webhook_at = last.received_at if last else False

    def _features_switched_off(self):
        """What the connector cannot do, because a permission was not granted.

        Optional permissions are genuinely optional, so a store without them is not
        unhealthy. It must still be said out loud: a silent feature is read as a
        broken one.
        """
        self.ensure_one()
        missing = set((self.missing_optional_scopes or '').split(', ')) - {''}
        return [why.rstrip('.') for scope, required, why in SCOPES
                if not required and scope in missing]

    def _waiting_work(self):
        """How much queued work cannot run, because the store is not connected."""
        self.ensure_one()
        if not self.id:
            return 0
        jobs = self.env['eh.shopify.job'].sudo().search_count(
            [('store_id', '=', self.id), ('state', 'in', ('pending', 'running'))])
        events = self.env['eh.shopify.event'].sudo().search_count(
            [('store_id', '=', self.id), ('state', '=', 'queued')])
        return jobs + events

    @api.depends('state', 'last_error', 'granted_scopes', 'api_version', 'webhooks_enabled', 'webhook_note')
    def _compute_health(self):
        for store in self:
            notes = []
            level = 'ok'
            if store.state in ('draft', 'disconnected'):
                store.health = 'off'
                lines = [_('Connect the store to start syncing.')]
                waiting = store._waiting_work()
                if waiting:
                    lines.append(_('%s pieces of sync work are waiting. Nothing runs while the store is not '
                                   'connected: connect it again to let the work through, or cancel the work '
                                   'in Sync Jobs.') % waiting)
                store.health_notes = '\n'.join(lines)
                continue
            if store.last_error:
                level = 'error'
                notes.append(store.last_error)
            if store.missing_required_scopes:
                level = 'error'
                notes.append(_('Missing permissions: %s') % store.missing_required_scopes)
            if store.api_version_state in ('expired', 'below_floor', 'unknown'):
                level = 'error'
                notes.append(_('API version %s is not supported. Choose a newer version.') % store.api_version)
            elif store.api_version_state == 'warning':
                level = 'warning' if level == 'ok' else level
                notes.append(_('API version %s support ends in %s days.') % (store.api_version,
                                                                             store.api_version_days_left))
            if store.served_api_version and store.served_api_version != store.api_version:
                level = 'warning' if level == 'ok' else level
                notes.append(_('Shopify answered with API version %s instead of %s.') % (
                    store.served_api_version, store.api_version))
            if store.webhooks_enabled and store.webhook_note:
                level = 'warning' if level == 'ok' else level
                notes.append(store.webhook_note)
            off = store._features_switched_off()
            if off:
                notes.append(_('These features are off, because the app was not given the permission they '
                               'need: %s. Add the permission in Shopify, then press Connect again.')
                             % '; '.join(off))
            if store.vault_key_source == 'database_secret':
                notes.append(_('Credentials are encrypted with the database secret. Set the eh_shopify_key server '
                               'option for a key kept outside the database.'))
            store.health = level
            store.health_notes = '\n'.join(notes) or _('Everything is working.')

    # ------------------------------------------------------------------ create and write
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('shop_domain'):
                vals['shop_domain'] = self._normalise_domain(vals['shop_domain'])
            if not vals.get('name') and vals.get('shop_domain'):
                vals['name'] = vals['shop_domain'].split('.')[0]
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('shop_domain'):
            vals['shop_domain'] = self._normalise_domain(vals['shop_domain'])
        result = super().write(vals)
        if vals.get('active') is False:
            for store in self:
                store.sudo()._stop_pending_work()
        return result

    # What a store keeps. Deleting the store would delete these links, and the next
    # import would then create every order, product and contact a second time.
    LINKED_RECORDS = [
        ('eh.shopify.order', 'sale_order_id'),
        ('eh.shopify.product', 'product_tmpl_id'),
        ('eh.shopify.customer', 'partner_id'),
    ]

    def unlink(self):
        labels = {
            'eh.shopify.order': _('orders linked to sales orders'),
            'eh.shopify.product': _('products linked to Odoo products'),
            'eh.shopify.customer': _('customers linked to Odoo contacts'),
        }
        for store in self:
            kept = []
            for model, link_field in self.LINKED_RECORDS:
                count = self.env[model].sudo().search_count(
                    [('store_id', '=', store.id), (link_field, '!=', False)])
                if count:
                    kept.append('%s %s' % (count, labels[model]))
            if kept:
                raise UserError(_(
                    'This store cannot be deleted, because it still holds %(kept)s. Deleting it would '
                    'remove those links, and the next import would create everything a second time. '
                    'Use Disconnect to erase the credentials and stop the sync, or archive the store to '
                    'hide it while keeping the links.',
                    kept=' and '.join(kept)))
        return super().unlink()

    @api.model
    def _normalise_domain(self, value):
        domain = hmac_util.normalize_shop_domain(value)
        if not domain:
            raise ValidationError(_(
                'Enter the store address as yourstore.myshopify.com, or paste the admin link '
                'https://admin.shopify.com/store/yourstore. A custom storefront domain cannot be used.'))
        return domain

    @api.constrains('webhook_base_url')
    def _check_webhook_base_url(self):
        for store in self:
            if store.webhook_base_url and not store.webhook_base_url.startswith('https://'):
                raise ValidationError(_('The public address must start with https://'))

    # ------------------------------------------------------------------ plumbing
    def _credential(self):
        self.ensure_one()
        Credential = self.env['eh.shopify.credential'].sudo()
        cred = Credential.search([('store_id', '=', self.id)], limit=1)
        return cred or Credential.create({'store_id': self.id})

    def _transport(self):
        self.ensure_one()
        override = testing.TRANSPORT_OVERRIDES.get(self.shop_domain)
        if override is not None:
            return override
        transport = _TRANSPORTS.get(self.shop_domain)
        if transport is None:
            transport = _TRANSPORTS[self.shop_domain] = gql.RequestsTransport()
        return transport

    def _client(self):
        # A job sets its own budget. Anything else is someone waiting at a screen, so the call gets a short one.
        self.ensure_one()
        cred = self._credential()

        def provider(force_refresh=False):
            return cred._access_token(force_refresh=force_refresh)

        transport = self._transport()
        return gql.GraphQLClient(self.shop_domain, provider, api_version=self.api_version, transport=transport,
                                 sleep=getattr(transport, 'fake_sleep', time.sleep),
                                 deadline=self.env.context.get('eh_shopify_deadline')
                                 or time.monotonic() + USER_BUDGET)

    def _granted_scope_set(self):
        self.ensure_one()
        return {s for s in (self.granted_scopes or '').split(',') if s}

    def _doc_text(self, document):
        """Document text for this store's API version, without fields its
        app has no permission for (Shopify rejects the whole query otherwise)."""
        self.ensure_one()
        granted = self._granted_scope_set() if self.granted_scopes else None
        return document.text_for(self.api_version, granted)

    def _explain(self, error):
        base = ERROR_HELP.get(getattr(error, 'kind', ''), '')
        detail = getattr(error, 'message', None) or str(error)
        if isinstance(error, errors.ScopeError) and error.missing_scopes:
            detail = _('Missing: %s') % ', '.join(error.missing_scopes)
        return ('%s %s' % (base, detail)).strip()

    def _record_failure(self, error):
        for store in self:
            vals = {'last_error': store._explain(error)[:2000], 'last_check_at': fields.Datetime.now()}
            if isinstance(error, (errors.AuthError, errors.ScopeError, errors.VersionError,
                                  errors.ShopUnavailable)) and store.state != 'draft':
                vals['state'] = 'attention'
            store.sudo().write(vals)

    @staticmethod
    def _notify(title, message, kind='success', sticky=False):
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': title, 'message': message, 'type': kind, 'sticky': sticky}}

    # ------------------------------------------------------------------ connection
    def _check_connection(self):
        self.ensure_one()
        client = self._client()
        shop = (client.execute(documents.SHOP_INFO.text_for(self.api_version)).data or {}).get('shop') or {}
        domain = (shop.get('myshopifyDomain') or '').lower()
        if domain and domain != self.shop_domain:
            raise errors.AuthError(_('These credentials belong to %s, not %s.') % (domain, self.shop_domain),
                                   code='shop_mismatch')
        installation = (client.execute(documents.APP_INSTALLATION.text_for(self.api_version)).data or {}).get(
            'currentAppInstallation') or {}
        scopes = sorted({s.get('handle') for s in installation.get('accessScopes') or [] if s.get('handle')})
        plan = shop.get('plan') or {}
        vals = {
            'shop_gid': shop.get('id'),
            'shop_name': shop.get('name'),
            'shop_email': shop.get('email'),
            'currency_code': shop.get('currencyCode'),
            'presentment_currencies': ','.join(shop.get('enabledPresentmentCurrencies') or []),
            'taxes_included': bool(shop.get('taxesIncluded')),
            'tax_shipping': bool(shop.get('taxShipping')),
            'iana_timezone': shop.get('ianaTimezone'),
            'weight_unit': shop.get('weightUnit'),
            'plan_name': plan.get('publicDisplayName'),
            'is_plus': bool(plan.get('shopifyPlus')),
            'is_development': bool(plan.get('partnerDevelopment')),
            'granted_scopes': ','.join(scopes),
            'served_api_version': client.served_version,
            'api_deprecations': '\n'.join(sorted(client.deprecations)) or False,
            'last_check_at': fields.Datetime.now(),
            'last_error': False,
        }
        self.sudo().write(vals)
        self.sudo().write({'state': 'attention' if self.missing_required_scopes else 'connected'})
        return vals

    def action_check_connection(self):
        self.ensure_one()
        try:
            self._check_connection()
        except errors.ShopifyError as error:
            self._record_failure(error)
            return self._notify(_('Connection failed'), self._explain(error), 'danger', sticky=True)
        if self.missing_required_scopes:
            return self._notify(_('Connected with missing permissions'),
                                _('Add these permissions to the app: %s') % self.missing_required_scopes,
                                'warning', sticky=True)
        return self._notify(_('Connected'), _('%s is connected.') % (self.shop_name or self.shop_domain))

    def _oauth_redirect_uri(self):
        """Where Shopify sends the merchant back.

        The store's own public address wins, because an Odoo behind a proxy or a
        tunnel often does not know the address the outside world uses. Shopify only
        accepts https, so an http address is refused here rather than at Shopify.
        """
        base = (self.webhook_base_url or compat.base_url(self.env) or '').strip().rstrip('/')
        if not base:
            raise UserError(_('This Odoo does not know its own public address. Fill in Public address on the '
                              'store, or set the web base URL in the general settings.'))
        if not base.lower().startswith('https://'):
            raise UserError(_('Shopify only sends merchants back to an https address, and this one is %s. '
                              'Fill in the https address of this Odoo in Public address on the store.') % base)
        return base + OAUTH_CALLBACK_PATH

    def _requested_scopes(self):
        """The permissions the install link asks Shopify for.

        Shopify refuses the whole request when it names a permission the app has not
        been granted, and the permissions in PROTECTED_SCOPES are granted only after
        Shopify approves the app or the store is on a plan that carries them. They
        are therefore left out unless the merchant says the app has them.
        """
        self.ensure_one()
        return [scope for scope, _required, _why in SCOPES
                if self.request_protected_scopes or scope not in PROTECTED_SCOPES]

    def _oauth_authorize_url(self):
        self.ensure_one()
        cred = self._credential()
        if not (self.client_id and cred._has('client_secret')):
            raise UserError(_('Enter the client ID and client secret of the app first.'))
        state = cred._begin_oauth()
        return grants.authorize_url(self.shop_domain, self.client_id, self._requested_scopes(),
                                    self._oauth_redirect_uri(), state)

    def action_start_oauth(self):
        self.ensure_one()
        url = self._oauth_authorize_url()
        digest = self._credential().sudo().oauth_state_digest
        try:
            from odoo.http import request as http_request
            if http_request:
                pending = dict(http_request.session.get('eh_shopify_oauth') or {})
                pending[str(self.id)] = digest
                http_request.session['eh_shopify_oauth'] = pending
        except RuntimeError:
            pass
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'self'}

    def _stop_pending_work(self):
        """Close work that can no longer run, instead of leaving it queued for ever.

        The job runner only claims work for a store that is active and connected, so
        work left behind by a store that stops being either is invisible and immovable:
        the waiting counter keeps counting it and Retry does nothing.
        """
        self.ensure_one()
        self.env['eh.shopify.job'].sudo().search([
            ('store_id', '=', self.id), ('state', '=', 'pending')])._close_cancelled()

    def action_disconnect(self):
        for store in self:
            store._unregister_webhooks_best_effort()
            store._credential()._wipe()
            store._stop_pending_work()
            store.write({'state': 'disconnected', 'last_error': False})
            store.message_post(body=_('Store disconnected and credentials erased.'))
        return True

    def _on_app_uninstalled(self):
        for store in self:
            store._credential()._wipe()
            store.sudo()._stop_pending_work()
            store.sudo().write({'state': 'disconnected',
                                'last_error': _('The app was uninstalled from the Shopify store.')})
            store.message_post(body=_('Shopify reported that the app was uninstalled. Credentials erased.'))

    # ------------------------------------------------------------------ locations
    def _sync_locations(self):
        self.ensure_one()
        Location = self.env['eh.shopify.location'].sudo()
        existing = {loc.shopify_gid: loc for loc in Location.with_context(active_test=False).search(
            [('store_id', '=', self.id)])}
        client = self._client()
        doc = documents.LOCATIONS
        seen = set()
        counts = {'created': 0, 'updated': 0, 'unchanged': 0, 'removed': 0}
        for node in client.paginate(doc.text_for(self.api_version), {}, doc.connection, page_size=100):
            gid = node['id']
            seen.add(gid)
            digest = util.fingerprint(node)
            binding = existing.get(gid)
            vals = Location._vals_from_node(self, node)
            vals['fingerprint'] = digest
            if binding:
                if binding.fingerprint == digest and not binding.removed_in_shopify:
                    counts['unchanged'] += 1
                    continue
                vals['removed_in_shopify'] = False
                binding.write(vals)
                counts['updated'] += 1
            else:
                vals.update(Location._defaults_for_new(self, node))
                Location.create(vals)
                counts['created'] += 1
        for gid, binding in existing.items():
            if gid not in seen and not binding.removed_in_shopify:
                binding.write({'removed_in_shopify': True})
                counts['removed'] += 1
        return counts

    def action_sync_locations(self):
        self.ensure_one()
        try:
            counts = self._sync_locations()
        except errors.ShopifyError as error:
            self._record_failure(error)
            return self._notify(_('Location sync failed'), self._explain(error), 'danger', sticky=True)
        return self._notify(_('Locations synced'), _('%(created)s new, %(updated)s updated, %(removed)s removed.')
                            % counts)

    # ------------------------------------------------------------------ webhooks
    def _redact_store(self):
        """Erase what Odoo stores of this Shopify shop: deliveries, job payloads
        and the credentials. The orders, products and accounting the merchant
        made from them stay, because they are the merchant's own records."""
        self.ensure_one()
        self._on_app_uninstalled()
        Event = self.env['eh.shopify.event'].sudo()
        Job = self.env['eh.shopify.job'].sudo()
        Event.search([('store_id', '=', self.id)]).write({'payload': {'handled': 'shop/redact'}})
        Job.search([('store_id', '=', self.id)]).filtered('payload').write({'payload': {'handled': 'shop/redact'}})
        credential = self._credential().sudo()
        if credential:
            credential.unlink()
        self.sudo().write({'state': 'disconnected', 'webhook_note': _(
            'Shopify asked for the data of this shop to be erased after the app was uninstalled. The stored '
            'deliveries and the credentials were removed. Connect again to use the store.')})
        return {'redacted': self.shop_domain}

    def _webhook_topics(self):
        topics = list(BASE_TOPICS)
        if self.orders_enabled:
            topics += ORDER_TOPICS
        if self.inventory_enabled:
            topics += INVENTORY_TOPICS
        if self.products_enabled:
            topics += PRODUCT_TOPICS
        if self.orders_enabled:
            # Last, so a shop that refuses the B2B company topics still registers stock and product topics.
            topics += CUSTOMER_TOPICS
        if self.granted_scopes:
            granted = self._granted_scope_set()
            topics = [topic for topic in topics if not TOPIC_SCOPES.get(topic) or any(
                documents.scope_granted(scope, granted) for scope in TOPIC_SCOPES[topic])]
        return topics

    def _register_webhooks(self):
        self.ensure_one()
        Webhook = self.env['eh.shopify.webhook'].sudo()
        url = self.webhook_url or ''
        if not self.webhooks_enabled:
            return {'skipped': _('Real time updates are switched off.')}
        if not url.startswith('https://'):
            note = _('Real time updates need a public HTTPS address. Scheduled checks keep the store in sync '
                     'meanwhile.')
            self.sudo().write({'webhook_note': note})
            return {'skipped': note}
        if not self._credential()._webhook_secret():
            note = _('Real time updates need the API secret key of the app to verify signatures. Scheduled checks '
                     'keep the store in sync meanwhile.')
            self.sudo().write({'webhook_note': note})
            return {'skipped': note}
        client = self._client()
        list_doc = documents.WEBHOOK_SUBSCRIPTIONS
        remote = list(client.paginate(list_doc.text_for(self.api_version), {}, list_doc.connection, page_size=100))
        prefix = url.rsplit('/', 1)[0] + '/'
        ours = {w['topic']: w for w in remote if w.get('uri') == url}
        wanted = self._webhook_topics()
        created = removed = 0
        for topic in wanted:
            sub = ours.get(topic)
            if not sub:
                payload = client.mutate(documents.WEBHOOK_CREATE.text_for(self.api_version),
                                        {'topic': topic, 'subscription': {'uri': url, 'format': 'JSON'}},
                                        documents.WEBHOOK_CREATE.payload_key)
                sub = payload.get('webhookSubscription') or {}
                created += 1
            record = Webhook.search([('store_id', '=', self.id), ('topic', '=', topic)], limit=1)
            vals = {'store_id': self.id, 'topic': topic, 'subscription_gid': sub.get('id'), 'uri': url,
                    'api_version': (sub.get('apiVersion') or {}).get('handle'), 'state': 'active',
                    'last_verified_at': fields.Datetime.now()}
            if record:
                record.write(vals)
            else:
                Webhook.create(vals)
        for sub in remote:
            uri = sub.get('uri') or ''
            if uri.startswith(prefix) and (uri != url or sub.get('topic') not in wanted):
                client.mutate(documents.WEBHOOK_DELETE.text_for(self.api_version), {'id': sub['id']},
                              documents.WEBHOOK_DELETE.payload_key)
                removed += 1
        Webhook.search([('store_id', '=', self.id), ('topic', 'not in', wanted)]).write({'state': 'removed'})
        self.sudo().write({'webhook_note': False, 'last_selfheal_at': fields.Datetime.now()})
        return {'created': created, 'removed': removed, 'active': len(wanted)}

    def action_register_webhooks(self):
        self.ensure_one()
        try:
            result = self._register_webhooks()
        except errors.ShopifyError as error:
            self._record_failure(error)
            return self._notify(_('Webhook setup failed'), self._explain(error), 'danger', sticky=True)
        if result.get('skipped'):
            return self._notify(_('Real time updates not active'), result['skipped'], 'warning', sticky=True)
        return self._notify(_('Real time updates active'), _('%(active)s topics subscribed, %(created)s new.')
                            % result)

    def _unregister_webhooks_best_effort(self):
        for store in self:
            if store.state not in ('connected', 'attention'):
                continue
            try:
                client = store._client()
                client.timeout = (5, 10)
                client.max_retries = 0
                for record in self.env['eh.shopify.webhook'].sudo().search([
                        ('store_id', '=', store.id), ('subscription_gid', '!=', False)]):
                    client.mutate(documents.WEBHOOK_DELETE.text_for(store.api_version),
                                  {'id': record.subscription_gid}, documents.WEBHOOK_DELETE.payload_key)
            except Exception:
                _logger.info('Could not remove Shopify webhooks for store %s; Shopify removes them after '
                             'repeated delivery failures.', store.id, exc_info=True)

    # ------------------------------------------------------------------ job handlers
    def _job_store_check(self, job):
        return job.store_id._check_connection() and {'checked': True}

    def _job_locations_sync(self, job):
        return job.store_id._sync_locations()

    def _job_webhooks_register(self, job):
        return job.store_id._register_webhooks()

    def _job_token_refresh(self, job):
        job.store_id._credential()._access_token(force_refresh=True)
        return {'refreshed': True}

    # ------------------------------------------------------------------ smart buttons
    def _open(self, model, name, domain, views='list,form', context=None):
        return {'type': 'ir.actions.act_window', 'name': name, 'res_model': model, 'view_mode': views,
                'domain': domain, 'context': dict(context or {}, default_store_id=self.id)}

    def action_open_locations(self):
        self.ensure_one()
        return self._open('eh.shopify.location', _('Locations'), [('store_id', '=', self.id)])

    def action_open_jobs(self):
        self.ensure_one()
        return self._open('eh.shopify.job', _('Sync jobs'), [('store_id', '=', self.id)])

    def action_open_attention(self):
        self.ensure_one()
        return self._open('eh.shopify.job', _('Exceptions'),
                          [('store_id', '=', self.id), ('state', 'in', ('failed', 'dead'))])

    def action_open_events(self):
        self.ensure_one()
        return self._open('eh.shopify.event', _('Webhook deliveries'), [('store_id', '=', self.id)])

    def action_open_setup(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'eh.shopify.setup.wizard', 'view_mode': 'form',
                'target': 'new', 'context': {'default_store_id': self.id, 'default_shop_domain': self.shop_domain,
                                             'default_auth_mode': self.auth_mode,
                                             'default_client_id': self.client_id,
                                             'default_company_id': self.company_id.id}}

    # ------------------------------------------------------------------ crons
    @api.model
    def _cron_maintenance(self):
        Job = self.env['eh.shopify.job'].sudo()
        Job._reset_stale()
        now = fields.Datetime.now()
        stores = self.sudo().search([('state', 'in', ('connected', 'attention'))])

        def queue(store, job_type, name, priority, retry_after=None):
            if Job._blocked_by_failure(store, job_type, retry_after=retry_after):
                return
            Job._enqueue(store, job_type, name=name, dedup_key=job_type, priority=priority)

        for store in stores:
            if store.auth_mode != 'legacy_token' and store.token_expires_at and \
                    store.token_expires_at < now + datetime.timedelta(minutes=30):
                queue(store, 'token.refresh', _('Renew access token'), 1)
            if store.webhooks_enabled and (not store.last_selfheal_at or
                                           store.last_selfheal_at < now - datetime.timedelta(hours=1)):
                queue(store, 'webhooks.register', _('Verify real time updates'), 5)
            if store.orders_enabled and (not store.orders_polled_until or
                                         store.orders_polled_until < now - datetime.timedelta(minutes=15)):
                queue(store, 'orders.poll', _('Check for missed orders'), 8, retry_after=POLL_RETRY_AFTER)
            if store.products_enabled and (not store.products_polled_until or
                                           store.products_polled_until < now - datetime.timedelta(minutes=15)):
                queue(store, 'products.poll', _('Check for changed products'), 8, retry_after=POLL_RETRY_AFTER)
            self.env['eh.shopify.bulk'].sudo()._finish_idle_imports(store)
            if store.products_enabled and (not store.products_swept_at or
                                           store.products_swept_at < now - datetime.timedelta(hours=24)):
                self.env['eh.shopify.bulk'].sudo()._start_sweep(store)
            if store.products_enabled and store.master_price == 'odoo' and store.product_pricelist_id and (
                    not store.prices_checked_at or store.prices_checked_at < now - datetime.timedelta(hours=24)):
                store._queue_price_check()
            if store.payouts_enabled and (not store.payouts_polled_at or
                                          store.payouts_polled_at < now - datetime.timedelta(hours=6)):
                queue(store, 'payouts.poll', _('Check Shopify Payments payouts'), 8, retry_after=POLL_RETRY_AFTER)
            if store._reads_disputes() and (not store.disputes_polled_at or
                                            store.disputes_polled_at < now - datetime.timedelta(hours=6)):
                queue(store, 'disputes.poll', _('Check Shopify Payments disputes'), 8, retry_after=POLL_RETRY_AFTER)
            store.price_list_ids.filtered(
                lambda price_list: price_list.pricelist_id and not price_list.removed_in_shopify
                and (not price_list.sent_at or price_list.sent_at < now - datetime.timedelta(hours=24)))._enqueue_push()
            if not store.last_check_at or store.last_check_at < now - datetime.timedelta(hours=6):
                Job._enqueue(store, 'store.check', name=_('Check connection'), dedup_key='store.check', priority=5)
            if store.inventory_enabled and (not store.inventory_reconciled_at or
                                            store.inventory_reconciled_at < now - datetime.timedelta(hours=24)):
                store._mark_all_inventory_dirty()
        return len(stores)
