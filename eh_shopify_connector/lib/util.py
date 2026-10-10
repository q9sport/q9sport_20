# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Small helpers shared by bindings and handlers."""
import hashlib
import json
import re

_PHONE_RE = re.compile(r"[^\d+]")

_GID_RE = re.compile(r"^gid://shopify/([A-Za-z]+)/(\d+)")


def fingerprint(value):
    """Stable digest of a JSON serialisable value, used to skip no op writes."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def gid_parts(gid):
    match = _GID_RE.match(gid or "")
    if not match:
        return None, None
    return match.group(1), match.group(2)


def gid(kind, legacy_id):
    return "gid://shopify/%s/%s" % (kind, legacy_id)


def redact(text, secrets_to_hide):
    """Remove known secret values from a message before it is stored or logged."""
    if not text:
        return text
    out = str(text)
    for secret in secrets_to_hide or ():
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[redacted]")
    return out


def readable_enum(value):
    """Shopify's PARTIALLY_REFUNDED as a person reads it: Partially refunded."""
    if not value:
        return value
    return value.replace("_", " ").strip().capitalize()


def normalize_email(value):
    value = (value or "").strip().lower()
    return value or None


def normalize_phone(value):
    value = _PHONE_RE.sub("", value or "")
    return value or None


def address_key(address, name=None):
    """Stable identity of a postal address, used to reuse delivery contacts."""
    if not address:
        return None
    parts = [
        name or address.get("name") or " ".join(p for p in (address.get("firstName"), address.get("lastName")) if p),
        address.get("company"), address.get("address1"), address.get("address2"), address.get("city"),
        address.get("zip"), address.get("provinceCode"), address.get("countryCodeV2") or address.get("countryCode"),
        normalize_phone(address.get("phone")),
    ]
    cleaned = [" ".join((p or "").lower().split()) for p in parts]
    if not any(cleaned):
        return None
    return fingerprint(cleaned)


def display_name(customer=None, address=None, email=None, phone=None, fallback=None):
    customer = customer or {}
    address = address or {}
    candidates = [
        customer.get("displayName"),
        " ".join(p for p in (customer.get("firstName"), customer.get("lastName")) if p),
        address.get("name"),
        " ".join(p for p in (address.get("firstName"), address.get("lastName")) if p),
        address.get("company"),
        email,
        phone,
        fallback,
    ]
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    return "Shopify customer"
