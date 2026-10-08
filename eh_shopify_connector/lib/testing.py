# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Offline Shopify for tests.

``FakeTransport`` stands in for the network. It answers each GraphQL
operation by name from an in memory ``FakeShop`` and can be scripted to fail
in exactly the ways the real API fails (HTTP 429 and 5xx, THROTTLED, timeouts,
cost overruns) so retry and pacing logic is proven without a live store.
"""
import copy
import json
import re

from .client import TransportConnectError, TransportReadTimeout

# Shop domain -> transport. Only Python code (tests) can populate it; stores
# fall back to the real network transport when their domain is absent.
TRANSPORT_OVERRIDES = {}

_OP_RE = re.compile(r"(query|mutation)\s+([A-Za-z_][A-Za-z0-9_]*)")


class CIDict(dict):
    """Case insensitive header dict, like requests' CaseInsensitiveDict."""

    def __init__(self, data=None):
        super().__init__()
        for key, value in (data or {}).items():
            self[key] = value

    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)


def throttle_status(available=1990.0, maximum=2000.0, rate=100.0, requested=10, actual=10):
    return {"requestedQueryCost": requested, "actualQueryCost": actual,
            "throttleStatus": {"maximumAvailable": maximum, "currentlyAvailable": available,
                               "restoreRate": rate}}


class FakeShop(object):
    """In memory store state. Extend with ``op_<OperationName>`` methods."""

    def __init__(self, domain="eh-test.myshopify.com", currency="USD", taxes_included=False,
                 scopes=None):
        self.domain = domain
        self.info = {
            "id": "gid://shopify/Shop/1",
            "name": "EH Test Store",
            "email": "owner@example.com",
            "myshopifyDomain": domain,
            "primaryDomain": {"host": domain},
            "currencyCode": currency,
            "enabledPresentmentCurrencies": [currency],
            "taxesIncluded": taxes_included,
            "taxShipping": True,
            "ianaTimezone": "UTC",
            "weightUnit": "KILOGRAMS",
            "plan": {"publicDisplayName": "Development", "partnerDevelopment": True, "shopifyPlus": False},
            "billingAddress": {"countryCodeV2": "US"},
        }
        self.scopes = list(scopes if scopes is not None else [
            "read_products", "write_products", "read_inventory", "write_inventory", "read_locations",
            "read_orders", "write_orders", "read_customers", "read_fulfillments",
            "read_assigned_fulfillment_orders", "write_assigned_fulfillment_orders",
            "read_merchant_managed_fulfillment_orders", "write_merchant_managed_fulfillment_orders",
            "read_returns", "read_shopify_payments_payouts",
        ])
        self.locations = [{
            "id": "gid://shopify/Location/1", "legacyResourceId": "1", "name": "Main warehouse",
            "isActive": True, "fulfillsOnlineOrders": True, "hasActiveInventory": True,
            "shipsInventory": True, "isFulfillmentService": False, "fulfillmentService": None,
            "address": {"address1": "1 Test St", "address2": None, "city": "Melbourne", "zip": "3000",
                        "provinceCode": "VIC", "countryCode": "AU", "phone": None},
        }]
        self.webhooks = []
        self._next_id = 1000
        self.client_id = "eh-client-id"
        self.client_secret = "eh-client-secret"
        self.same_organisation = True
        self.token_lifetime = 86399
        self.issued = []
        self.refresh_tokens = {}
        self.codes = {}
        self.bulk_results = {}
        # Bulk file downloads: bytes served per call (None for all) and, per
        # URL, how many downloads are cut in half without an error.
        self.bulk_download_limit = None
        self.bulk_cuts = {}
        self.bulk_operations = {}
        self.bulk_queries = []
        self.product_updates = []
        self.variant_updates = []
        self.variant_update_refusals = {}
        self.product_sets = []
        self.product_operations = {}
        self.staged = {}
        self.uploads = {}
        self.files = {}
        self.downloads = {}
        self.file_updates = []
        self.refund_creates = []
        self.refund_keys = {}
        self.refund_refusal = None
        self.returns = {}
        self.payments_account = {"id": "gid://shopify/ShopifyPaymentsAccount/1", "defaultCurrency": "USD"}
        self.payouts = []
        self.balance_transactions = []
        self.disputes = {}
        self.metafield_sets = []
        self.metafield_force_stale = False
        self.price_lists = {}
        self.price_list_updates = []
        self.quantity_pricing_updates = []
        self.customers = {}
        self.companies = {}
        self.gift_cards = []
        self.order_edit_sessions = {}
        self.order_edits = []
        self.order_edit_refuse = False
        self.order_edit_begin_timeout = False
        self.order_edit_commit_lost = False
        self.return_page_size = 50
        self.orders = {}
        self.inventory = {}
        self.fulfillment_orders = {}
        self.fulfillments = []
        self.inventory_error_codes = {"stale": "CHANGE_FROM_QUANTITY_STALE",
                                      "not_stocked": "ITEM_NOT_STOCKED_AT_LOCATION"}
        self.inventory_committed = {}
        self.inventory_reserved = {}
        self.untracked_items = set()
        self.missing_items = set()
        self.after_inventory_read = None
        self.cancel_requests = []
        self.tracking_updates = []
        self.fo_moves = []
        self.pickup_ready = []
        self.products = {}
        self.cancel_errors = None

    def next_gid(self, kind):
        self._next_id += 1
        return "gid://shopify/%s/%s" % (kind, self._next_id)

    # ---------------------------------------------------------------- operations
    def op_EhShopInfo(self, variables):
        return {"shop": copy.deepcopy(self.info)}

    def op_EhAppInstallation(self, variables):
        return {"currentAppInstallation": {
            "id": "gid://shopify/AppInstallation/1",
            "accessScopes": [{"handle": s} for s in self.scopes],
            "app": {"id": "gid://shopify/App/1", "title": "ERP Heritage", "handle": "erp-heritage"},
        }}

    def _page(self, items, variables):
        first = int(variables.get("first") or 50)
        after = variables.get("after")
        start = int(after) if after else 0
        chunk = items[start:start + first]
        end = start + len(chunk)
        return {"pageInfo": {"hasNextPage": end < len(items), "endCursor": str(end) if chunk else None},
                "nodes": copy.deepcopy(chunk)}

    def op_EhLocations(self, variables):
        return {"locations": self._page(self.locations, variables)}

    def op_EhWebhookSubscriptions(self, variables):
        return {"webhookSubscriptions": self._page(self.webhooks, variables)}

    def op_EhWebhookSubscriptionCreate(self, variables):
        sub = variables["subscription"]
        for existing in self.webhooks:
            if existing["topic"] == variables["topic"] and existing["uri"] == sub["uri"]:
                return {"webhookSubscriptionCreate": {"webhookSubscription": None, "userErrors": [
                    {"field": ["webhookSubscription", "callbackUrl"],
                     "message": "Address for this topic has already been taken"}]}}
        record = {"id": self.next_gid("WebhookSubscription"), "topic": variables["topic"],
                  "uri": sub["uri"], "format": sub.get("format", "JSON"), "filter": sub.get("filter"),
                  "includeFields": sub.get("includeFields") or [], "apiVersion": {"handle": "2026-07"},
                  "createdAt": "2026-09-15T00:00:00Z", "updatedAt": "2026-09-15T00:00:00Z"}
        self.webhooks.append(record)
        return {"webhookSubscriptionCreate": {"webhookSubscription": copy.deepcopy(record), "userErrors": []}}

    def op_EhWebhookSubscriptionDelete(self, variables):
        before = len(self.webhooks)
        self.webhooks = [w for w in self.webhooks if w["id"] != variables["id"]]
        if len(self.webhooks) == before:
            return {"webhookSubscriptionDelete": {"deletedWebhookSubscriptionId": None, "userErrors": [
                {"field": ["id"], "message": "Webhook subscription does not exist"}]}}
        return {"webhookSubscriptionDelete": {"deletedWebhookSubscriptionId": variables["id"], "userErrors": []}}


    # ---------------------------------------------------------------- orders
    def add_order(self, order):
        self.orders[order["id"]] = order
        for fulfillment_order in (order.get("_all") or {}).get("fulfillmentOrders") or []:
            self.fulfillment_orders[fulfillment_order["id"]] = fulfillment_order
        return order

    def _order_page(self, gid, key, variables):
        order = self.orders.get(gid) or {}
        nodes = (order.get("_all") or {}).get(key) or []
        return {"node": {key: self._page(nodes, variables)}}

    def op_EhOrder(self, variables):
        order = self.orders.get(variables["id"])
        if not order:
            return {"order": None}
        view = copy.deepcopy({k: v for k, v in order.items() if k != "_all"})
        for key, size in (("lineItems", 20), ("discountApplications", 20), ("shippingLines", 10),
                          ("fulfillmentOrders", 10)):
            nodes = (order.get("_all") or {}).get(key)
            if nodes is not None:
                view[key] = self._page(nodes, {"first": size})
        return {"order": view}

    def op_EhOrderLineItems(self, variables):
        return self._order_page(variables["id"], "lineItems", variables)

    def op_EhOrderDiscountApplications(self, variables):
        return self._order_page(variables["id"], "discountApplications", variables)

    def op_EhOrderShippingLines(self, variables):
        return self._order_page(variables["id"], "shippingLines", variables)

    def op_EhOrderFulfillmentOrders(self, variables):
        return self._order_page(variables["id"], "fulfillmentOrders", variables)

    def op_EhFulfillmentOrderLineItems(self, variables):
        return {"node": {"lineItems": self._page([], variables)}}

    def op_EhOrderRefundIds(self, variables):
        order = self.orders.get(variables["id"]) or {}
        refunds = (order.get("_all") or {}).get("refunds") or []
        return {"order": {"id": variables["id"], "refunds": [{"id": r["id"], "createdAt": r.get("createdAt"),
                                                              "note": r.get("note")} for r in refunds]}}

    def op_EhRefundLineItems(self, variables):
        for order in self.orders.values():
            for refund in (order.get("_all") or {}).get("refunds") or []:
                if refund["id"] == variables["id"]:
                    return {"node": {"refundLineItems": self._page(refund.get("lines") or [], variables)}}
        return {"node": None}

    def op_EhOrdersUpdated(self, variables):
        nodes = [{"id": o["id"], "name": o.get("name"), "updatedAt": o.get("updatedAt"),
                  "displayFinancialStatus": o.get("displayFinancialStatus"), "cancelledAt": o.get("cancelledAt"),
                  "test": o.get("test", False)} for o in self.orders.values()]
        query = variables.get("query") or ""
        if "updated_at:>='" in query:
            since = query.split("updated_at:>='", 1)[1].split("'", 1)[0]
            nodes = [n for n in nodes if (n["updatedAt"] or "") >= since]
        nodes.sort(key=lambda n: n["updatedAt"] or "")
        return {"orders": self._page(nodes, variables)}

    def op_EhOrderFulfillments(self, variables):
        order = self.orders.get(variables["id"])
        if not order:
            return {"order": None}
        fulfillments = []
        for record in (order.get("_all") or {}).get("fulfillments") or []:
            view = copy.deepcopy({k: v for k, v in record.items() if k != "_lines"})
            view["fulfillmentLineItems"] = self._page(record.get("_lines") or [], {"first": 10})
            fulfillments.append(view)
        return {"order": {"id": order["id"], "displayFulfillmentStatus": order.get("displayFulfillmentStatus"),
                          "fulfillmentsCount": {"count": len(fulfillments)}, "fulfillments": fulfillments}}

    def op_EhFulfillmentLineItems(self, variables):
        for order in self.orders.values():
            for record in (order.get("_all") or {}).get("fulfillments") or []:
                if record["id"] == variables["id"]:
                    return {"node": {"fulfillmentLineItems": self._page(record.get("_lines") or [], variables)}}
        return {"node": None}

    def op_EhFulfillmentTrackingUpdate(self, variables):
        self.tracking_updates.append(copy.deepcopy(variables))
        record = next((f for f in self.fulfillments if f["id"] == variables["fulfillmentId"]), None)
        if not record:
            return {"fulfillmentTrackingInfoUpdate": {"fulfillment": None, "userErrors": [
                {"field": ["fulfillmentId"], "message": "Fulfillment does not exist"}]}}
        info = variables.get("trackingInfoInput") or {}
        record["trackingInfo"] = [{"company": info.get("company"), "number": number, "url": None}
                                  for number in info.get("numbers") or []]
        return {"fulfillmentTrackingInfoUpdate": {"fulfillment": {"id": record["id"],
                                                                  "trackingInfo": record["trackingInfo"]},
                                                  "userErrors": []}}

    def _order_owning(self, fulfillment_order):
        for order in self.orders.values():
            if any(item is fulfillment_order for item in (order.get("_all") or {}).get("fulfillmentOrders") or []):
                return order
        return None

    def op_EhFulfillmentOrderOrder(self, variables):
        fulfillment_order = self.fulfillment_orders.get(variables["id"])
        order = self._order_owning(fulfillment_order) if fulfillment_order else None
        if not order:
            return {"node": None}
        return {"node": {"id": fulfillment_order["id"], "orderId": order["id"], "status": fulfillment_order["status"]}}

    def op_EhFulfillmentOrderMove(self, variables):
        self.fo_moves.append(copy.deepcopy(variables))
        fulfillment_order = self.fulfillment_orders.get(variables["id"])
        if not fulfillment_order or fulfillment_order["status"] not in ("OPEN", "IN_PROGRESS"):
            return {"fulfillmentOrderMove": {"movedFulfillmentOrder": None, "remainingFulfillmentOrder": None,
                                             "userErrors": [{"field": ["id"], "code": None,
                                                             "message": "Fulfillment order cannot be moved"}]}}
        new_location = {"name": "Moved", "location": {"id": variables["newLocationId"]}}
        requested = {item["id"]: item["quantity"] for item in variables.get("fulfillmentOrderLineItems") or []}
        lines = fulfillment_order["lineItems"]["nodes"]
        if not requested or all(requested.get(line["id"], 0) >= line["remainingQuantity"]
                                for line in lines if line["remainingQuantity"] > 0):
            fulfillment_order["assignedLocation"] = new_location
            moved = fulfillment_order
            remaining = None
        else:
            moved_lines = []
            for line in lines:
                quantity = min(requested.get(line["id"], 0), line["remainingQuantity"])
                if quantity > 0:
                    line["remainingQuantity"] -= quantity
                    line["totalQuantity"] -= quantity
                    moved_lines.append({"id": self.next_gid("FulfillmentOrderLineItem"), "totalQuantity": quantity,
                                        "remainingQuantity": quantity, "lineItem": copy.deepcopy(line["lineItem"])})
            moved = copy.deepcopy({k: v for k, v in fulfillment_order.items() if k != "lineItems"})
            moved.update({"id": self.next_gid("FulfillmentOrder"), "assignedLocation": new_location,
                          "lineItems": {"nodes": moved_lines}})
            self.fulfillment_orders[moved["id"]] = moved
            owner = self._order_owning(fulfillment_order)
            if owner:
                owner["_all"]["fulfillmentOrders"].append(moved)
            remaining = {"id": fulfillment_order["id"]}
        return {"fulfillmentOrderMove": {"movedFulfillmentOrder": {
            "id": moved["id"], "status": moved["status"], "assignedLocation": {"location": new_location["location"]}},
            "remainingFulfillmentOrder": remaining, "userErrors": []}}

    def op_EhPreparedForPickup(self, variables):
        errors = []
        ready = []
        for group in (variables.get("input") or {}).get("lineItemsByFulfillmentOrder") or []:
            fulfillment_order = self.fulfillment_orders.get(group["fulfillmentOrderId"])
            method = ((fulfillment_order or {}).get("deliveryMethod") or {}).get("methodType")
            if not fulfillment_order or method != "PICK_UP" or fulfillment_order["status"] not in ("OPEN", "IN_PROGRESS"):
                errors.append({"field": ["input"], "message": "Fulfillment order is not a pickup waiting to be prepared"})
                continue
            ready.append(group["fulfillmentOrderId"])
        if not errors:
            self.pickup_ready.extend(ready)
        return {"fulfillmentOrderLineItemsPreparedForPickup": {"userErrors": errors}}

    def add_product(self, product):
        self.products[product["id"]] = product
        return product

    def op_EhProductsUpdated(self, variables):
        match = re.search(r"updated_at:>='([^']+)'", variables.get("query") or "")
        nodes = [{"id": p["id"], "updatedAt": p.get("updatedAt")}
                 for p in sorted(self.products.values(), key=lambda p: (p.get("updatedAt") or "", p["id"]))
                 if not match or (p.get("updatedAt") or "") >= match.group(1)]
        return {"products": self._page(nodes, variables)}

    def op_EhProduct(self, variables):
        product = self.products.get(variables["id"])
        if not product:
            return {"product": None}
        view = copy.deepcopy({k: v for k, v in product.items() if k != "_variants"})
        variants = product.get("_variants") or []
        view["variantsCount"] = {"count": len(variants)}
        view["variants"] = self._page(variants, {"first": 50})
        return {"product": view}

    def op_EhProductVariants(self, variables):
        product = self.products.get(variables["id"]) or {}
        return {"node": {"variants": self._page(product.get("_variants") or [], variables)}}

    def op_EhBulkRunQuery(self, variables):
        self.bulk_queries.append(variables.get("query"))
        operation = {"id": self.next_gid("BulkOperation"), "status": "CREATED", "errorCode": None,
                     "objectCount": "0", "rootObjectCount": "0", "fileSize": None, "url": None,
                     "partialDataUrl": None, "createdAt": "2026-09-16T08:00:00Z", "completedAt": None}
        self.bulk_operations[operation["id"]] = operation
        return {"bulkOperationRunQuery": {"bulkOperation": {"id": operation["id"], "status": "CREATED",
                                                            "createdAt": operation["createdAt"]}, "userErrors": []}}

    def op_EhBulkStatus(self, variables):
        return {"bulkOperation": copy.deepcopy(self.bulk_operations.get(variables["id"]))}

    def finish_bulk(self, operation_id, rows, status="COMPLETED", partial=False, error_code=None):
        url = "https://storage.example.com/bulk/%s.jsonl" % operation_id.rsplit("/", 1)[-1]
        self.bulk_results[url] = rows
        roots = [row for row in rows if "__parentId" not in row]
        self.bulk_operations[operation_id].update({
            "status": status, "errorCode": error_code, "objectCount": str(len(rows)),
            "rootObjectCount": str(len(roots)), "fileSize": "1024",
            "url": None if partial or status != "COMPLETED" else url, "partialDataUrl": url if partial else None,
            "completedAt": "2026-09-16T08:05:00Z"})
        return url

    def op_EhProductUpdate(self, variables):
        data = variables.get("product") or {}
        self.product_updates.append(copy.deepcopy(data))
        product = self.products.get(data.get("id"))
        if not product:
            return {"productUpdate": {"product": None, "userErrors": [
                {"field": ["id"], "message": "Product does not exist"}]}}
        for key in ("title", "status", "descriptionHtml", "vendor", "productType"):
            if key in data:
                product[key] = data[key]
        return {"productUpdate": {"product": {"id": product["id"], "title": product.get("title"),
                                              "status": product.get("status"), "updatedAt": "2026-09-16T09:00:00Z"},
                                  "userErrors": []}}

    def op_EhVariantsBulkUpdate(self, variables):
        self.variant_updates.append(copy.deepcopy(variables))
        product = self.products.get(variables.get("productId"))
        if not product:
            return {"productVariantsBulkUpdate": {"productVariants": None, "userErrors": [
                {"field": ["productId"], "message": "Product does not exist", "code": "PRODUCT_DOES_NOT_EXIST"}]}}
        by_id = {variant["id"]: variant for variant in product.get("_variants") or []}
        errors, results = [], []
        for index, entry in enumerate(variables.get("variants") or []):
            variant = by_id.get(entry.get("id"))
            if not variant or entry.get("id") in self.variant_update_refusals:
                errors.append({"field": ["variants", str(index), "price"], "code": "INVALID_INPUT",
                               "message": self.variant_update_refusals.get(entry.get("id"), "Variant does not exist")})
                continue
            if "price" in entry:
                variant["price"] = entry["price"]
            if "barcode" in entry:
                variant["barcode"] = entry["barcode"]
            if "barcodes" in entry:
                variant["barcode"] = entry["barcodes"][0]["value"] if entry["barcodes"] else None
            item = entry.get("inventoryItem") or {}
            if "sku" in item:
                variant["sku"] = item["sku"]
            for key in ("harmonizedSystemCode", "measurement"):
                if key in item:
                    variant["inventoryItem"][key] = copy.deepcopy(item[key])
            results.append({"id": variant["id"], "price": variant["price"], "sku": variant.get("sku"),
                            "updatedAt": "2026-09-16T09:00:00Z"})
        return {"productVariantsBulkUpdate": {"productVariants": results, "userErrors": errors}}

    def op_EhProductSet(self, variables):
        data = copy.deepcopy(variables.get("input") or {})
        self.product_sets.append(copy.deepcopy(data))
        handle = data.get("handle")
        if any(product.get("handle") == handle for product in self.products.values()):
            return {"productSet": {"product": None, "userErrors": [
                {"field": ["input", "handle"], "message": "Handle has already been taken", "code": "HANDLE_NOT_UNIQUE"}]}}
        number = 900 + len(self.products)
        gid = "gid://shopify/Product/%s" % number
        variants = []
        for index, entry in enumerate(data.get("variants") or []):
            item = entry.get("inventoryItem") or {}
            barcode = entry.get("barcode") or ((entry.get("barcodes") or [{}])[0].get("value"))
            options = [{"name": value["optionName"], "value": value["name"]} for value in entry.get("optionValues") or []]
            variants.append({
                "id": "gid://shopify/ProductVariant/%s%03d" % (number, index), "legacyResourceId": "%s%03d" % (number, index),
                "title": " / ".join(option["value"] for option in options), "sku": item.get("sku"), "barcode": barcode,
                "price": entry.get("price") or "0.00", "compareAtPrice": None, "position": index + 1, "taxable": True,
                "inventoryPolicy": entry.get("inventoryPolicy") or "DENY", "updatedAt": "2026-09-16T10:00:00Z",
                "selectedOptions": options,
                "inventoryItem": {"id": "gid://shopify/InventoryItem/%s%03d" % (number, index),
                                  "tracked": bool(item.get("tracked")), "requiresShipping": True,
                                  "harmonizedSystemCode": item.get("harmonizedSystemCode"), "countryCodeOfOrigin": None,
                                  "unitCost": None, "measurement": item.get("measurement") or {"weight": None}}})
        default = len(variants) == 1 and variants[0]["selectedOptions"] == [{"name": "Title", "value": "Default Title"}]
        self.products[gid] = {
            "id": gid, "legacyResourceId": str(number), "title": data.get("title"), "handle": handle,
            "status": data.get("status") or "DRAFT", "descriptionHtml": data.get("descriptionHtml"), "vendor": None,
            "productType": None, "tags": [], "templateSuffix": None, "createdAt": "2026-09-16T10:00:00Z",
            "updatedAt": "2026-09-16T10:00:00Z", "hasOnlyDefaultVariant": default, "isGiftCard": False,
            "category": None, "seo": {"title": None, "description": None},
            "options": [{"id": "gid://shopify/ProductOption/%s%s" % (number, position), "name": option["name"],
                         "position": position + 1, "optionValues": []}
                        for position, option in enumerate(data.get("productOptions") or [])],
            "_variants": variants}
        return {"productSet": {"product": {"id": gid, "handle": handle, "variants": {"nodes": [
            {"id": variant["id"], "sku": variant["sku"], "selectedOptions": variant["selectedOptions"],
             "inventoryItem": {"id": variant["inventoryItem"]["id"]}} for variant in variants]}}, "userErrors": []}}

    def op_EhProductSetAsync(self, variables):
        result = self.op_EhProductSet(variables)["productSet"]
        if result["userErrors"]:
            return {"productSet": {"productSetOperation": None, "userErrors": result["userErrors"]}}
        gid = self.next_gid("ProductSetOperation")
        self.product_operations[gid] = {"id": gid, "status": "CREATED", "userErrors": [],
                                        "product": {"id": result["product"]["id"], "handle": result["product"]["handle"]}}
        return {"productSet": {"productSetOperation": {"id": gid, "status": "CREATED", "userErrors": []},
                               "userErrors": []}}

    def op_EhProductOperation(self, variables):
        operation = self.product_operations.get(variables["id"])
        if not operation:
            return {"productOperation": None}
        view = copy.deepcopy(operation)
        if view["status"] != "COMPLETE" or view["userErrors"]:
            view["product"] = None
        return {"productOperation": view}

    def op_EhProductByHandle(self, variables):
        for product in self.products.values():
            if product.get("handle") == variables.get("handle"):
                return {"productByIdentifier": {"id": product["id"], "handle": product["handle"]}}
        return {"productByIdentifier": None}

    def op_EhVariantsBySku(self, variables):
        match = re.search(r'sku:"?([^"]+)"?', variables.get("query") or "")
        sku = match.group(1) if match else None
        nodes = [{"id": variant["id"], "sku": variant.get("sku"), "product": {"id": product["id"]}}
                 for product in self.products.values() for variant in product.get("_variants") or []
                 if sku and variant.get("sku") == sku]
        return {"productVariants": {"nodes": nodes[:5]}}

    def receive_upload(self, url, filename, content):
        target = self.staged.get(url)
        if not target:
            return 403
        self.uploads[target["resourceUrl"]] = {"filename": filename, "content": content}
        return 201

    def op_EhStagedUploads(self, variables):
        targets = []
        for item in variables.get("input") or []:
            number = len(self.staged) + 1
            target = {"url": "https://upload.example.com/staged/%s" % number,
                      "resourceUrl": "https://upload.example.com/resources/%s/%s" % (number, item["filename"]),
                      "parameters": [{"name": "key", "value": "staged-%s" % number}]}
            self.staged[target["url"]] = target
            targets.append(target)
        return {"stagedUploadsCreate": {"stagedTargets": targets, "userErrors": []}}

    def op_EhFileCreate(self, variables):
        created = []
        for index, item in enumerate(variables.get("files") or []):
            upload = self.uploads.get(item.get("originalSource"))
            if not upload:
                return {"fileCreate": {"files": None, "userErrors": [
                    {"field": ["files", str(index), "originalSource"], "message": "File was not uploaded", "code": "INVALID"}]}}
            gid = self.next_gid("MediaImage")
            self.files[gid] = {"id": gid, "filename": item.get("filename") or upload["filename"], "alt": item.get("alt"),
                               "fileStatus": "READY", "references": []}
            created.append({"id": gid, "fileStatus": "READY"})
        return {"fileCreate": {"files": created, "userErrors": []}}

    def op_EhFileUpdate(self, variables):
        self.file_updates.append(copy.deepcopy(variables))
        updated = []
        for index, item in enumerate(variables.get("files") or []):
            record = self.files.get(item.get("id"))
            if not record:
                return {"fileUpdate": {"files": None, "userErrors": [
                    {"field": ["files", str(index), "id"], "message": "File does not exist", "code": "FILE_DOES_NOT_EXIST"}]}}
            for reference in item.get("referencesToAdd") or []:
                record["references"].append(reference)
                product = self.products.get(reference)
                if product is not None and not product.get("featuredMedia"):
                    product["featuredMedia"] = {"id": record["id"], "image": {
                        "url": "https://cdn.shopify.com/s/files/%s" % record["filename"]}}
            updated.append({"id": record["id"], "fileStatus": record["fileStatus"]})
        return {"fileUpdate": {"files": updated, "userErrors": []}}

    def op_EhFilesByName(self, variables):
        match = re.search(r'filename:"?([^"\s]+)"?', variables.get("query") or "")
        name = match.group(1) if match else None
        nodes = [{"id": record["id"], "fileStatus": record["fileStatus"]}
                 for record in self.files.values() if name and record["filename"] == name]
        return {"files": {"nodes": nodes[:1]}}

    def op_EhFileStatus(self, variables):
        record = self.files.get(variables.get("id"))
        return {"node": {"id": record["id"], "fileStatus": record["fileStatus"]} if record else None}

    def op_EhRefund(self, variables):
        for order in self.orders.values():
            for refund in (order.get("_all") or {}).get("refunds") or []:
                if refund["id"] != variables["id"]:
                    continue
                node = {key: copy.deepcopy(value) for key, value in refund.items()
                        if key not in ("lines", "shipping", "adjustments", "transactions")}
                node["order"] = {"id": order["id"]}
                node.setdefault("return", None)
                node.setdefault("duties", [])
                node["refundLineItems"] = self._page(refund.get("lines") or [], {"first": 50})
                node["refundShippingLines"] = self._page(refund.get("shipping") or [], {"first": 10})
                node["orderAdjustments"] = self._page(refund.get("adjustments") or [], {"first": 10})
                node["transactions"] = self._page(refund.get("transactions") or [], {"first": 20})
                return {"node": node}
        return {"node": None}

    @staticmethod
    def _money_bag(value, currency):
        amount = {"amount": "%.2f" % value, "currencyCode": currency}
        return {"shopMoney": dict(amount), "presentmentMoney": dict(amount)}

    def op_EhSuggestRefund(self, variables):
        order = self.orders.get(variables["id"])
        if not order:
            return {"order": None}
        currency = order.get("currencyCode") or "USD"
        items = {item["id"]: item for item in order["_all"]["lineItems"]}
        amount = sum(float(items[line["lineItemId"]]["originalUnitPriceSet"]["shopMoney"]["amount"]) * line["quantity"]
                     for line in variables.get("lines") or [] if line["lineItemId"] in items)
        shipping = float(variables.get("shippingAmount") or 0)
        amount += shipping
        paid = sum(float(t["amountSet"]["shopMoney"]["amount"]) for t in order["transactions"]
                   if t["kind"] in ("SALE", "CAPTURE") and t["status"] == "SUCCESS")
        refunded = sum(float(t["amountSet"]["shopMoney"]["amount"]) for t in order["transactions"] if t["kind"] == "REFUND")
        maximum = max(0.0, paid - refunded)
        parent = next((t for t in order["transactions"] if t["kind"] in ("SALE", "CAPTURE")), None)
        suggested = min(amount, maximum)
        bag = self._money_bag
        return {"order": {"id": order["id"], "suggestedRefund": {
            "amountSet": bag(suggested, currency), "maximumRefundableSet": bag(maximum, currency),
            "subtotalSet": bag(amount, currency), "totalTaxSet": bag(0.0, currency),
            "shipping": {"amountSet": bag(shipping, currency), "maximumRefundableSet": bag(shipping, currency)},
            "suggestedTransactions": [{"gateway": parent["gateway"], "kind": "SUGGESTED_REFUND",
                                       "parentTransaction": {"id": parent["id"]},
                                       "amountSet": bag(suggested, currency), "maximumRefundableSet": bag(maximum, currency)}]
            if parent else []}}}

    def op_EhRefundCreate(self, variables):
        key = variables.get("idempotencyKey")
        if key and key in self.refund_keys:
            return copy.deepcopy(self.refund_keys[key])
        data = variables.get("input") or {}
        order = self.orders.get(data.get("orderId"))
        if not order:
            return {"refundCreate": {"refund": None, "userErrors": [{"field": ["orderId"], "message": "Order does not exist"}]}}
        if self.refund_refusal:
            return {"refundCreate": {"refund": None, "userErrors": [{"field": ["transactions"], "message": self.refund_refusal}]}}
        self.refund_creates.append(copy.deepcopy(data))
        number = 800 + len(self.refund_creates)
        currency = order.get("currencyCode") or "USD"
        bag = self._money_bag
        stamp = "2026-09-16T11:00:00Z"
        transactions = [{"id": "gid://shopify/OrderTransaction/%s%s" % (number, index), "kind": "REFUND",
                         "status": "SUCCESS", "gateway": item["gateway"], "manualPaymentGateway": False, "test": False,
                         "processedAt": stamp, "createdAt": stamp, "amountSet": bag(float(item["amount"]), currency),
                         "parentTransaction": {"id": item.get("parentId")}}
                        for index, item in enumerate(data.get("transactions") or [])]
        total = sum(float(item["amount"]) for item in data.get("transactions") or [])
        refund = {"id": "gid://shopify/Refund/%s" % number, "legacyResourceId": str(number), "createdAt": stamp,
                  "processedAt": stamp, "note": data.get("note"), "totalRefundedSet": bag(total, currency),
                  "lines": [{"id": "gid://shopify/RefundLineItem/%s%s" % (number, index), "quantity": line["quantity"],
                             "restockType": line.get("restockType") or "NO_RESTOCK", "restocked": False, "location": None,
                             "lineItem": {"id": line["lineItemId"]}, "priceSet": bag(0.0, currency),
                             "subtotalSet": bag(0.0, currency), "totalTaxSet": bag(0.0, currency)}
                            for index, line in enumerate(data.get("refundLineItems") or [])],
                  "shipping": [], "duties": [], "adjustments": [], "transactions": transactions}
        order["_all"].setdefault("refunds", []).append(refund)
        order["transactions"].extend(transactions)
        order["transactionsCount"] = {"count": len(order["transactions"])}
        response = {"refundCreate": {"refund": {"id": refund["id"], "legacyResourceId": refund["legacyResourceId"],
                                                "note": refund["note"], "totalRefundedSet": refund["totalRefundedSet"]},
                                     "userErrors": []}}
        if key:
            self.refund_keys[key] = copy.deepcopy(response)
        return response

    def _return_parts(self, record):
        lines = [{"id": line["id"], "quantity": line["quantity"], "processedQuantity": line.get("processedQuantity", 0),
                  "fulfillmentLineItem": {"lineItem": {"id": line["lineItemId"]}}} for line in record.get("lines") or []]
        reverse_lines = [{"id": "%s-rfo-%s" % (record["id"], index), "totalQuantity": line["quantity"],
                          "dispositions": copy.deepcopy(line.get("dispositions") or []),
                          "fulfillmentLineItem": {"lineItem": {"id": line["lineItemId"]}}}
                         for index, line in enumerate(record.get("lines") or [])]
        return lines, reverse_lines

    def op_EhReturn(self, variables):
        record = self.returns.get(variables["id"])
        if not record:
            return {"node": None}
        size = self.return_page_size
        lines, reverse_lines = self._return_parts(record)
        return {"node": {
            "id": record["id"], "name": record.get("name"), "status": record["status"],
            "totalQuantity": sum(line["quantity"] for line in record.get("lines") or []),
            "order": {"id": record["orderId"]},
            "returnLineItems": self._page(lines, {"first": size}),
            "reverseFulfillmentOrders": self._page([{"id": record["id"] + "-rfo", "status": "OPEN",
                                                     "lineItems": self._page(reverse_lines, {"first": size})}],
                                                   {"first": 10}),
            "exchangeLineItems": self._page(copy.deepcopy(record.get("exchanges") or []), {"first": size})}}

    def op_EhReturnLineItems(self, variables):
        record = self.returns.get(variables["id"]) or {}
        return {"node": {"returnLineItems": self._page(self._return_parts(record)[0] if record else [], variables)}}

    def op_EhReturnReverseOrders(self, variables):
        record = self.returns.get(variables["id"]) or {}
        reverse_lines = self._return_parts(record)[1] if record else []
        return {"node": {"reverseFulfillmentOrders": self._page(
            [{"id": variables["id"] + "-rfo", "status": "OPEN",
              "lineItems": self._page(reverse_lines, {"first": self.return_page_size})}] if record else [], variables)}}

    def op_EhReverseOrderLines(self, variables):
        record = self.returns.get(variables["id"][:-len("-rfo")]) or {}
        return {"node": {"lineItems": self._page(self._return_parts(record)[1] if record else [], variables)}}

    def op_EhReturnExchangeLines(self, variables):
        record = self.returns.get(variables["id"]) or {}
        return {"node": {"exchangeLineItems": self._page(copy.deepcopy(record.get("exchanges") or []), variables)}}

    def add_payout(self, number, status, issued, transactions, currency="USD", net=None):
        """``transactions``: (type, amount, fee, source order transaction id or None, order gid or None)."""
        rows = []
        for index, (kind, amount, fee, source, order_gid) in enumerate(transactions):
            order = self.orders.get(order_gid) or {}
            money = lambda value: {"amount": "%.2f" % value, "currencyCode": currency}
            rows.append({"id": "gid://shopify/ShopifyPaymentsBalanceTransaction/%s%02d" % (number, index), "type": kind,
                         "sourceType": "CHARGE" if kind == "CHARGE" else kind, "sourceId": None,
                         "sourceOrderTransactionId": int(source) if source else None, "adjustmentReason": None,
                         "transactionDate": issued, "test": False, "amount": money(amount), "fee": money(fee),
                         "net": money(amount - fee), "associatedOrder": {"id": order_gid, "name": order.get("name")}
                         if order_gid else None,
                         "associatedPayout": {"id": "gid://shopify/ShopifyPaymentsPayout/%s" % number, "status": status},
                         "_payout": number})
        self.balance_transactions.extend(rows)
        total = sum(amount - fee for _kind, amount, fee, _source, _order in transactions)
        zero = {"amount": "0.00"}
        payout = {"id": "gid://shopify/ShopifyPaymentsPayout/%s" % number, "legacyResourceId": str(number),
                  "status": status, "issuedAt": issued, "transactionType": "DEPOSIT", "externalTraceId": None,
                  "net": {"amount": "%.2f" % (total if net is None else net), "currencyCode": currency},
                  "summary": {key: dict(zero) for key in (
                      "chargesGross", "chargesFee", "refundsFeeGross", "refundsFee", "adjustmentsGross",
                      "adjustmentsFee", "reservedFundsGross", "reservedFundsFee", "retriedPayoutsGross",
                      "retriedPayoutsFee", "advanceGross", "advanceFees")}}
        payout["summary"]["chargesFee"] = {"amount": "%.2f" % sum(fee for _k, _a, fee, _s, _o in transactions)}
        self.payouts.append(payout)
        return payout

    def op_EhPayouts(self, variables):
        if self.payments_account is None:
            return {"shopifyPaymentsAccount": None}
        match = re.search(r"issued_at:>='([^']+)'", variables.get("query") or "")
        nodes = [copy.deepcopy(payout) for payout in sorted(self.payouts, key=lambda payout: payout["issuedAt"])
                 if not match or payout["issuedAt"][:10] >= match.group(1)]
        return {"shopifyPaymentsAccount": dict(self.payments_account, payouts=self._page(nodes, variables))}

    def op_EhPayoutTransactions(self, variables):
        if self.payments_account is None:
            return {"shopifyPaymentsAccount": None}
        match = re.search(r"payments_transfer_id:(\d+)", variables.get("query") or "")
        nodes = [copy.deepcopy({key: value for key, value in row.items() if not key.startswith("_")})
                 for row in self.balance_transactions if not match or str(row["_payout"]) == match.group(1)]
        return {"shopifyPaymentsAccount": dict(self.payments_account, balanceTransactions=self._page(nodes, variables))}

    def add_dispute(self, number, order_gid, status, due, initiated, amount=10.0, currency="USD"):
        order = self.orders.get(order_gid) or {}
        gid = "gid://shopify/ShopifyPaymentsDispute/%s" % number
        self.disputes[gid] = {
            "id": gid, "legacyResourceId": str(number), "status": status, "type": "CHARGEBACK",
            "initiatedAt": initiated, "evidenceDueBy": due, "evidenceSentOn": None, "finalizedOn": None,
            "amount": {"amount": "%.2f" % amount, "currencyCode": currency},
            "reasonDetails": {"reason": "FRAUDULENT", "networkReasonCode": "4837"},
            "order": {"id": order_gid, "legacyResourceId": str(order.get("legacyResourceId") or ""),
                      "name": order.get("name")} if order_gid else None}
        return self.disputes[gid]

    def op_EhDisputes(self, variables):
        match = re.search(r"initiated_at:>='([^']+)'", variables.get("query") or "")
        nodes = [copy.deepcopy(dispute) for dispute in sorted(self.disputes.values(), key=lambda d: d["initiatedAt"])
                 if not match or dispute["initiatedAt"][:10] >= match.group(1)]
        return {"disputes": self._page(nodes, variables)}

    def op_EhDispute(self, variables):
        return {"dispute": copy.deepcopy(self.disputes.get(variables["id"]))}

    @staticmethod
    def _digest(value):
        import hashlib
        return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:16]

    def set_metafield(self, owner, namespace, key, kind, value):
        full = "%s.%s" % (namespace, key)
        owner.setdefault("_metafields", {})[full] = {
            "id": "gid://shopify/Metafield/%s" % self._digest(owner["id"] + full), "namespace": namespace, "key": key,
            "type": kind, "value": value, "compareDigest": self._digest(value)}
        return owner["_metafields"][full]

    def _metafield_owner(self, gid):
        for owners in (self.products, self.customers, self.orders):
            if gid in owners:
                return owners[gid]
        for product in self.products.values():
            for variant in product.get("_variants") or []:
                if variant["id"] == gid:
                    return variant
        return None

    @staticmethod
    def _metafield_nodes(owner, keys):
        values = (owner or {}).get("_metafields") or {}
        return [copy.deepcopy(node) for full, node in values.items() if not keys or full in keys]

    def op_EhProductMetafields(self, variables):
        product = self.products.get(variables["id"])
        if not product:
            return {"product": None}
        return {"product": {"id": product["id"], "metafields": {
            "nodes": self._metafield_nodes(product, variables.get("keys"))}}}

    def op_EhCustomerMetafields(self, variables):
        owner = self.customers.get(variables["id"])
        return {"customer": {"id": owner["id"], "metafields": {"nodes": self._metafield_nodes(owner, variables.get("keys"))}}
                if owner else None}

    def op_EhOrderMetafields(self, variables):
        owner = self.orders.get(variables["id"])
        return {"order": {"id": owner["id"], "metafields": {"nodes": self._metafield_nodes(owner, variables.get("keys"))}}
                if owner else None}

    def op_EhVariantMetafields(self, variables):
        nodes = []
        for gid in variables["ids"]:
            owner = self._metafield_owner(gid)
            nodes.append({"id": gid, "metafields": {"nodes": self._metafield_nodes(owner, variables.get("keys"))}}
                         if owner else None)
        return {"nodes": nodes}

    def op_EhMetafieldsSet(self, variables):
        entries = variables.get("metafields") or []
        self.metafield_sets.append(copy.deepcopy(entries))
        problems, saved = [], []
        for index, entry in enumerate(entries):
            owner = self._metafield_owner(entry["ownerId"])
            full = "%s.%s" % (entry["namespace"], entry["key"])
            current = ((owner or {}).get("_metafields") or {}).get(full)
            if self.metafield_force_stale or (current and entry.get("compareDigest") is not None
                                              and entry["compareDigest"] != current["compareDigest"]):
                problems.append({"field": ["metafields", str(index)], "code": "STALE_OBJECT", "elementIndex": index,
                                 "message": "The resource has been updated since it was loaded."})
                continue
            node = self.set_metafield(owner, entry["namespace"], entry["key"], entry["type"], entry["value"])
            saved.append({"id": node["id"], "namespace": node["namespace"], "key": node["key"],
                          "compareDigest": node["compareDigest"]})
        return {"metafieldsSet": {"metafields": saved if not problems else [], "userErrors": problems}}

    def op_EhOrderEditBegin(self, variables):
        if self.order_edit_begin_timeout:
            self.order_edit_begin_timeout = False
            raise TransportReadTimeout("order edit begin timed out")
        order = self.orders.get(variables["id"])
        if not order:
            return {"orderEditBegin": {"calculatedOrder": None,
                                       "userErrors": [{"field": ["id"], "message": "The order does not exist."}]}}
        calc_id = "gid://shopify/CalculatedOrder/%s" % variables["id"].rsplit("/", 1)[-1]
        self.order_edit_sessions[calc_id] = {"order": variables["id"], "set": {}, "add": []}
        nodes = [{"id": "gid://shopify/CalculatedLineItem/%s" % item["id"].rsplit("/", 1)[-1],
                  "quantity": item.get("currentQuantity", item["quantity"]),
                  "editableQuantity": item.get("currentQuantity", item["quantity"]),
                  "variant": {"id": (item.get("variant") or {}).get("id")}} for item in order["_all"]["lineItems"]]
        return {"orderEditBegin": {"calculatedOrder": {"id": calc_id, "lineItems": {"nodes": nodes}}, "userErrors": []}}

    def op_EhOrderEditSetQuantity(self, variables):
        if self.order_edit_refuse:
            return {"orderEditSetQuantity": {"calculatedLineItem": None, "userErrors": [
                {"field": ["quantity"], "message": "This line item cannot be edited."}]}}
        session = self.order_edit_sessions[variables["id"]]
        session["set"][variables["lineItemId"].rsplit("/", 1)[-1]] = variables["quantity"]
        return {"orderEditSetQuantity": {"calculatedLineItem": {"id": variables["lineItemId"],
                                                                "quantity": variables["quantity"]}, "userErrors": []}}

    def op_EhOrderEditAddVariant(self, variables):
        if self.order_edit_refuse:
            return {"orderEditAddVariant": {"calculatedLineItem": None, "userErrors": [
                {"field": ["variantId"], "message": "This variant cannot be added."}]}}
        session = self.order_edit_sessions[variables["id"]]
        session["add"].append((variables["variantId"], variables["quantity"]))
        return {"orderEditAddVariant": {"calculatedLineItem": {"id": "gid://shopify/CalculatedLineItem/added%s"
                                                               % len(session["add"]), "quantity": variables["quantity"]},
                                        "userErrors": []}}

    def op_EhOrderEditCommit(self, variables):
        session = self.order_edit_sessions.pop(variables["id"])
        order = self.orders[session["order"]]
        items = order["_all"]["lineItems"]
        for item in items:
            number = item["id"].rsplit("/", 1)[-1]
            if number in session["set"]:
                # quantity is what was ordered and stays above currentQuantity by the units refunded or removed.
                item["quantity"] = item["quantity"] + session["set"][number] - item["currentQuantity"]
                item["currentQuantity"] = session["set"][number]
        template = items[0]
        for variant_gid, quantity in session["add"]:
            suffix = variant_gid.rsplit("/", 1)[-1]
            added = copy.deepcopy(template)
            added.update({"id": "gid://shopify/LineItem/%s9%02d" % (order["id"].rsplit("/", 1)[-1], len(items)),
                          "sku": "SKU-%s" % suffix, "name": "Item %s" % suffix, "quantity": quantity,
                          "currentQuantity": quantity, "discountAllocations": [], "taxLines": [], "duties": [],
                          "variant": {"id": variant_gid, "sku": "SKU-%s" % suffix, "barcode": None,
                                      "inventoryItem": {"id": "gid://shopify/InventoryItem/%s" % suffix}},
                          "product": {"id": "gid://shopify/Product/%s" % suffix}})
            items.append(added)
        total = sum(float(item["originalUnitPriceSet"]["shopMoney"]["amount"]) * item["currentQuantity"] for item in items)
        for key in ("currentTotalPriceSet", "totalPriceSet"):
            for money in order[key].values():
                money["amount"] = "%.2f" % total
        self.order_edits.append(copy.deepcopy(session))
        order["updatedAt"] = "2026-09-16T12:%02d:00Z" % len(self.order_edits)
        if self.order_edit_commit_lost:
            self.order_edit_commit_lost = False
            raise TransportReadTimeout("order edit commit answer lost")
        return {"orderEditCommit": {"order": {"id": order["id"], "lineItems": {"nodes": [
            {"id": item["id"], "currentQuantity": item["currentQuantity"], "variant": {"id": item["variant"]["id"]}}
            for item in items]}}, "userErrors": []}}

    def add_gift_card(self, number, balance, currency="USD", order_gid=None, enabled=True, transactions=None,
                      order_created="2026-01-01T00:00:00Z"):
        """``transactions``: (kind, amount, processed at) with kind credit or debit."""
        card = {"id": "gid://shopify/GiftCard/%s" % number, "lastCharacters": "%04d" % number, "enabled": enabled,
                "createdAt": "2026-01-01T00:00:00Z", "expiresOn": None,
                "balance": {"amount": "%.2f" % balance, "currencyCode": currency},
                "order": {"id": order_gid, "createdAt": order_created} if order_gid else None,
                "_transactions": [{"__typename": "GiftCardDebitTransaction" if kind == "debit"
                                   else "GiftCardCreditTransaction", "processedAt": at,
                                   "amount": {"amount": "%.2f" % (-amount if kind == "debit" else amount),
                                              "currencyCode": currency}}
                                  for kind, amount, at in transactions or []]}
        self.gift_cards.append(card)
        return card

    def op_EhGiftCards(self, variables):
        enabled_only = "status:enabled" in (variables.get("query") or "")
        nodes = []
        for card in self.gift_cards:
            if card["enabled"] or not enabled_only:
                node = copy.deepcopy({key: value for key, value in card.items() if not key.startswith("_")})
                node["transactions"] = self._page(copy.deepcopy(card["_transactions"]), {"first": 25})
                nodes.append(node)
        return {"giftCards": self._page(nodes, variables)}

    def op_EhGiftCardTransactions(self, variables):
        card = next((card for card in self.gift_cards if card["id"] == variables["id"]), None)
        return {"node": {"transactions": self._page(copy.deepcopy(card["_transactions"]) if card else [], variables)}}

    def add_price_list(self, number, name, currency="USD"):
        gid = "gid://shopify/PriceList/%s" % number
        self.price_lists[gid] = {"id": gid, "name": name, "currency": currency, "fixedPricesCount": 0,
                                 "catalog": {"id": "gid://shopify/MarketCatalog/%s" % number,
                                             "title": "%s catalog" % name, "status": "ACTIVE"},
                                 "_prices": {}, "_breaks": {}}
        return self.price_lists[gid]

    def op_EhPriceLists(self, variables):
        nodes = [dict({key: value for key, value in price_list.items() if not key.startswith("_")},
                      fixedPricesCount=len(price_list["_prices"]))
                 for price_list in sorted(self.price_lists.values(), key=lambda price_list: price_list["name"])]
        return {"priceLists": self._page(copy.deepcopy(nodes), variables)}

    def op_EhPriceListFixedPrices(self, variables):
        price_list = self.price_lists.get(variables["id"])
        if not price_list:
            return {"priceList": None}
        nodes = []
        for gid, entry in sorted(price_list["_prices"].items()):
            breaks = [{"id": "gid://shopify/QuantityPriceBreak/" + gid.rsplit("/", 1)[-1] + "-" + str(quantity),
                       "minimumQuantity": quantity, "price": dict(money)}
                      for quantity, money in sorted((price_list["_breaks"].get(gid) or {}).items())]
            nodes.append({"variant": {"id": gid}, "price": dict(entry["price"]),
                          "compareAtPrice": dict(entry["compareAtPrice"]) if entry.get("compareAtPrice") else None,
                          "quantityPriceBreaks": self._page(breaks, {"first": 10})})
        return {"priceList": {"id": price_list["id"], "prices": self._page(nodes, variables)}}

    def op_EhPriceListFixedPricesUpdate(self, variables):
        self.price_list_updates.append(copy.deepcopy(variables))
        price_list = self.price_lists.get(variables["priceListId"])
        empty = {"pricesAdded": [], "deletedFixedPriceVariantIds": []}
        if not price_list:
            return {"priceListFixedPricesUpdate": dict(empty, userErrors=[
                {"field": ["priceListId"], "message": "Price list does not exist", "code": "PRICE_LIST_NOT_FOUND"}])}
        for entry in variables.get("pricesToAdd") or []:
            if entry["price"]["currencyCode"] != price_list["currency"]:
                return {"priceListFixedPricesUpdate": dict(empty, userErrors=[
                    {"field": ["pricesToAdd"], "message": "Currency does not match the price list",
                     "code": "PRICES_TO_ADD_CURRENCY_MISMATCH"}])}
        added = []
        for entry in variables.get("pricesToAdd") or []:
            price_list["_prices"][entry["variantId"]] = {
                "price": dict(entry["price"]),
                "compareAtPrice": dict(entry["compareAtPrice"]) if entry.get("compareAtPrice") else None}
            added.append({"variant": {"id": entry["variantId"]}})
        for gid in variables.get("variantIdsToDelete") or []:
            price_list["_prices"].pop(gid, None)
            price_list["_breaks"].pop(gid, None)
        return {"priceListFixedPricesUpdate": {"pricesAdded": added,
                                               "deletedFixedPriceVariantIds": list(variables.get("variantIdsToDelete") or []),
                                               "userErrors": []}}

    def op_EhQuantityPricingUpdate(self, variables):
        self.quantity_pricing_updates.append(copy.deepcopy(variables))
        price_list = self.price_lists.get(variables["priceListId"])
        if not price_list:
            return {"quantityPricingByVariantUpdate": {"productVariants": [], "userErrors": [
                {"field": ["priceListId"], "message": "Price list does not exist", "code": "PRICE_LIST_NOT_FOUND"}]}}
        data = variables.get("input") or {}
        for gid in data.get("quantityPriceBreaksToDeleteByVariantId") or []:
            price_list["_breaks"].pop(gid, None)
        touched = []
        for entry in data.get("quantityPriceBreaksToAdd") or []:
            gid = entry["variantId"]
            price_list["_breaks"].setdefault(gid, {})[int(entry["minimumQuantity"])] = dict(entry["price"])
            touched.append({"id": gid})
        return {"quantityPricingByVariantUpdate": {"productVariants": touched, "userErrors": []}}

    def op_EhCustomer(self, variables):
        return {"customer": copy.deepcopy(self.customers.get(variables["id"]))}

    def op_EhCompany(self, variables):
        return {"company": copy.deepcopy(self.companies.get(variables["id"]))}

    def op_EhOrderReturnIds(self, variables):
        nodes = [{"id": record["id"], "status": record["status"]} for record in self.returns.values()
                 if record.get("orderId") == variables["id"]]
        return {"order": {"id": variables["id"], "returns": {"nodes": nodes}}}

    def op_EhOrderState(self, variables):
        order = self.orders.get(variables["id"])
        if not order:
            return {"order": None}
        view = {key: order.get(key) for key in ("id", "name", "cancelledAt", "displayFulfillmentStatus",
                                                 "displayFinancialStatus")}
        view["netPaymentSet"] = copy.deepcopy(order.get("netPaymentSet") or order.get("currentTotalPriceSet"))
        return {"order": view}

    def op_EhOrderCancel(self, variables):
        self.cancel_requests.append(copy.deepcopy(variables))
        order = self.orders.get(variables["orderId"])
        if self.cancel_errors:
            return {"orderCancel": {"job": None, "orderCancelUserErrors": copy.deepcopy(self.cancel_errors)}}
        if not order:
            return {"orderCancel": {"job": None, "orderCancelUserErrors": [
                {"field": ["orderId"], "message": "Order does not exist", "code": "NOT_FOUND"}]}}
        if order.get("cancelledAt"):
            return {"orderCancel": {"job": None, "orderCancelUserErrors": [
                {"field": ["orderId"], "message": "Order has already been cancelled", "code": "INVALID"}]}}
        order["cancelledAt"] = "2026-09-16T08:00:00Z"
        order["updatedAt"] = "2026-09-16T08:00:00Z"
        return {"orderCancel": {"job": {"id": self.next_gid("Job"), "done": False}, "orderCancelUserErrors": []}}

    # ---------------------------------------------------------------- inventory
    def op_EhInventorySet(self, variables):
        data = variables.get("input") or {}
        if not variables.get("idempotencyKey"):
            return {"inventorySetQuantities": {"inventoryAdjustmentGroup": None, "userErrors": [
                {"field": ["idempotencyKey"], "message": "An idempotency key is required", "code": "INVALID"}]}}
        errors = []
        changes = []
        for index, quantity in enumerate(data.get("quantities") or []):
            key = (quantity["inventoryItemId"], quantity["locationId"])
            if key not in self.inventory:
                errors.append({"field": ["input", "quantities", str(index), "locationId"],
                               "message": "The item is not stocked at the location",
                               "code": self.inventory_error_codes["not_stocked"]})
                continue
            current = self.inventory[key]
            if quantity.get("changeFromQuantity") is not None and quantity["changeFromQuantity"] != current:
                errors.append({"field": ["input", "quantities", str(index), "changeFromQuantity"],
                               "message": "The quantity changed since it was read",
                               "code": self.inventory_error_codes["stale"]})
                continue
            changes.append((key, quantity["quantity"], current))
        if errors:
            return {"inventorySetQuantities": {"inventoryAdjustmentGroup": None, "userErrors": errors}}
        for key, target, current in changes:
            self.inventory[key] = target
        return {"inventorySetQuantities": {"inventoryAdjustmentGroup": {
            "id": self.next_gid("InventoryAdjustmentGroup"), "reason": data.get("reason"),
            "changes": [{"name": data.get("name"), "delta": target - current, "quantityAfterChange": target,
                         "item": {"id": key[0]}, "location": {"id": key[1]}} for key, target, current in changes]},
            "userErrors": []}}

    def op_EhInventoryActivate(self, variables):
        key = (variables["inventoryItemId"], variables["locationId"])
        self.inventory.setdefault(key, 0)
        return {"inventoryActivate": {"inventoryLevel": {"id": self.next_gid("InventoryLevel")}, "userErrors": []}}

    def op_EhInventoryLevels(self, variables):
        nodes = []
        for item in variables.get("ids") or []:
            if item in self.missing_items:
                nodes.append(None)
                continue
            key = (item, variables["locationId"])
            level = None
            if key in self.inventory:
                committed = self.inventory_committed.get(key, 0)
                reserved = self.inventory_reserved.get(key, 0)
                level = {"id": "level:%s" % item, "quantities": [
                    {"name": "available", "quantity": self.inventory[key]},
                    {"name": "on_hand", "quantity": self.inventory[key] + committed + reserved},
                    {"name": "committed", "quantity": committed},
                    {"name": "reserved", "quantity": reserved}]}
            nodes.append({"id": item, "sku": None, "tracked": item not in self.untracked_items,
                          "inventoryLevel": level})
        hook, self.after_inventory_read = self.after_inventory_read, None
        if hook:
            hook()
        return {"nodes": nodes}

    # ---------------------------------------------------------------- fulfillment
    def op_EhFulfillmentCreate(self, variables):
        data = variables.get("fulfillment") or {}
        errors = []
        planned = []
        for group in data.get("lineItemsByFulfillmentOrder") or []:
            fulfillment_order = self.fulfillment_orders.get(group["fulfillmentOrderId"])
            if not fulfillment_order:
                errors.append({"field": ["fulfillment"], "message": "Fulfillment order does not exist"})
                continue
            if fulfillment_order["status"] not in ("OPEN", "IN_PROGRESS"):
                errors.append({"field": ["fulfillment"], "message": "Fulfillment order is not fulfillable"})
                continue
            owed = {line["id"]: line for line in fulfillment_order["lineItems"]["nodes"]}
            for requested in group.get("fulfillmentOrderLineItems") or []:
                line = owed.get(requested["id"])
                if not line or requested["quantity"] > line["remainingQuantity"]:
                    errors.append({"field": ["fulfillment"], "message": "Quantity exceeds what is owed"})
                    continue
                planned.append((fulfillment_order, line, requested["quantity"]))
        if errors:
            return {"fulfillmentCreate": {"fulfillment": None, "userErrors": errors}}
        for fulfillment_order, line, quantity in planned:
            line["remainingQuantity"] -= quantity
            if all(l["remainingQuantity"] == 0 for l in fulfillment_order["lineItems"]["nodes"]):
                fulfillment_order["status"] = "CLOSED"
            else:
                fulfillment_order["status"] = "IN_PROGRESS"
        tracking = data.get("trackingInfo") or {}
        record = {"id": self.next_gid("Fulfillment"), "name": "#F%s" % (len(self.fulfillments) + 1),
                  "status": "SUCCESS", "notifyCustomer": bool(data.get("notifyCustomer")),
                  "trackingInfo": [{"company": tracking.get("company"), "number": number, "url": None}
                                   for number in (tracking.get("numbers") or [])]}
        self.fulfillments.append(record)
        return {"fulfillmentCreate": {"fulfillment": {k: v for k, v in record.items() if k != "notifyCustomer"},
                                      "userErrors": []}}

    # ---------------------------------------------------------------- token endpoint
    def token_endpoint(self, data):
        if data.get("client_id") != self.client_id or data.get("client_secret") != self.client_secret:
            return 401, json.dumps({"error": "invalid_client", "error_description": "bad client"})
        grant = data.get("grant_type")
        if grant == "client_credentials":
            if not self.same_organisation:
                return 400, "Oauth error shop_not_permitted: Client credentials cannot be performed on this shop."
            token = "cc_%s" % (len(self.issued) + 1)
            self.issued.append(token)
            return 200, json.dumps({"access_token": token, "scope": ",".join(self.scopes),
                                    "expires_in": self.token_lifetime})
        if grant == "refresh_token":
            if data.get("refresh_token") not in self.refresh_tokens:
                return 400, json.dumps({"error": "invalid_grant"})
            self.refresh_tokens.pop(data["refresh_token"])
            return 200, json.dumps(self._code_token())
        code = data.get("code")
        if code and self.codes.pop(code, None):
            return 200, json.dumps(self._code_token(expiring=data.get("expiring") == "1"))
        return 400, json.dumps({"error": "invalid_grant"})

    def _code_token(self, expiring=True):
        token = "ac_%s" % (len(self.issued) + 1)
        self.issued.append(token)
        body = {"access_token": token, "scope": ",".join(self.scopes)}
        if expiring:
            refresh = "shprt_%s" % (len(self.issued))
            self.refresh_tokens[refresh] = token
            body.update({"expires_in": 3600, "refresh_token": refresh, "refresh_token_expires_in": 7776000})
        return body


class FakeTransport(object):
    def __init__(self, shop=None, api_version="2026-07"):
        self.shop = shop or FakeShop()
        self.api_version = api_version
        self.requests = []
        self._script = []
        self.available = 1990.0
        self.waits = []

    def fake_sleep(self, seconds):
        self.waits.append(seconds)

    def script(self, *steps):
        """Queue scripted outcomes consumed before normal handling. A step is
        an exception instance, or a tuple (status, headers, body_dict_or_list),
        or the string 'throttled'."""
        self._script.extend(steps)

    def post_form(self, url, data, timeout=(10, 30)):
        self.requests.append({"url": url, "form": dict(data), "timeout": timeout})
        if self._script:
            step = self._script.pop(0)
            if isinstance(step, Exception):
                raise step
            status, _headers, response_body = step
            return status, response_body if isinstance(response_body, str) else json.dumps(response_body)
        return self.shop.token_endpoint(data)

    def stream_lines(self, url, timeout):
        for line in self.shop.bulk_results.get(url, []):
            yield line if isinstance(line, str) else json.dumps(line)

    def download_to(self, url, handle, offset, timeout, deadline=None, clock=None):
        rows = self.shop.bulk_results.get(url, [])
        content = "".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows).encode("utf-8")
        self.requests.append({"url": url, "download_from": offset})
        if self.shop.bulk_cuts.get(url):
            self.shop.bulk_cuts[url] -= 1
            handle.seek(0)
            handle.truncate()
            handle.write(content[:content.rfind(b"\n", 0, max(1, len(content) // 2)) + 1])
            return {"complete": True, "restart": bool(offset), "total": None}
        piece = content[offset:]
        if self.shop.bulk_download_limit is not None:
            piece = piece[:self.shop.bulk_download_limit]
        handle.write(piece)
        return {"complete": offset + len(piece) >= len(content), "restart": False, "total": len(content)}

    def upload_multipart(self, url, fields, filename, content, mimetype, timeout=(10, 120)):
        self.requests.append({"url": url, "upload": filename, "fields": list(fields), "size": len(content)})
        return self.shop.receive_upload(url, filename, content)

    def get_bytes(self, url, timeout=(10, 60), max_bytes=20 * 1024 * 1024):
        from . import errors as lib_errors
        self.requests.append({"url": url, "download": True})
        content = self.shop.downloads.get(url)
        if content is None:
            raise lib_errors.QueryError("Image download failed with HTTP 404")
        return content

    def post(self, url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        self.requests.append({"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout})
        step = self._script.pop(0) if self._script else None
        if step is not None:
            if isinstance(step, (TransportConnectError, TransportReadTimeout)):
                raise step
            if step == "throttled":
                return 200, CIDict({"X-Shopify-API-Version": self.api_version}), json.dumps({
                    "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                    "extensions": {"cost": throttle_status(available=0.0, requested=50, actual=0)},
                }).encode("utf-8")
            status, extra_headers, response_body = step
            response_headers = CIDict({"X-Shopify-API-Version": self.api_version})
            for key, value in (extra_headers or {}).items():
                response_headers[key] = value
            raw = json.dumps(response_body).encode("utf-8") if not isinstance(response_body, bytes) else response_body
            return status, response_headers, raw
        match = _OP_RE.search(payload["query"])
        name = match.group(2) if match else None
        handler = getattr(self.shop, "op_%s" % name, None)
        if handler is None:
            return 200, CIDict({"X-Shopify-API-Version": self.api_version}), json.dumps({
                "errors": [{"message": "FakeShop has no handler for %s" % name,
                            "extensions": {"code": "undefinedField"}}]}).encode("utf-8")
        data = handler(payload.get("variables") or {})
        return 200, CIDict({"X-Shopify-API-Version": self.api_version}), json.dumps({
            "data": data, "extensions": {"cost": throttle_status(available=self.available)}}).encode("utf-8")
