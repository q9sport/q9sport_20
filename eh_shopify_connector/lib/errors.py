# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Typed errors raised by the GraphQL client.

Every error carries a stable ``kind`` so the job engine can decide, without
string matching, whether to retry, how long to wait, and what to tell the
merchant.
"""


PROTECTED_DATA_MARKERS = ("not approved to access", "not approved to use")


def is_protected_data_error(error):
    """Shopify answers 200 with null fields and this kind of error when an app
    lacks protected customer data approval. The rest of the data is valid."""
    message = (error.get("message") or "").lower()
    return any(marker in message for marker in PROTECTED_DATA_MARKERS)


class ShopifyError(Exception):
    """Base class. ``retryable`` tells the job engine a later attempt may work."""

    kind = "error"
    retryable = False

    def __init__(self, message, *, code=None, details=None, retry_after=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}
        self.retry_after = retry_after

    def as_dict(self):
        return {
            "kind": self.kind,
            "message": self.message,
            "code": self.code,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "details": self.details,
        }


class AuthError(ShopifyError):
    """The token is missing, expired, revoked or the app was uninstalled."""

    kind = "auth"


class ScopeError(ShopifyError):
    """The app lacks an access scope required by an operation."""

    kind = "scope"

    def __init__(self, message, *, missing_scopes=None, **kw):
        super().__init__(message, **kw)
        self.missing_scopes = list(missing_scopes or [])
        self.details.setdefault("missing_scopes", self.missing_scopes)


class ShopUnavailable(ShopifyError):
    """The shop is frozen, paused, locked or not found (HTTP 402, 404, 423)."""

    kind = "shop_unavailable"
    retryable = True


class ThrottledError(ShopifyError):
    """Cost bucket or request rate exhausted after local waits."""

    kind = "throttled"
    retryable = True


class TransientError(ShopifyError):
    """Network failure, timeout or 5xx. ``ambiguous`` marks a mutation whose
    outcome is unknown because the response never arrived."""

    kind = "transient"
    retryable = True

    def __init__(self, message, *, ambiguous=False, **kw):
        super().__init__(message, **kw)
        self.ambiguous = ambiguous
        self.details.setdefault("ambiguous", ambiguous)


class QueryError(ShopifyError):
    """The document was rejected by schema validation. A programming or API
    version problem, never fixed by retrying."""

    kind = "query"


class CostExceeded(QueryError):
    """A single query asked for more than the per query cost cap."""

    kind = "cost_exceeded"


class MappingError(ShopifyError):
    """Something in Odoo that the connector needs is missing or ambiguous: a
    product, a tax, a journal, a currency. Fixed by the merchant, then retried."""

    kind = "mapping"


class UserErrors(ShopifyError):
    """A mutation ran but Shopify returned ``userErrors``."""

    kind = "user_errors"

    def __init__(self, message, *, errors=None, **kw):
        super().__init__(message, **kw)
        self.errors = list(errors or [])
        self.details.setdefault("user_errors", self.errors)

    def codes(self):
        return {e.get("code") for e in self.errors if e.get("code")}


class NotFound(ShopifyError):
    """The requested node does not exist (or is not visible to the app)."""

    kind = "not_found"


class VersionError(ShopifyError):
    """The configured API version is outside the supported window."""

    kind = "version"


class IncompleteData(ShopifyError):
    """A connection could not be fully paginated, so the payload is partial.
    Raised instead of silently importing a truncated record."""

    kind = "incomplete"
    retryable = True


class BulkOperationError(ShopifyError):
    """A bulk operation failed, expired or was cancelled."""

    kind = "bulk"
    retryable = True
