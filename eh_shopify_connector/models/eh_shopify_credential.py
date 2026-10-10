# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime
import logging

from odoo import SUPERUSER_ID, _, api, fields, models

from .. import compat
from ..lib import crypto, errors, grants

_logger = logging.getLogger(__name__)

SECRET_FIELDS = ('client_secret', 'access_token', 'refresh_token', 'webhook_secret')

# (database, credential id) -> (token, expires_at). A token refreshed and
# committed in its own transaction is invisible to the transaction that asked
# for it, so the fresh value is kept here until it expires or is replaced.
_TOKEN_CACHE = {}
REFRESH_MARGIN = datetime.timedelta(seconds=120)


def _to_datetime(epoch):
    if not epoch:
        return False
    value = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


class ShopifyCredential(models.Model):
    """Encrypted secrets for one store.

    No group has access rights on this model. Only ``sudo`` helpers read it, so
    tokens never travel to the browser, RPC clients or exports.
    """

    _name = 'eh.shopify.credential'
    _description = 'Shopify Store Credential'
    _rec_name = 'store_id'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True)
    client_secret = fields.Char(copy=False)
    access_token = fields.Char(copy=False)
    refresh_token = fields.Char(copy=False)
    webhook_secret = fields.Char(copy=False)
    token_expires_at = fields.Datetime('Token expires', copy=False)
    refresh_expires_at = fields.Datetime('Refresh token expires', copy=False)
    token_scope = fields.Char(copy=False)
    oauth_state_digest = fields.Char(copy=False)
    oauth_state_expires_at = fields.Datetime('Install link expires', copy=False)
    token_issued_at = fields.Datetime('Token issued', copy=False)

    def init(self):
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_credential_store_uniq "
            "ON eh_shopify_credential (store_id)")

    # ------------------------------------------------------------------ vault
    @api.model
    def _key_source(self):
        return 'server_option' if compat.server_option('eh_shopify_key') else 'database_secret'

    @api.model
    def _vault(self):
        material = compat.server_option('eh_shopify_key') or \
            self.env['ir.config_parameter'].sudo().get_str('database.secret')
        return crypto.Vault(material)

    def _set_secrets(self, **values):
        self.ensure_one()
        vault = self._vault()
        vals = {}
        for name, value in values.items():
            if name not in SECRET_FIELDS:
                raise ValueError('Unknown credential field %s' % name)
            vals[name] = vault.encrypt(value.strip()) if value else False
        if 'access_token' in vals:
            _TOKEN_CACHE.pop(self._cache_key(), None)
            if vals['access_token'] and self.store_id.auth_mode == 'legacy_token':
                vals['token_issued_at'] = fields.Datetime.now()
        if vals:
            self.sudo().write(vals)

    def _cache_key(self):
        return (self.env.cr.dbname, self.id)

    def _secret(self, name):
        self.ensure_one()
        stored = self.sudo()[name]
        if not stored:
            return None
        try:
            return self._vault().decrypt(stored)
        except crypto.VaultError as error:
            # The key that encrypted this changed, usually a restored copy of the database or a new key option.
            raise errors.AuthError(_(
                'The stored credentials of this store cannot be read, because the key that encrypted them has '
                'changed. Enter the credentials again on the store and connect it once more.'),
                code='vault_key_changed') from error

    def _has(self, name):
        return bool(self.sudo()[name])

    def _wipe(self):
        _TOKEN_CACHE.pop(self._cache_key(), None)
        vals = dict.fromkeys(SECRET_FIELDS, False)
        vals.update(token_expires_at=False, refresh_expires_at=False, token_scope=False,
                    oauth_state_digest=False, oauth_state_expires_at=False)
        self.sudo().write(vals)

    def _post_form(self):
        transport = self.store_id._transport()
        return getattr(transport, 'post_form', None) or grants.default_post_form

    # ------------------------------------------------------------------ tokens
    def _webhook_secret(self):
        self.ensure_one()
        if self.store_id.auth_mode == 'legacy_token':
            return self._secret('webhook_secret')
        return self._secret('client_secret')

    def _access_token(self, force_refresh=False):
        self.ensure_one()
        cred = self.sudo()
        store = cred.store_id
        token = cred._secret('access_token')
        if store.auth_mode == 'legacy_token':
            if not token:
                raise errors.AuthError('No admin API access token is stored for this store.', code='missing_token')
            return token
        cached = _TOKEN_CACHE.get(cred._cache_key())
        if cached and not force_refresh and cached[1] and cached[1] > fields.Datetime.now() + REFRESH_MARGIN:
            return cached[0]
        if token and not force_refresh:
            if not cred.token_expires_at:
                return token
            if cred.token_expires_at > fields.Datetime.now() + REFRESH_MARGIN:
                return token
        return cred._refresh_access_token(force=force_refresh)

    def _refresh_access_token(self, force=False):
        self.ensure_one()
        rotates = self.store_id.auth_mode == 'authorization_code'
        cred = self.sudo()
        if rotates and cred.oauth_state_digest and cred.oauth_state_expires_at and \
                cred.oauth_state_expires_at > fields.Datetime.now():
            # Shopify retires every refresh token when a new code is exchanged, so
            # never refresh while a reconnection is under way.
            raise errors.TransientError('The store is being reconnected. Token renewal waits until it finishes.',
                                        retry_after=60)
        if not rotates or compat.in_test_mode(self.env) or not self.id:
            return self._do_refresh(force=force)
        # A rotated refresh token must survive even if the job that asked for
        # it later rolls back, so persist it in its own transaction.
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            other = env['eh.shopify.credential'].browse(self.id)
            token = other._do_refresh(force=force)
            expires = other.token_expires_at
        _TOKEN_CACHE[self._cache_key()] = (token, expires)
        self.invalidate_recordset()
        return token

    def _do_refresh(self, force=False):
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM eh_shopify_credential WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset()
        cred = self.sudo()
        store = cred.store_id
        current = cred._secret('access_token')
        if not force and current and cred.token_expires_at and \
                cred.token_expires_at > fields.Datetime.now() + REFRESH_MARGIN:
            return current
        secret = cred._secret('client_secret')
        if store.auth_mode == 'client_credentials':
            token = grants.client_credentials(store.shop_domain, store.client_id, secret,
                                              post_form=cred._post_form())
        elif store.auth_mode == 'authorization_code':
            if not cred._has('refresh_token'):
                if current and not force:
                    return current
                raise errors.AuthError('The store authorisation expired. Reconnect with the install link.',
                                       code='reconnect_required')
            token = grants.refresh(store.shop_domain, store.client_id, secret, cred._secret('refresh_token'),
                                   post_form=cred._post_form())
        else:
            raise errors.AuthError('This connection mode does not refresh tokens.', code='no_refresh')
        cred._store_token(token)
        return token.access_token

    def _store_token(self, token):
        self.ensure_one()
        vals = {
            'token_scope': token.scope or False,
            'token_expires_at': _to_datetime(token.expires_at),
            'token_issued_at': fields.Datetime.now(),
        }
        _TOKEN_CACHE.pop(self._cache_key(), None)
        if token.refresh_token:
            vals['refresh_expires_at'] = _to_datetime(token.refresh_expires_at)
        self.sudo().write(vals)
        secrets = {'access_token': token.access_token}
        if token.refresh_token:
            secrets['refresh_token'] = token.refresh_token
        self._set_secrets(**secrets)

    # ------------------------------------------------------------------ oauth
    @api.model
    def _consume_oauth_committed(self, credential_id, params):
        """Exchange the one time code in a transaction of its own, so a retried
        request can never lose the tokens of a code Shopify already spent."""
        if compat.in_test_mode(self.env):
            return self.browse(credential_id)._consume_oauth(params)
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            token = env['eh.shopify.credential'].browse(credential_id)._consume_oauth(params)
        self.browse(credential_id).invalidate_recordset()
        return token

    def _begin_oauth(self):
        self.ensure_one()
        state = grants.new_state()
        self.sudo().write({
            'oauth_state_digest': grants.state_digest(state),
            'oauth_state_expires_at': fields.Datetime.now() + datetime.timedelta(minutes=10),
        })
        return state

    def _consume_oauth(self, params):
        """Verify the callback first, then spend the state once. A forged or
        tampered callback changes nothing, so it cannot cancel a real one."""
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM eh_shopify_credential WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset()
        cred = self.sudo()
        store = cred.store_id
        digest = cred.oauth_state_digest
        expires = cred.oauth_state_expires_at
        if not digest or not expires or expires < fields.Datetime.now():
            cred.write({'oauth_state_digest': False, 'oauth_state_expires_at': False})
            raise errors.AuthError('This connection link expired. Start the connection again.', code='state_expired')
        secret = cred._secret('client_secret')
        code = grants.verify_callback(secret, params, store.shop_domain, digest)
        cred.write({'oauth_state_digest': False, 'oauth_state_expires_at': False})
        token = grants.exchange_code(store.shop_domain, store.client_id, secret, code, expiring=True,
                                     post_form=cred._post_form())
        cred._store_token(token)
        return token
