# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime
import json
import logging
import re
import time

from odoo import fields, http
from odoo.http import request

from .. import compat
from ..lib import errors, grants, hmac_util

_logger = logging.getLogger(__name__)
_UUID_RE = re.compile(r'^[0-9a-f]{32}$')

# Shopify webhook bodies are small; large order payloads stay well below this.
MAX_BODY_BYTES = 5 * 1024 * 1024
_WARN_EVERY_SECONDS = 60.0
_last_warning = {}
_CALLBACK_SIGNATURE_CODES = {'bad_hmac', 'bad_state', 'shop_mismatch', 'bad_shop', 'stale_callback', 'missing_code'}


def _warn_rate_limited(key, message, *args):
    now = time.monotonic()
    if now - _last_warning.get(key, 0.0) >= _WARN_EVERY_SECONDS:
        _last_warning[key] = now
        _logger.warning(message, *args)


class ShopifyController(http.Controller):

    @http.route('/eh_shopify/webhook/<string:store_uuid>', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False)
    def eh_shopify_webhook(self, store_uuid, **_kwargs):
        """Check size, verify, persist, acknowledge. Processing happens in the
        job engine so Shopify gets its answer well inside the five second limit."""
        if not _UUID_RE.match(store_uuid or ''):
            return self._reply(404, 'unknown store')
        store = request.env['eh.shopify.store'].sudo().search([('store_uuid', '=', store_uuid)], limit=1)
        if not store:
            return self._reply(404, 'unknown store')
        httprequest = request.httprequest
        length = httprequest.content_length
        if length is None:
            return self._reply(411, 'length required')
        if length > MAX_BODY_BYTES:
            return self._reply(413, 'payload too large')
        if (httprequest.mimetype or '').lower() != 'application/json':
            return self._reply(415, 'json required')
        raw = httprequest.get_data()
        if len(raw) > MAX_BODY_BYTES:
            return self._reply(413, 'payload too large')
        headers = httprequest.headers
        try:
            secret = store._credential()._webhook_secret()
        except errors.AuthError as error:
            _warn_rate_limited(('vault', store.id), 'Shopify webhooks for store %s cannot be checked: %s',
                               store.id, error.message)
            store.sudo().webhook_note = error.message
            return self._reply(401, 'credentials unavailable')
        if not hmac_util.verify_webhook(secret, raw, headers.get('X-Shopify-Hmac-Sha256')):
            _warn_rate_limited(('hmac', store.id), 'Rejected Shopify webhooks with an invalid signature for store %s.',
                               store.id)
            return self._reply(401, 'invalid signature')
        if (headers.get('X-Shopify-Shop-Domain') or '').strip().lower() != store.shop_domain:
            _warn_rate_limited(('shop', store.id), 'Rejected Shopify webhooks for another shop domain on store %s.',
                               store.id)
            return self._reply(401, 'wrong shop')
        if not headers.get('X-Shopify-Webhook-Id') or not headers.get('X-Shopify-Topic'):
            return self._reply(400, 'missing headers')
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except ValueError:
            return self._reply(400, 'invalid json')
        _event, created = request.env['eh.shopify.event'].sudo()._receive(store, headers, payload)
        return self._reply(200, 'ok' if created else 'duplicate')

    @http.route('/eh_shopify/oauth/callback', type='http', auth='user', methods=['GET'])
    def eh_shopify_oauth_callback(self, **params):
        # The rights are checked before anything is read, so a user without them reads the
        # sentence written for them instead of an access error page.
        if not request.env.user.has_group('eh_shopify_connector.group_shopify_manager'):
            return self._reply(403, 'Only a Shopify manager can connect a store.')
        shop = hmac_util.normalize_shop_domain(params.get('shop') or '')
        store = request.env['eh.shopify.store'].sudo().search([
            ('shop_domain', '=', shop), ('auth_mode', '=', 'authorization_code')], limit=1) if shop else None
        if not store:
            return self._reply(404, 'No store is waiting for this connection.')
        pending = dict(request.session.get('eh_shopify_oauth') or {})
        expected = pending.get(str(store.id))
        if not expected or expected != grants.state_digest(params.get('state') or ''):
            return self._reply(400, 'This connection was not started from this browser session. '
                                    'Start it again from Odoo.')
        credential = store.sudo()._credential()
        try:
            request.env['eh.shopify.credential'].sudo()._consume_oauth_committed(credential.id, dict(params))
        except errors.AuthError as error:
            issued = credential.sudo().token_issued_at
            recently = issued and issued > fields.Datetime.now() - datetime.timedelta(minutes=2)
            if error.code == 'state_expired' and recently:
                return request.redirect(compat.record_url('eh.shopify.store', store.id))
            if error.code in _CALLBACK_SIGNATURE_CODES:
                return self._reply(400, 'The connection response could not be verified. Start it again from Odoo.')
            store.sudo()._record_failure(error)
            return request.redirect(compat.record_url('eh.shopify.store', store.id))
        except errors.ShopifyError as error:
            store.sudo()._record_failure(error)
            return request.redirect(compat.record_url('eh.shopify.store', store.id))
        pending.pop(str(store.id), None)
        request.session['eh_shopify_oauth'] = pending
        try:
            store.sudo()._check_connection()
        except errors.ShopifyError as error:
            store.sudo()._record_failure(error)
        return request.redirect(compat.record_url('eh.shopify.store', store.id))

    @staticmethod
    def _reply(status, text):
        return request.make_response(text, headers=[('Content-Type', 'text/plain; charset=utf-8')], status=status)
