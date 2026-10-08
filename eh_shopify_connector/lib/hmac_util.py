# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Signature checks and shop domain handling.

Both checks use ``hmac.compare_digest`` so timing does not leak how much of a
forged signature matched.
"""
import base64
import hashlib
import hmac
import re
from urllib.parse import urlparse

_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")
_ADMIN_STORE_RE = re.compile(r"^/store/([a-z0-9][a-z0-9\-]*)", re.I)


def _to_bytes(value):
    if isinstance(value, bytes):
        return value
    return (value or "").encode("utf-8")


def webhook_signature(secret, raw_body):
    digest = hmac.new(_to_bytes(secret), _to_bytes(raw_body), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_webhook(secret, raw_body, header_value):
    """Verify ``X-Shopify-Hmac-Sha256`` over the exact raw request body."""
    if not secret or not header_value:
        return False
    expected = webhook_signature(secret, raw_body)
    return hmac.compare_digest(expected.encode("ascii"), _to_bytes(header_value.strip()))


def oauth_query_signature(secret, params):
    """Signature Shopify puts in the ``hmac`` query parameter of install and
    callback redirects: hex HMAC SHA256 over the remaining parameters sorted by
    name and joined as ``k=v`` with ``&``."""
    items = []
    for key in sorted(params):
        if key in ("hmac", "signature"):
            continue
        value = params[key]
        if isinstance(value, (list, tuple)):
            value = ",".join(value)
        items.append("%s=%s" % (key, value))
    message = "&".join(items)
    return hmac.new(_to_bytes(secret), _to_bytes(message), hashlib.sha256).hexdigest()


def verify_oauth_query(secret, params):
    given = params.get("hmac")
    if not secret or not given:
        return False
    expected = oauth_query_signature(secret, params)
    return hmac.compare_digest(expected.encode("ascii"), _to_bytes(given))


def normalize_shop_domain(value):
    """Turn what a merchant pastes into ``handle.myshopify.com``.

    Accepts a bare handle, a myshopify domain, a URL on that domain, or an
    admin URL such as ``https://admin.shopify.com/store/handle``. Returns None
    when the value cannot be a Shopify store domain (for example a custom
    storefront domain, which does not identify the shop for the Admin API).
    """
    if not value:
        return None
    text = value.strip().lower()
    if "://" not in text and "/" in text:
        text = "https://" + text
    if "://" in text:
        parsed = urlparse(text)
        host = parsed.hostname or ""
        if host == "admin.shopify.com":
            match = _ADMIN_STORE_RE.match(parsed.path or "")
            if not match:
                return None
            text = match.group(1)
        else:
            text = host
    text = text.strip("/")
    if "." not in text:
        text = text + ".myshopify.com"
    return text if _SHOP_RE.match(text) else None


def is_valid_shop_domain(value):
    return bool(value) and bool(_SHOP_RE.match(value))
