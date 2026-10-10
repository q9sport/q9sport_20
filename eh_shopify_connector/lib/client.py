# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify Admin GraphQL client.

Design rules, each one a direct answer to a failure seen in the field:

* One transport per store with explicit connect and read timeouts. A call can
  never hang a worker forever.
* Pacing from ``extensions.cost.throttleStatus`` before sending, then THROTTLED
  and HTTP 429 handled with bounded waits. Neither means the request ran, so
  both are always safe to resend.
* A mutation is only ever resent automatically when it carries an idempotency
  key, or when the failure proves it never reached Shopify (a connect error,
  HTTP 429, THROTTLED). A read timeout, a 5xx or an unreadable 200 on a
  mutation without a key is reported as ambiguous instead of replayed.
* Any GraphQL error aborts the call with a typed exception. Partial data is
  never returned as if it were complete, and pagination never stops silently.
* Tokens are fetched from a provider callable at send time and never logged.
"""
import json
import logging
import random
import re
import time

from . import errors, throttle, versions

_logger = logging.getLogger(__name__)

USER_AGENT = "ERPHeritage-ShopifyConnector/1.0"
IDEMPOTENT_MARKER = "@idempotent(key: $idempotencyKey)"
_OPERATION_RE = re.compile(r"^\s*(?:#[^\n]*\n\s*)*(query|mutation|subscription)\b\s*([A-Za-z_][A-Za-z0-9_]*)?", re.I)
_SCOPE_RE = re.compile(r"`([a-z_]+)`")

RETRYABLE_CODES = {"INTERNAL_SERVER_ERROR", "SERVICE_UNAVAILABLE", "TIMEOUT", "BAD_GATEWAY"}
QUERY_CODES_NOT_FOUND = {"NOT_FOUND", "RESOURCE_NOT_FOUND"}


class TransportConnectError(Exception):
    """The request did not reach Shopify (DNS, TCP, TLS, connect timeout)."""


BEFORE_SEND = {"NewConnectionError", "NameResolutionError", "ConnectTimeoutError", "ConnectTimeout", "ProxyError",
               "SSLCertVerificationError", "SSLError", "gaierror"}


def _never_left(error):
    """True when the request cannot have reached Shopify, so replaying it is safe."""
    names, seen = set(), error
    for _step in range(6):
        if seen is None:
            break
        names.add(type(seen).__name__)
        seen = getattr(seen, "__cause__", None) or getattr(seen, "__context__", None)
    for argument in getattr(error, "args", ()):
        names.add(type(argument).__name__)
        for inner in getattr(argument, "args", ()):
            names.add(type(inner).__name__)
    return bool(names & BEFORE_SEND)


class TransportReadTimeout(Exception):
    """The request was sent but no response arrived in time."""


class RequestsTransport(object):
    """Thin wrapper so tests can swap the network for a fake."""

    def __init__(self, session=None):
        import requests
        from requests.adapters import HTTPAdapter

        self._requests = requests
        self.session = session or requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self.session.mount("https://", adapter)

    def post(self, url, headers, body, timeout):
        exc = self._requests.exceptions
        try:
            response = self.session.post(url, data=body, headers=headers, timeout=timeout)
        except exc.ConnectTimeout as error:
            raise TransportConnectError(str(error))
        except exc.ReadTimeout as error:
            raise TransportReadTimeout(str(error))
        except (exc.ConnectionError, exc.SSLError) as error:
            # A failure while connecting is safe to replay; one after the request left is not, because Shopify
            # may have run it.
            if _never_left(error):
                raise TransportConnectError(str(error))
            raise TransportReadTimeout(str(error))
        return response.status_code, response.headers, response.content

    def post_form(self, url, data, timeout=(10, 30)):
        exc = self._requests.exceptions
        try:
            response = self.session.post(url, data=data, headers={"Accept": "application/json"},
                                         timeout=timeout)
        except exc.Timeout as error:
            raise errors.TransientError("Shopify token endpoint timed out: %s" % error)
        except (exc.ConnectionError, exc.SSLError) as error:
            raise errors.TransientError("Could not reach the Shopify token endpoint: %s" % error)
        return response.status_code, response.text

    def upload_multipart(self, url, fields, filename, content, mimetype, timeout=(10, 120)):
        """POST a file to a staged upload target and return the HTTP status."""
        exc = self._requests.exceptions
        try:
            response = self.session.post(url, data=fields, files={"file": (filename, content, mimetype)},
                                         timeout=timeout, allow_redirects=False)
        except exc.Timeout as error:
            raise errors.TransientError("Image upload timed out: %s" % error)
        except (exc.ConnectionError, exc.SSLError) as error:
            raise errors.TransientError("Image upload failed: %s" % error)
        return response.status_code

    def get_bytes(self, url, timeout=(10, 60), max_bytes=20 * 1024 * 1024):
        """Download a file with a size cap and without following redirects."""
        exc = self._requests.exceptions
        try:
            with self.session.get(url, stream=True, timeout=timeout, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise errors.QueryError("Image download failed with HTTP %s" % response.status_code)
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        raise errors.QueryError("Image is larger than %s bytes" % max_bytes)
                    chunks.append(chunk)
                return b"".join(chunks)
        except exc.Timeout as error:
            raise errors.TransientError("Image download timed out: %s" % error)
        except (exc.ConnectionError, exc.SSLError) as error:
            raise errors.TransientError("Image download failed: %s" % error)

    def download_to(self, url, handle, offset, timeout, deadline=None, clock=time.monotonic):
        """Append the file at ``url`` to ``handle`` from byte ``offset`` until it
        ends or ``deadline`` passes, and say how far it got.

        Returns a dict: ``complete`` when the server sent the rest of the file,
        ``restart`` when it ignored the byte range and sent the whole file again
        (the handle is emptied first), and ``total`` when the server stated the
        full size. A connection that closes early with a stated size is reported
        incomplete, so the next call resumes instead of trusting a short file."""
        exc = self._requests.exceptions
        headers = {"Range": "bytes=%d-" % offset} if offset else {}
        try:
            with self.session.get(url, stream=True, timeout=timeout, headers=headers,
                                  allow_redirects=False) as response:
                if offset and response.status_code == 416:
                    return {"complete": True, "restart": False, "total": offset}
                if response.status_code not in (200, 206):
                    raise errors.BulkOperationError(
                        "Bulk result download failed with HTTP %s" % response.status_code)
                restart = bool(offset) and response.status_code != 206
                start = offset if response.status_code == 206 else 0
                if restart:
                    handle.seek(0)
                    handle.truncate()
                encoded = (response.headers.get("Content-Encoding") or "identity").lower() != "identity"
                total = None
                if response.status_code == 206:
                    match = re.search(r"/(\d+)\s*$", response.headers.get("Content-Range") or "")
                    total = int(match.group(1)) if match else None
                elif (response.headers.get("Content-Length") or "").isdigit() and not encoded:
                    total = int(response.headers["Content-Length"])
                written, stopped = 0, False
                for chunk in response.iter_content(65536):
                    handle.write(chunk)
                    written += len(chunk)
                    if deadline is not None and clock() > deadline:
                        stopped = True
                        break
                size = start + written
                complete = not stopped and (total is None or size >= total)
                return {"complete": complete, "restart": restart, "total": total}
        except exc.Timeout as error:
            raise errors.TransientError("Bulk result download timed out: %s" % error)
        except (exc.ConnectionError, exc.SSLError) as error:
            raise errors.TransientError("Bulk result download failed: %s" % error)

    def stream_lines(self, url, timeout):
        """Yield decoded lines of a bulk operation JSONL result."""
        exc = self._requests.exceptions
        try:
            with self.session.get(url, stream=True, timeout=timeout) as response:
                if response.status_code != 200:
                    raise errors.BulkOperationError(
                        "Bulk result download failed with HTTP %s" % response.status_code)
                for raw in response.iter_lines():
                    if raw:
                        yield raw.decode("utf-8")
        except exc.Timeout as error:
            raise errors.TransientError("Bulk result download timed out: %s" % error)
        except (exc.ConnectionError, exc.SSLError) as error:
            raise errors.TransientError("Bulk result download failed: %s" % error)


class Response(object):
    __slots__ = ("data", "cost", "served_version", "deprecated_reason", "request_id", "status", "warnings")

    def __init__(self, data, cost, served_version, deprecated_reason, request_id, status, warnings=None):
        self.warnings = list(warnings or [])
        self.data = data
        self.cost = cost
        self.served_version = served_version
        self.deprecated_reason = deprecated_reason
        self.request_id = request_id
        self.status = status


def operation_of(document):
    """Return (kind, name) for a GraphQL document."""
    match = _OPERATION_RE.match(document or "")
    if not match:
        return "query", None
    return match.group(1).lower(), match.group(2)


def dig(data, path):
    node = data
    for key in path:
        if node is None:
            return None
        node = node.get(key)
    return node


def connection_nodes(connection):
    if not connection:
        return []
    if connection.get("nodes") is not None:
        return connection["nodes"]
    return [edge["node"] for edge in connection.get("edges") or []]


class GraphQLClient(object):
    def __init__(self, shop_domain, token_provider, api_version=versions.DEFAULT, transport=None,
                 timeout=(10, 60), max_retries=5, max_throttle_waits=10, sleep=time.sleep,
                 clock=time.monotonic, rng=random.random, bucket=None, on_event=None, deadline=None):
        self.deadline = deadline
        self._clock = clock
        self.shop_domain = shop_domain
        self.api_version = api_version
        self._token_provider = token_provider
        self.transport = transport or RequestsTransport()
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_throttle_waits = max_throttle_waits
        self._sleep = sleep
        self._rng = rng
        self.bucket = bucket or throttle.bucket_for(shop_domain, clock=clock)
        self._cost_memory = {}
        self._on_event = on_event
        self.served_version = None
        self.deprecations = set()
        self.calls = 0
        self.throttle_seconds = 0.0

    @property
    def endpoint(self):
        return "https://%s/admin/api/%s/graphql.json" % (self.shop_domain, self.api_version)

    # ------------------------------------------------------------------ helpers
    def _emit(self, event, **values):
        if self._on_event:
            try:
                self._on_event(event, values)
            except Exception:  # never let telemetry break a sync
                _logger.debug("Shopify client event hook failed", exc_info=True)

    def _remaining(self):
        return None if self.deadline is None else self.deadline - self._clock()

    def _out_of_time(self):
        return errors.TransientError("The sync job used its time budget; it continues in a later run.",
                                     code="deadline", retry_after=5)

    def _wait(self, seconds, reason):
        if seconds <= 0:
            return
        remaining = self._remaining()
        if remaining is not None and seconds >= remaining:
            raise self._out_of_time()
        self.throttle_seconds += seconds
        self._emit("wait", seconds=seconds, reason=reason)
        self._sleep(seconds)

    def _headers(self, token=None):
        token = token or self._token_provider(force_refresh=False)
        if not token:
            raise errors.AuthError("No access token is available for %s." % self.shop_domain)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Shopify-Access-Token": token,
        }

    @staticmethod
    def _normalise_errors(body):
        if isinstance(body, list):
            return body, None, None
        if not isinstance(body, dict):
            return [{"message": "Unexpected response body"}], None, None
        raw = body.get("errors")
        if raw is None:
            found = []
        elif isinstance(raw, list):
            found = raw
        elif isinstance(raw, dict):
            found = [{"message": json.dumps(raw)}]
        else:
            found = [{"message": str(raw)}]
        return found, body.get("data"), body.get("extensions") or {}

    def _cost_key(self, name, variables):
        return (name, variables.get("first"))

    # ------------------------------------------------------------------ core
    def execute(self, document, variables=None, idempotency_key=None, tolerate=None):
        kind, name = operation_of(document)
        is_mutation = kind == "mutation"
        if IDEMPOTENT_MARKER in (document or "") and not idempotency_key:
            raise errors.QueryError("%s must be sent with an idempotency key." % (name or "This mutation"),
                                    code="IDEMPOTENCY_KEY_REQUIRED")
        variables = dict(variables or {})
        if idempotency_key is not None:
            variables["idempotencyKey"] = idempotency_key
        replay_safe = (not is_mutation) or idempotency_key is not None
        payload = {"query": document, "variables": variables}
        if name:
            payload["operationName"] = name
        body = json.dumps(payload).encode("utf-8")
        cost_key = self._cost_key(name, variables)

        attempts = 0
        throttle_waits = 0
        refreshed = False
        forced_token = None
        while True:
            remaining = self._remaining()
            if remaining is not None and remaining <= 1:
                raise self._out_of_time()
            self._wait(self.bucket.wait_for(self._cost_memory.get(cost_key)), "cost")
            headers = self._headers(token=forced_token)
            timeout = self.timeout
            remaining = self._remaining()
            if remaining is not None:
                timeout = (timeout[0], max(1.0, min(timeout[1], remaining - 1)))
            started = time.time()
            try:
                status, response_headers, content = self.transport.post(
                    self.endpoint, headers=headers, body=body, timeout=timeout)
            except TransportConnectError as error:
                if attempts < self.max_retries:
                    self._wait(throttle.backoff(attempts, rng=self._rng), "connect")
                    attempts += 1
                    continue
                raise errors.TransientError("Could not reach Shopify: %s" % error)
            except TransportReadTimeout as error:
                if not replay_safe:
                    raise errors.TransientError(
                        "Shopify did not answer %s in time; the change may or may not have been "
                        "applied, so it is not replayed automatically." % (name or "mutation"),
                        ambiguous=True)
                if attempts < self.max_retries:
                    self._wait(throttle.backoff(attempts, rng=self._rng), "read_timeout")
                    attempts += 1
                    continue
                raise errors.TransientError("Shopify timed out: %s" % error)
            self.calls += 1
            response_headers = response_headers or {}
            served = response_headers.get("X-Shopify-API-Version")
            if served:
                self.served_version = served
            deprecated = response_headers.get("X-Shopify-API-Deprecated-Reason")
            if deprecated:
                self.deprecations.add(deprecated)
            request_id = response_headers.get("X-Request-Id")
            self._emit("response", operation=name, status=status, seconds=time.time() - started,
                       request_id=request_id)

            if status == 401:
                if not refreshed:
                    refreshed = True
                    # Use the token the refresh returned: a token committed by
                    # another transaction is not visible to this one yet.
                    forced_token = self._token_provider(force_refresh=True)
                    continue
                raise errors.AuthError(
                    "Shopify rejected the access token for %s (HTTP 401)." % self.shop_domain,
                    code="unauthorized")
            if status == 403:
                text = content.decode("utf-8", "replace") if content else ""
                raise errors.ScopeError(
                    "Shopify refused access (HTTP 403): %s" % text[:300],
                    missing_scopes=_SCOPE_RE.findall(text), code="forbidden")
            if status in (402, 423):
                raise errors.ShopUnavailable(
                    "The shop %s is frozen or locked (HTTP %s)." % (self.shop_domain, status),
                    code="shop_%s" % status, retry_after=3600)
            if status == 404:
                raise errors.ShopUnavailable(
                    "No Shopify store answers at %s (HTTP 404)." % self.shop_domain,
                    code="shop_not_found", retry_after=3600)
            if status == 429:
                retry_after = _float(response_headers.get("Retry-After"), 2.0)
                throttle_waits += 1
                if throttle_waits > self.max_throttle_waits:
                    raise errors.ThrottledError("Shopify rate limit persisted.", retry_after=retry_after)
                self._wait(throttle.jitter(retry_after, rng=self._rng), "http_429")
                continue
            if status >= 500:
                if not replay_safe:
                    raise errors.TransientError(
                        "Shopify answered %s with HTTP %s; the change may or may not have been applied, so it is "
                        "not replayed automatically." % (name or "the mutation", status),
                        code="http_%s" % status, ambiguous=True)
                if attempts < self.max_retries:
                    self._wait(throttle.backoff(attempts, rng=self._rng), "http_%s" % status)
                    attempts += 1
                    continue
                raise errors.TransientError("Shopify returned HTTP %s." % status, code="http_%s" % status)
            if status != 200:
                raise errors.ShopifyError("Unexpected HTTP %s from Shopify." % status, code="http_%s" % status)

            try:
                parsed = json.loads(content.decode("utf-8")) if content else {}
            except ValueError:
                if not replay_safe:
                    raise errors.TransientError(
                        "Shopify returned an unreadable answer to %s; the change may or may not have been "
                        "applied, so it is not replayed automatically." % (name or "the mutation"),
                        ambiguous=True)
                if attempts < self.max_retries:
                    self._wait(throttle.backoff(attempts, rng=self._rng), "bad_json")
                    attempts += 1
                    continue
                raise errors.TransientError("Shopify returned an unreadable response.")

            found, data, extensions = self._normalise_errors(parsed)
            codes = {((e.get("extensions") or {}).get("code") or "").upper() for e in found}
            cost = (extensions or {}).get("cost")
            if cost:
                self.bucket.update(cost)
                if name and cost.get("requestedQueryCost") is not None and "MAX_COST_EXCEEDED" not in codes:
                    self._cost_memory[cost_key] = cost["requestedQueryCost"]

            if found and tolerate and data is not None and all(tolerate(e) for e in found):
                return Response(data or {}, cost, served, deprecated, request_id, status, warnings=found)
            if found:
                if "THROTTLED" in codes:
                    throttle_waits += 1
                    if throttle_waits > self.max_throttle_waits:
                        raise errors.ThrottledError("Shopify cost limit persisted.", retry_after=5)
                    wait = self.bucket.wait_for(self._cost_memory.get(cost_key)) or throttle.backoff(
                        throttle_waits, base=1.0, cap=10.0, rng=self._rng)
                    self._wait(wait, "throttled")
                    continue
                if codes & RETRYABLE_CODES and attempts < self.max_retries and replay_safe:
                    self._wait(throttle.backoff(attempts, rng=self._rng), "graphql_transient")
                    attempts += 1
                    continue
                message = "; ".join(e.get("message", "") for e in found)[:500]
                if "MAX_COST_EXCEEDED" in codes:
                    raise errors.CostExceeded(message, code="MAX_COST_EXCEEDED")
                if "ACCESS_DENIED" in codes:
                    required = " ".join(
                        str((e.get("extensions") or {}).get("requiredAccess") or e.get("message") or "")
                        for e in found)
                    raise errors.ScopeError(message, missing_scopes=sorted(set(_SCOPE_RE.findall(required))),
                                            code="ACCESS_DENIED")
                if codes & RETRYABLE_CODES:
                    raise errors.TransientError(message, code=sorted(codes & RETRYABLE_CODES)[0],
                                                ambiguous=not replay_safe)
                if codes and codes <= QUERY_CODES_NOT_FOUND:
                    raise errors.NotFound(message, code="NOT_FOUND")
                raise errors.QueryError(message, code=",".join(sorted(c for c in codes if c)) or None,
                                        details={"errors": found, "operation": name,
                                                 "api_version": self.api_version})
            return Response(data or {}, cost, served, deprecated, request_id, status)

    def mutate(self, document, variables, payload_key, idempotency_key=None, concurrent_retries=4):
        """Run a mutation and return its payload, raising UserErrors when needed."""
        attempt = 0
        while True:
            response = self.execute(document, variables, idempotency_key=idempotency_key)
            payload = (response.data or {}).get(payload_key)
            if payload is None:
                raise errors.QueryError("Mutation %s returned no payload." % payload_key)
            user_errors = []
            for key, value in payload.items():
                if (key == "userErrors" or key.endswith("UserErrors")) and value:
                    user_errors.extend(value)
            if not user_errors:
                return payload
            codes = {e.get("code") for e in user_errors}
            if idempotency_key and codes == {"IDEMPOTENCY_CONCURRENT_REQUEST"}:
                if attempt < concurrent_retries:
                    self._wait(throttle.backoff(attempt, base=1.0, cap=10.0, rng=self._rng), "idempotency")
                    attempt += 1
                    continue
                raise errors.TransientError(
                    "Shopify is still applying an earlier attempt of %s." % payload_key, retry_after=30)
            if "IDEMPOTENCY_KEY_PARAMETER_MISMATCH" in codes:
                raise errors.QueryError(
                    "%s reused an idempotency key with different input." % payload_key,
                    code="IDEMPOTENCY_KEY_PARAMETER_MISMATCH", details={"user_errors": user_errors})
            message = "; ".join(
                "%s%s" % (("%s: " % ".".join(str(p) for p in e["field"])) if e.get("field") else "",
                          e.get("message", "")) for e in user_errors)
            raise errors.UserErrors(message[:500], errors=user_errors)

    def iter_pages(self, document, variables, path, page_size=250, min_page_size=1, after=None):
        """Yield ``(nodes, end_cursor, has_next_page)`` for each page of a
        connection, halving the page on cost overruns. ``after`` resumes from a
        cursor saved by an earlier call."""
        first = page_size
        while True:
            page_vars = dict(variables or {}, first=first, after=after)
            try:
                response = self.execute(document, page_vars)
            except errors.CostExceeded:
                if first <= min_page_size:
                    raise
                first = max(min_page_size, first // 2)
                continue
            connection = dig(response.data, path)
            if connection is None:
                raise errors.NotFound("Connection %s missing in the response." % ".".join(path))
            info = connection.get("pageInfo") or {}
            more = bool(info.get("hasNextPage"))
            cursor = info.get("endCursor")
            if more and not cursor:
                raise errors.IncompleteData(
                    "Connection %s reported more pages without a cursor." % ".".join(path))
            yield list(connection_nodes(connection)), cursor, more
            if not more:
                return
            after = cursor

    def paginate(self, document, variables, path, page_size=250, min_page_size=1):
        """Yield every node of a connection, halving the page on cost overruns."""
        for nodes, _cursor, _more in self.iter_pages(document, variables, path, page_size, min_page_size):
            for node in nodes:
                yield node

    def complete_connection(self, parent, key, document, node_id, path, page_size=250, min_page_size=1):
        """Return every node of ``parent[key]``, fetching further pages with
        ``document`` (which selects ``node(id: $id) { ... key(first, after) }``)
        whenever the embedded first page says there is more."""
        connection = parent.get(key) or {}
        nodes = list(connection_nodes(connection))
        info = connection.get("pageInfo") or {}
        after = info.get("endCursor")
        first = page_size
        while info.get("hasNextPage"):
            if after is None:
                raise errors.IncompleteData("Nested connection %s has no cursor." % key)
            try:
                response = self.execute(document, {"id": node_id, "first": first, "after": after or None})
            except errors.CostExceeded:
                if first <= min_page_size:
                    raise
                first = max(min_page_size, first // 2)
                continue
            page = dig(response.data, path)
            if page is None:
                raise errors.IncompleteData("Nested connection %s vanished while paginating." % key)
            nodes.extend(connection_nodes(page))
            info = page.get("pageInfo") or {}
            after = info.get("endCursor")
        return nodes

    def stream_bulk_result(self, url, timeout=(10, 300)):
        transport = getattr(self.transport, "stream_lines", None)
        if transport is None:
            raise errors.BulkOperationError("Transport cannot stream bulk results.")
        for line in transport(url, timeout=timeout):
            yield json.loads(line)


def assemble_bulk(rows):
    """Rebuild object trees from flat bulk operation JSONL rows.

    Shopify guarantees only that a child row comes after its parent, not that a
    parent's children are adjacent, and nesting can go two levels deep. Rows are
    therefore attached by ``__parentId``; a row naming an unknown parent means
    the file is incomplete and raises instead of being dropped.
    """
    roots = []
    index = {}
    for row in rows:
        parent_id = row.pop("__parentId", None)
        node = {"node": row, "children": []}
        if row.get("id"):
            index[row["id"]] = node
        if parent_id is None:
            roots.append(node)
            continue
        owner = index.get(parent_id)
        if owner is None:
            raise errors.IncompleteData("Bulk result row refers to unknown parent %s." % parent_id)
        owner["children"].append(node)
    return roots


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
