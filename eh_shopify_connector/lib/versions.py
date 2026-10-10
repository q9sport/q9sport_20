# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify Admin API version window and version gated features.

Shopify ships a stable version on the first day of each quarter and supports
each one for at least twelve months. Requests to an unsupported version are
silently served by the oldest supported one, which is why the client also
compares the ``X-Shopify-API-Version`` response header with what it asked for.

Floor is 2026-01 because the connector relies on three things that do not
exist in 2025-10: ``InventoryQuantityInput.changeFromQuantity``, the
``@idempotent(key:)`` directive and the ``bulkOperation(id:)`` query. These
facts were checked against live introspection of every version below.
"""
import datetime as _dt

# handle: (release date, end of support date)
VERSIONS = {
    "2025-10": (_dt.date(2025, 10, 1), _dt.date(2026, 10, 16)),
    "2026-01": (_dt.date(2026, 1, 1), _dt.date(2027, 1, 16)),
    "2026-04": (_dt.date(2026, 4, 1), _dt.date(2027, 4, 16)),
    "2026-07": (_dt.date(2026, 7, 1), _dt.date(2027, 7, 16)),
    "2026-10": (_dt.date(2026, 10, 1), _dt.date(2027, 10, 16)),
}

FLOOR = "2026-01"
DEFAULT = "2026-07"
WARN_DAYS = 90

# feature: first version that has it
FEATURES = {
    "change_from_quantity": "2026-01",
    "idempotent_directive": "2026-01",
    "bulk_operation_by_id": "2026-01",
    "return_reason_definitions": "2026-01",
    "webhook_subscription_name": "2026-04",
    "inventory_level_is_active": "2026-04",
    "inventory_shipments": "2026-04",
    "order_attribution": "2026-07",
    "line_item_weight": "2026-07",
    "variant_barcodes_list": "2026-10",
}


def _key(handle):
    try:
        year, month = handle.split("-")
        return int(year) * 100 + int(month)
    except (AttributeError, ValueError):
        raise ValueError("Invalid Shopify API version handle: %r" % (handle,))


def is_known(handle):
    return handle in VERSIONS


def supports(handle, feature):
    """True when ``handle`` includes ``feature``."""
    first = FEATURES[feature]
    return _key(handle) >= _key(first)


def selectable(today=None):
    """Versions a merchant may pin: at or above the floor, released, not expired."""
    today = today or _dt.date.today()
    out = []
    for handle, (released, ends) in sorted(VERSIONS.items(), key=lambda kv: _key(kv[0])):
        if _key(handle) < _key(FLOOR):
            continue
        if released > today or ends <= today:
            continue
        out.append(handle)
    return out


def status(handle, today=None):
    """Return (state, days_left) where state is one of
    ok, warning, expired, below_floor, unreleased, unknown."""
    today = today or _dt.date.today()
    if handle not in VERSIONS:
        return "unknown", None
    released, ends = VERSIONS[handle]
    days_left = (ends - today).days
    if _key(handle) < _key(FLOOR):
        return "below_floor", days_left
    if released > today:
        return "unreleased", days_left
    if days_left <= 0:
        return "expired", days_left
    if days_left <= WARN_DAYS:
        return "warning", days_left
    return "ok", days_left


def recommended(today=None):
    """Newest released version at or above the floor."""
    options = selectable(today)
    return options[-1] if options else DEFAULT
