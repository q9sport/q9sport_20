# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""OAuth token grants for Shopify Dev Dashboard apps.

Checked against shopify.dev (September 2026):

* Client credentials: POST form ``grant_type=client_credentials``,
  ``client_id``, ``client_secret`` to ``https://{shop}/admin/oauth/access_token``.
  Returns ``access_token``, ``scope``, ``expires_in`` (86399). No refresh
  token. Only works when the app and the store belong to the same Shopify
  organisation, otherwise ``shop_not_permitted``.
* Authorization code: browser goes to ``/admin/oauth/authorize`` with
  ``client_id``, ``scope``, ``redirect_uri``, ``state``. Callback carries
  ``code``, ``hmac``, ``shop``, ``state``, ``timestamp``. The code is
  exchanged with ``client_id``, ``client_secret``, ``code`` and ``expiring=1``
  for a one hour access token plus a refresh token valid 90 days.
* Refresh: POST ``grant_type=refresh_token`` with ``client_id``,
  ``client_secret``, ``refresh_token``. Refresh tokens may rotate, so the
  returned one always replaces the stored one.
"""
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

from . import errors, hmac_util

_KNOWN_OAUTH_CODES = ("shop_not_permitted", "invalid_client", "invalid_grant", "invalid_request",
                      "invalid_scope", "unauthorized_client", "access_denied")


class Token(object):
    def __init__(self, access_token, scope=None, expires_in=None, refresh_token=None,
                 refresh_token_expires_in=None, obtained_at=None):
        self.access_token = access_token
        self.scope = scope
        self.expires_in = expires_in
        self.refresh_token = refresh_token
        self.refresh_token_expires_in = refresh_token_expires_in
        self.obtained_at = obtained_at if obtained_at is not None else time.time()

    @property
    def expires_at(self):
        return self.obtained_at + self.expires_in if self.expires_in else None

    @property
    def refresh_expires_at(self):
        if not self.refresh_token_expires_in:
            return None
        return self.obtained_at + self.refresh_token_expires_in

    def scopes(self):
        return [s for s in (self.scope or "").split(",") if s]


def token_url(shop_domain):
    return "https://%s/admin/oauth/access_token" % shop_domain


def default_post_form(url, data, timeout=(10, 30)):
    import requests

    try:
        response = requests.post(url, data=data, headers={"Accept": "application/json"}, timeout=timeout)
    except requests.exceptions.Timeout as error:
        raise errors.TransientError("Shopify token endpoint timed out: %s" % error)
    except (requests.exceptions.ConnectionError, requests.exceptions.SSLError) as error:
        raise errors.TransientError("Could not reach the Shopify token endpoint: %s" % error)
    return response.status_code, response.text


def _parse(status, text, clock):
    body = None
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    if status == 200 and isinstance(body, dict) and body.get("access_token"):
        return Token(body["access_token"], scope=body.get("scope"), expires_in=body.get("expires_in"),
                     refresh_token=body.get("refresh_token"),
                     refresh_token_expires_in=body.get("refresh_token_expires_in"), obtained_at=clock())
    code = None
    description = ""
    if isinstance(body, dict):
        code = body.get("error") if isinstance(body.get("error"), str) else None
        description = body.get("error_description") or body.get("errors") or ""
        if isinstance(description, (dict, list)):
            description = json.dumps(description)
    lowered = (text or "").lower()
    if not code:
        for candidate in _KNOWN_OAUTH_CODES:
            if candidate in lowered:
                code = candidate
                break
    if status == 429:
        raise errors.ThrottledError("Shopify token endpoint rate limited the request.", retry_after=5)
    if status >= 500:
        raise errors.TransientError("Shopify token endpoint returned HTTP %s." % status)
    if code == "shop_not_permitted":
        raise errors.AuthError(
            "This store is not in the same Shopify organisation as the app, so the client credentials "
            "connection is not allowed. Use the install link connection instead.", code=code)
    if code in ("invalid_client", "unauthorized_client"):
        raise errors.AuthError("Shopify did not accept the client ID and client secret.", code=code)
    if code == "invalid_grant":
        raise errors.AuthError(
            "The authorisation expired or was already used. Reconnect the store.", code=code)
    raise errors.AuthError(
        "Shopify refused the token request (HTTP %s)%s." % (status, (": %s" % description) if description else ""),
        code=code or "http_%s" % status)


def client_credentials(shop_domain, client_id, client_secret, post_form=None, clock=time.time):
    if not (client_id and client_secret):
        raise errors.AuthError("Client ID and client secret are both required.", code="missing_credentials")
    status, text = (post_form or default_post_form)(token_url(shop_domain), {
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    return _parse(status, text, clock)


def new_state():
    return secrets.token_urlsafe(32)


def state_digest(state):
    return hashlib.sha256((state or "").encode("utf-8")).hexdigest()


def authorize_url(shop_domain, client_id, scopes, redirect_uri, state):
    query = urlencode({
        "client_id": client_id,
        "scope": ",".join(scopes),
        "redirect_uri": redirect_uri,
        "state": state,
    })
    return "https://%s/admin/oauth/authorize?%s" % (shop_domain, query)


def verify_callback(client_secret, params, expected_shop, expected_state_digest, max_age=600, clock=time.time):
    """Validate an authorization code callback. Returns the code or raises AuthError."""
    shop = params.get("shop")
    if not hmac_util.is_valid_shop_domain(shop or ""):
        raise errors.AuthError("The callback did not come from a Shopify store domain.", code="bad_shop")
    if shop != expected_shop:
        raise errors.AuthError("The callback is for a different store.", code="shop_mismatch")
    if not hmac_util.verify_oauth_query(client_secret, params):
        raise errors.AuthError("The callback signature is not valid.", code="bad_hmac")
    if not expected_state_digest or not secrets.compare_digest(
            state_digest(params.get("state")), expected_state_digest):
        raise errors.AuthError("The callback state does not match this connection attempt.", code="bad_state")
    try:
        timestamp = int(params.get("timestamp") or 0)
    except ValueError:
        timestamp = 0
    if timestamp and abs(clock() - timestamp) > max_age + 300:
        raise errors.AuthError("The callback is too old. Start the connection again.", code="stale_callback")
    code = params.get("code")
    if not code:
        raise errors.AuthError("The callback has no authorisation code.", code="missing_code")
    return code


def exchange_code(shop_domain, client_id, client_secret, code, expiring=True, post_form=None, clock=time.time):
    data = {"client_id": client_id, "client_secret": client_secret, "code": code}
    if expiring:
        data["expiring"] = "1"
    status, text = (post_form or default_post_form)(token_url(shop_domain), data)
    return _parse(status, text, clock)


def refresh(shop_domain, client_id, client_secret, refresh_token, post_form=None, clock=time.time):
    if not refresh_token:
        raise errors.AuthError("No refresh token is stored. Reconnect the store.", code="missing_refresh_token")
    status, text = (post_form or default_post_form)(token_url(shop_domain), {
        "client_id": client_id, "client_secret": client_secret,
        "grant_type": "refresh_token", "refresh_token": refresh_token})
    return _parse(status, text, clock)
