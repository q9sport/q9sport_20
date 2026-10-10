# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Every GraphQL document the connector sends, in one place.

Keeping documents in a registry lets ``tools/shopify_contract_check.py``
validate each one against every supported Admin API version before release,
and lets the client pick a version specific variant where the schema drifted.
"""
import re

from . import versions

_GATE_RE = re.compile(r"^[ \t]*# @requires ([a-z_, ]+)\n(.*?)^[ \t]*# @end[ \t]*\n", re.S | re.M)


def scope_granted(scope, granted):
    """Shopify grants read access with the matching write scope."""
    if scope in granted:
        return True
    return scope.startswith("read_") and ("write_" + scope[len("read_"):]) in granted


def apply_scope_gates(text, granted):
    """Keep ``# @requires scope`` blocks only when every listed scope is granted.
    ``granted=None`` keeps everything (contract checks, tests)."""
    def replace(match):
        needed = [s.strip() for s in match.group(1).split(",") if s.strip()]
        if granted is None or all(scope_granted(s, granted) for s in needed):
            return match.group(2)
        return ""
    return _GATE_RE.sub(replace, text)


class Document(object):
    def __init__(self, name, text, payload_key=None, connection=None, min_version=versions.FLOOR,
                 idempotent=False, variants=None, sample_variables=None, scopes=(), bulk=False, page_size=250):
        self.page_size = page_size
        self.name = name
        self.text = text.strip()
        self.payload_key = payload_key
        self.connection = connection
        self.min_version = min_version
        self.idempotent = idempotent
        self.variants = dict(variants or {})
        self.sample_variables = sample_variables or {}
        self.scopes = tuple(scopes)
        self.bulk = bulk

    @property
    def kind(self):
        return "mutation" if self.text.lstrip().startswith("mutation") else "query"

    def text_for(self, api_version, scopes=None):
        chosen = self.text
        chosen_from = None
        for since, text in self.variants.items():
            if versions._key(api_version) >= versions._key(since):
                if chosen_from is None or versions._key(since) > versions._key(chosen_from):
                    chosen, chosen_from = text.strip(), since
        return apply_scope_gates(chosen + "\n", scopes).strip()

    def gated_scopes(self):
        found = set()
        for match in _GATE_RE.finditer(self.text + "\n"):
            found.update(s.strip() for s in match.group(1).split(",") if s.strip())
        return found


REGISTRY = {}


def register(document):
    if document.name in REGISTRY:
        raise ValueError("Duplicate GraphQL document %s" % document.name)
    REGISTRY[document.name] = document
    return document


def get(name):
    return REGISTRY[name]


SHOP_INFO = register(Document(
    "EhShopInfo",
    """
query EhShopInfo {
  shop {
    id
    name
    email
    myshopifyDomain
    primaryDomain { host }
    currencyCode
    enabledPresentmentCurrencies
    taxesIncluded
    taxShipping
    ianaTimezone
    weightUnit
    plan { publicDisplayName partnerDevelopment shopifyPlus }
    billingAddress { countryCodeV2 }
  }
}
""",
    scopes=(),
))

APP_INSTALLATION = register(Document(
    "EhAppInstallation",
    """
query EhAppInstallation {
  currentAppInstallation {
    id
    accessScopes { handle }
    app { id title handle }
  }
}
""",
))

LOCATIONS = register(Document(
    "EhLocations",
    """
query EhLocations($first: Int!, $after: String) {
  locations(first: $first, after: $after, includeInactive: true, includeLegacy: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      legacyResourceId
      name
      isActive
      fulfillsOnlineOrders
      hasActiveInventory
      shipsInventory
      isFulfillmentService
      fulfillmentService { handle serviceName }
      address { address1 address2 city zip provinceCode countryCode phone }
    }
  }
}
""",
    connection=("locations",),
    sample_variables={"first": 5},
    scopes=("read_locations",),
    page_size=100
))

WEBHOOK_SUBSCRIPTIONS = register(Document(
    "EhWebhookSubscriptions",
    """
query EhWebhookSubscriptions($first: Int!, $after: String) {
  webhookSubscriptions(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      topic
      uri
      format
      filter
      includeFields
      apiVersion { handle }
      createdAt
      updatedAt
    }
  }
}
""",
    connection=("webhookSubscriptions",),
    sample_variables={"first": 5},
    page_size=100
))

WEBHOOK_CREATE = register(Document(
    "EhWebhookSubscriptionCreate",
    """
mutation EhWebhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $subscription: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $subscription) {
    webhookSubscription { id topic uri format apiVersion { handle } }
    userErrors { field message }
  }
}
""",
    payload_key="webhookSubscriptionCreate",
    sample_variables={"topic": "ORDERS_CREATE",
                      "subscription": {"uri": "https://example.com/eh_shopify/webhook/x", "format": "JSON"}},
))

WEBHOOK_DELETE = register(Document(
    "EhWebhookSubscriptionDelete",
    """
mutation EhWebhookSubscriptionDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors { field message }
  }
}
""",
    payload_key="webhookSubscriptionDelete",
    sample_variables={"id": "gid://shopify/WebhookSubscription/1"},
))

BULK_RUN_QUERY = register(Document(
    "EhBulkRunQuery",
    """
mutation EhBulkRunQuery($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id status createdAt }
    userErrors { field message code }
  }
}
""",
    payload_key="bulkOperationRunQuery",
    sample_variables={"query": "{ products { edges { node { id } } } }"},
))

BULK_STATUS = register(Document(
    "EhBulkStatus",
    """
query EhBulkStatus($id: ID!) {
  bulkOperation(id: $id) {
    id
    status
    errorCode
    objectCount
    rootObjectCount
    fileSize
    url
    partialDataUrl
    createdAt
    completedAt
  }
}
""",
    min_version="2026-01",
    sample_variables={"id": "gid://shopify/BulkOperation/1"},
))

BULK_CANCEL = register(Document(
    "EhBulkCancel",
    """
mutation EhBulkCancel($id: ID!) {
  bulkOperationCancel(id: $id) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
""",
    payload_key="bulkOperationCancel",
    sample_variables={"id": "gid://shopify/BulkOperation/1"},
))


# ---------------------------------------------------------------------------
# Orders (Phase 1)
# ---------------------------------------------------------------------------
MONEY_FRAGMENTS = """
fragment EhBag on MoneyBag { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
fragment EhTax on TaxLine { title rate ratePercentage channelLiable source priceSet { ...EhBag } }
fragment EhAllocation on DiscountAllocation { allocatedAmountSet { ...EhBag } discountApplication { index } }
"""

ADDRESS_FRAGMENT = """
fragment EhAddress on MailingAddress {
  firstName lastName name company address1 address2 city zip province provinceCode countryCodeV2 phone
}
"""

LINE_FRAGMENT = """
fragment EhLine on LineItem {
  id sku name title variantTitle vendor quantity currentQuantity refundableQuantity nonFulfillableQuantity
  unfulfilledQuantity requiresShipping taxable isGiftCard restockable
  variant { id legacyResourceId sku barcode inventoryItem { id } }
  product { id legacyResourceId }
  originalUnitPriceSet { ...EhBag }
  discountedUnitPriceAfterAllDiscountsSet { ...EhBag }
  totalDiscountSet { ...EhBag }
  taxLines { ...EhTax }
  discountAllocations { ...EhAllocation }
  duties { id harmonizedSystemCode countryCodeOfOrigin price { ...EhBag } taxLines { ...EhTax } }
  customAttributes { key value }
  lineItemGroup { id title quantity variantSku productId }
}
"""

DISCOUNT_FRAGMENT = """
fragment EhDiscountApplication on DiscountApplication {
  __typename index allocationMethod targetSelection targetType
  value { __typename ... on MoneyV2 { amount currencyCode } ... on PricingPercentageValue { percentage } }
  ... on DiscountCodeApplication { code }
  ... on AutomaticDiscountApplication { title }
  ... on ManualDiscountApplication { title description }
  ... on ScriptDiscountApplication { title }
}
"""

FULFILLMENT_ORDER_FRAGMENT = """
fragment EhFulfillmentOrder on FulfillmentOrder {
  id status requestStatus fulfillAt fulfillBy updatedAt
  assignedLocation { name location { id } }
  deliveryMethod { methodType serviceCode presentedName }
  fulfillmentHolds { reason reasonNotes }
  supportedActions { action }
  lineItems(first: 50) {
    pageInfo { hasNextPage endCursor }
    nodes { id totalQuantity remainingQuantity lineItem { id } }
  }
}
"""

ORDERS_UPDATED = register(Document(
    "EhOrdersUpdated",
    """
query EhOrdersUpdated($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, sortKey: UPDATED_AT, query: $query) {
    pageInfo { hasNextPage endCursor }
    nodes { id name updatedAt displayFinancialStatus cancelledAt test }
  }
}
""",
    connection=("orders",),
    sample_variables={"first": 50, "query": "updated_at:>='2026-09-01T00:00:00Z'"},
    scopes=("read_orders",),
    page_size=250
))

ORDER = register(Document(
    "EhOrder",
    """
query EhOrder($id: ID!) {
  order(id: $id) {
    id legacyResourceId name number confirmationNumber
    createdAt processedAt updatedAt cancelledAt closedAt cancelReason
    cancellation { staffNote }
    test confirmed closed edited
    displayFinancialStatus displayFulfillmentStatus returnStatus
    sourceName sourceIdentifier poNumber note tags email phone
    currencyCode presentmentCurrencyCode taxesIncluded taxExempt dutiesIncluded estimatedTaxes customerLocale
    paymentGatewayNames discountCodes
    customAttributes { key value }
    originalTotalPriceSet { ...EhBag }
    totalPriceSet { ...EhBag }
    currentTotalPriceSet { ...EhBag }
    currentSubtotalPriceSet { ...EhBag }
    currentTotalTaxSet { ...EhBag }
    currentTotalDiscountsSet { ...EhBag }
    currentShippingPriceSet { ...EhBag }
    currentTotalDutiesSet { ...EhBag }
    currentTotalAdditionalFeesSet { ...EhBag }
    totalTipReceivedSet { ...EhBag }
    totalRefundedSet { ...EhBag }
    totalOutstandingSet { ...EhBag }
    totalCashRoundingAdjustment { paymentSet { ...EhBag } refundSet { ...EhBag } }
    currentTaxLines { ...EhTax }
    additionalFees { id name price { ...EhBag } taxLines { ...EhTax } }
    customer {
      id legacyResourceId firstName lastName displayName locale taxExempt tags note verifiedEmail state
      defaultEmailAddress { emailAddress marketingState }
      defaultPhoneNumber { phoneNumber marketingState }
    }
    billingAddress { ...EhAddress }
    shippingAddress { ...EhAddress }
    purchasingEntity {
      __typename
      ... on PurchasingCompany {
        company { id name externalId }
        location {
          id name externalId
          taxSettings { taxRegistrationId taxExempt }
          billingAddress { firstName lastName recipient companyName address1 address2 city zip zoneCode countryCode phone }
          shippingAddress { firstName lastName recipient companyName address1 address2 city zip zoneCode countryCode phone }
        }
        contact { id customer { id } }
      }
    }
    # @requires read_payment_terms
    paymentTerms { paymentTermsName paymentTermsType dueInDays }
    # @end
    risk { recommendation assessments { riskLevel facts { description sentiment } } }
    retailLocation { id name }
    customerJourneySummary {
      ready
      firstVisit { source sourceType referrerUrl landingPage utmParameters { campaign source medium content term } }
      lastVisit { source sourceType utmParameters { campaign source medium content term } }
    }
    discountApplications(first: 20) {
      pageInfo { hasNextPage endCursor }
      nodes { ...EhDiscountApplication }
    }
    shippingLines(first: 10, includeRemovals: true) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id title code source carrierIdentifier deliveryCategory isRemoved custom
        originalPriceSet { ...EhBag }
        discountedPriceSet { ...EhBag }
        taxLines { ...EhTax }
        discountAllocations { ...EhAllocation }
      }
    }
    lineItems(first: 20) {
      pageInfo { hasNextPage endCursor }
      nodes { ...EhLine }
    }
    transactionsCount { count }
    transactions(first: 100) {
      id kind status gateway formattedGateway manualPaymentGateway test processedAt createdAt paymentId
      authorizationExpiresAt settlementCurrency settlementCurrencyRate errorCode
      amountSet { ...EhBag }
      parentTransaction { id }
      fees { type rate amount { amount currencyCode } flatFee { amount currencyCode } taxAmount { amount currencyCode } }
    }
    fulfillmentOrders(first: 10) {
      pageInfo { hasNextPage endCursor }
      nodes { ...EhFulfillmentOrder }
    }
  }
}
""" + MONEY_FRAGMENTS + ADDRESS_FRAGMENT + LINE_FRAGMENT + DISCOUNT_FRAGMENT + FULFILLMENT_ORDER_FRAGMENT,
    sample_variables={"id": "gid://shopify/Order/1"},
    scopes=("read_orders",),
))

ORDER_LINE_ITEMS = register(Document(
    "EhOrderLineItems",
    """
query EhOrderLineItems($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Order {
      lineItems(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...EhLine }
      }
    }
  }
}
""" + MONEY_FRAGMENTS + LINE_FRAGMENT,
    connection=("node", "lineItems"),
    sample_variables={"id": "gid://shopify/Order/1", "first": 50},
    scopes=("read_orders",),
    page_size=50
))

ORDER_DISCOUNT_APPLICATIONS = register(Document(
    "EhOrderDiscountApplications",
    """
query EhOrderDiscountApplications($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Order {
      discountApplications(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...EhDiscountApplication }
      }
    }
  }
}
""" + DISCOUNT_FRAGMENT,
    connection=("node", "discountApplications"),
    sample_variables={"id": "gid://shopify/Order/1", "first": 100},
    scopes=("read_orders",),
    page_size=100
))

ORDER_SHIPPING_LINES = register(Document(
    "EhOrderShippingLines",
    """
query EhOrderShippingLines($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Order {
      shippingLines(first: $first, after: $after, includeRemovals: true) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id title code source carrierIdentifier deliveryCategory isRemoved custom
          originalPriceSet { ...EhBag }
          discountedPriceSet { ...EhBag }
          taxLines { ...EhTax }
          discountAllocations { ...EhAllocation }
        }
      }
    }
  }
}
""" + MONEY_FRAGMENTS,
    connection=("node", "shippingLines"),
    sample_variables={"id": "gid://shopify/Order/1", "first": 50},
    scopes=("read_orders",),
    page_size=50
))

ORDER_FULFILLMENT_ORDERS = register(Document(
    "EhOrderFulfillmentOrders",
    """
query EhOrderFulfillmentOrders($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Order {
      fulfillmentOrders(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...EhFulfillmentOrder }
      }
    }
  }
}
""" + FULFILLMENT_ORDER_FRAGMENT,
    connection=("node", "fulfillmentOrders"),
    sample_variables={"id": "gid://shopify/Order/1", "first": 10},
    scopes=("read_merchant_managed_fulfillment_orders",),
    page_size=25
))

FULFILLMENT_ORDER_LINE_ITEMS = register(Document(
    "EhFulfillmentOrderLineItems",
    """
query EhFulfillmentOrderLineItems($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on FulfillmentOrder {
      lineItems(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id totalQuantity remainingQuantity lineItem { id } }
      }
    }
  }
}
""",
    connection=("node", "lineItems"),
    sample_variables={"id": "gid://shopify/FulfillmentOrder/1", "first": 250},
    scopes=("read_merchant_managed_fulfillment_orders",),
    page_size=100
))

# ---------------------------------------------------------------------------
# Catalogue (Phase 2)
# ---------------------------------------------------------------------------
PRODUCT_VARIANT_FRAGMENT = """
fragment EhProductVariant on ProductVariant {
  id legacyResourceId title sku barcode price compareAtPrice position taxable inventoryPolicy updatedAt
  selectedOptions { name value }
  inventoryItem {
    id tracked requiresShipping harmonizedSystemCode countryCodeOfOrigin
    unitCost { amount currencyCode }
    measurement { weight { unit value } }
  }
}
"""
# ProductVariant.barcode is deprecated from 2026-10 in favour of the barcodes connection.
PRODUCT_VARIANT_FRAGMENT_2026_10 = PRODUCT_VARIANT_FRAGMENT.replace(
    " barcode price", " barcodes(first: 5) { nodes { type value } } price")

PRODUCTS_UPDATED = register(Document(
    "EhProductsUpdated",
    """
query EhProductsUpdated($first: Int!, $after: String, $query: String) {
  products(first: $first, after: $after, sortKey: UPDATED_AT, query: $query) {
    pageInfo { hasNextPage endCursor }
    nodes { id updatedAt }
  }
}
""",
    connection=("products",),
    sample_variables={"first": 50, "query": "updated_at:>='2026-01-01T00:00:00Z'"},
    scopes=("read_products",),
    page_size=250,
))

PRODUCT_TEXT = """
query EhProduct($id: ID!) {
  product(id: $id) {
    id legacyResourceId title handle status descriptionHtml vendor productType tags templateSuffix
    createdAt updatedAt hasOnlyDefaultVariant isGiftCard
    category { id fullName }
    seo { title description }
    options(first: 3) { id name position optionValues { id name hasVariants } }
    featuredMedia { id ... on MediaImage { image { url } } }
    variantsCount { count }
    variants(first: 50) {
      pageInfo { hasNextPage endCursor }
      nodes { ...EhProductVariant }
    }
  }
}
"""

PRODUCT = register(Document(
    "EhProduct",
    PRODUCT_TEXT + PRODUCT_VARIANT_FRAGMENT,
    variants={"2026-10": PRODUCT_TEXT + PRODUCT_VARIANT_FRAGMENT_2026_10},
    sample_variables={"id": "gid://shopify/Product/1"},
    scopes=("read_products",),
))

PRODUCT_VARIANTS_TEXT = """
query EhProductVariants($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Product {
      variants(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...EhProductVariant }
      }
    }
  }
}
"""

BULK_PRODUCTS_TEXT = """
{
  products {
    edges {
      node {
        id legacyResourceId title handle status descriptionHtml vendor productType tags templateSuffix
        createdAt updatedAt hasOnlyDefaultVariant isGiftCard
        category { id fullName }
        seo { title description }
        options { id name position optionValues { id name hasVariants } }
        featuredMedia { id ... on MediaImage { image { url } } }
        variantsCount { count }
        variants {
          edges {
            node {
              id legacyResourceId title sku barcode price compareAtPrice position taxable inventoryPolicy updatedAt
              selectedOptions { name value }
              inventoryItem {
                id tracked requiresShipping harmonizedSystemCode countryCodeOfOrigin
                unitCost { amount currencyCode }
                measurement { weight { unit value } }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Run through bulkOperationRunQuery, so connections carry no page size. The
# contract check validates it with a small page size added to each connection.
BULK_PRODUCTS = register(Document(
    "EhBulkProducts",
    BULK_PRODUCTS_TEXT,
    scopes=("read_products",),
    bulk=True,
))

# One product through a bulk export, for products with more variants than one
# job can page. The id is substituted for 0 when the export starts.
BULK_ONE_PRODUCT = register(Document(
    "EhBulkOneProduct",
    BULK_PRODUCTS_TEXT.replace("products {", 'products(query: "id:0") {', 1),
    scopes=("read_products",),
    bulk=True,
))

# Order history: only ids are exported, and each order is read through the
# normal order import. The date is substituted when the export starts.
BULK_ORDER_IDS = register(Document(
    "EhBulkOrderIds",
    """
{
  orders(query: "created_at:>='2000-01-01'") {
    edges {
      node { id }
    }
  }
}
""",
    scopes=("read_orders",),
    bulk=True,
))

BULK_CUSTOMERS = register(Document(
    "EhBulkCustomers",
    """
{
  customers {
    edges {
      node {
        id firstName lastName displayName locale taxExempt tags
        defaultEmailAddress { emailAddress marketingState }
        defaultPhoneNumber { phoneNumber marketingState }
        defaultAddress { firstName lastName company address1 address2 city zip provinceCode countryCodeV2 phone }
      }
    }
  }
}
""",
    scopes=("read_customers",),
    bulk=True,
))

BULK_PRODUCT_IDS = register(Document(
    "EhBulkProductIds",
    """
{
  products {
    edges {
      node { id }
    }
  }
}
""",
    scopes=("read_products",),
    bulk=True,
))

PRODUCT_UPDATE = register(Document(
    "EhProductUpdate",
    """
mutation EhProductUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id title status updatedAt }
    userErrors { field message }
  }
}
""",
    payload_key="productUpdate",
    sample_variables={"product": {"id": "gid://shopify/Product/1", "title": "Brass gear"}},
    scopes=("write_products",),
))

VARIANTS_BULK_UPDATE = register(Document(
    "EhVariantsBulkUpdate",
    """
mutation EhVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants, allowPartialUpdates: true) {
    productVariants { id price sku updatedAt }
    userErrors { field message code }
  }
}
""",
    payload_key="productVariantsBulkUpdate",
    sample_variables={"productId": "gid://shopify/Product/1",
                      "variants": [{"id": "gid://shopify/ProductVariant/1", "price": "10.00"}]},
    scopes=("write_products",),
))

PRODUCT_SET = register(Document(
    "EhProductSet",
    """
mutation EhProductSet($input: ProductSetInput!) {
  productSet(input: $input, synchronous: true) {
    product {
      id handle
      variants(first: 100) {
        nodes { id sku selectedOptions { name value } inventoryItem { id } }
      }
    }
    userErrors { field message code }
  }
}
""",
    payload_key="productSet",
    sample_variables={"input": {"title": "Brass gear", "status": "DRAFT",
                                "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
                                "variants": [{"optionValues": [{"optionName": "Title", "name": "Default Title"}],
                                              "price": "10.00"}]}},
    scopes=("write_products",),
))

# Products with more variants than a synchronous productSet handles are created
# in the background; the operation is then read until it completes.
PRODUCT_SET_ASYNC = register(Document(
    "EhProductSetAsync",
    """
mutation EhProductSetAsync($input: ProductSetInput!) {
  productSet(input: $input, synchronous: false) {
    productSetOperation { id status userErrors { field message code } }
    userErrors { field message code }
  }
}
""",
    payload_key="productSet",
    sample_variables={"input": {"title": "Brass gear", "status": "DRAFT",
                                "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
                                "variants": [{"optionValues": [{"optionName": "Title", "name": "Default Title"}],
                                              "price": "10.00"}]}},
    scopes=("write_products",),
))

PRODUCT_OPERATION = register(Document(
    "EhProductOperation",
    """
query EhProductOperation($id: ID!) {
  productOperation(id: $id) {
    ... on ProductSetOperation { id status product { id handle } userErrors { field message code } }
  }
}
""",
    sample_variables={"id": "gid://shopify/ProductSetOperation/1"},
    scopes=("read_products",),
))

PRODUCT_BY_HANDLE = register(Document(
    "EhProductByHandle",
    """
query EhProductByHandle($handle: String!) {
  productByIdentifier(identifier: {handle: $handle}) { id handle }
}
""",
    sample_variables={"handle": "brass-gear"},
    scopes=("read_products",),
))

VARIANTS_BY_SKU = register(Document(
    "EhVariantsBySku",
    """
query EhVariantsBySku($query: String!) {
  productVariants(first: 5, query: $query) {
    nodes { id sku product { id } }
  }
}
""",
    sample_variables={"query": 'sku:"BRASS-1"'},
    scopes=("read_products",),
))

STAGED_UPLOADS = register(Document(
    "EhStagedUploads",
    """
mutation EhStagedUploads($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
""",
    payload_key="stagedUploadsCreate",
    sample_variables={"input": [{"resource": "IMAGE", "filename": "gear.png", "mimeType": "image/png",
                                 "httpMethod": "POST", "fileSize": "1024"}]},
    scopes=("write_files",),
))

FILE_CREATE = register(Document(
    "EhFileCreate",
    """
mutation EhFileCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files { id fileStatus }
    userErrors { field message code }
  }
}
""",
    payload_key="fileCreate",
    sample_variables={"files": [{"originalSource": "https://example.com/gear.png", "contentType": "IMAGE",
                                 "filename": "gear.png", "duplicateResolutionMode": "RAISE_ERROR"}]},
    scopes=("write_files",),
))

FILE_UPDATE = register(Document(
    "EhFileUpdate",
    """
mutation EhFileUpdate($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) {
    files { id fileStatus }
    userErrors { field message code }
  }
}
""",
    payload_key="fileUpdate",
    sample_variables={"files": [{"id": "gid://shopify/MediaImage/1", "referencesToAdd": ["gid://shopify/Product/1"]}]},
    scopes=("write_files",),
))

FILES_BY_NAME = register(Document(
    "EhFilesByName",
    """
query EhFilesByName($query: String!) {
  files(first: 1, query: $query) {
    nodes { id fileStatus }
  }
}
""",
    sample_variables={"query": "filename:gear.png"},
    scopes=("read_files",),
))

FILE_STATUS = register(Document(
    "EhFileStatus",
    """
query EhFileStatus($id: ID!) {
  node(id: $id) {
    ... on MediaImage { id fileStatus }
  }
}
""",
    sample_variables={"id": "gid://shopify/MediaImage/1"},
    scopes=("read_files",),
))

PRODUCT_VARIANTS = register(Document(
    "EhProductVariants",
    PRODUCT_VARIANTS_TEXT + PRODUCT_VARIANT_FRAGMENT,
    variants={"2026-10": PRODUCT_VARIANTS_TEXT + PRODUCT_VARIANT_FRAGMENT_2026_10},
    connection=("node", "variants"),
    sample_variables={"id": "gid://shopify/Product/1", "first": 100},
    scopes=("read_products",),
    page_size=100
))


ORDER_FULFILLMENTS = register(Document(
    "EhOrderFulfillments",
    """
query EhOrderFulfillments($id: ID!) {
  order(id: $id) {
    id
    displayFulfillmentStatus
    fulfillmentsCount { count }
    fulfillments(first: 30) {
      id name status createdAt updatedAt
      location { id }
      trackingInfo(first: 5) { company number url }
      fulfillmentLineItems(first: 10) {
        pageInfo { hasNextPage endCursor }
        nodes { id quantity lineItem { id } }
      }
    }
  }
}
""",
    sample_variables={"id": "gid://shopify/Order/1"},
    scopes=("read_orders",),
))

FULFILLMENT_LINE_ITEMS = register(Document(
    "EhFulfillmentLineItems",
    """
query EhFulfillmentLineItems($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Fulfillment {
      fulfillmentLineItems(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id quantity lineItem { id } }
      }
    }
  }
}
""",
    connection=("node", "fulfillmentLineItems"),
    sample_variables={"id": "gid://shopify/Fulfillment/1", "first": 100},
    scopes=("read_orders",),
    page_size=100
))

FULFILLMENT_CREATE = register(Document(
    "EhFulfillmentCreate",
    """
mutation EhFulfillmentCreate($fulfillment: FulfillmentInput!, $message: String) {
  fulfillmentCreate(fulfillment: $fulfillment, message: $message) {
    fulfillment {
      id name status
      trackingInfo(first: 10) { company number url }
    }
    userErrors { field message }
  }
}
""",
    payload_key="fulfillmentCreate",
    sample_variables={"fulfillment": {
        "notifyCustomer": False,
        "trackingInfo": {"company": "DHL Express", "numbers": ["1234"]},
        "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": "gid://shopify/FulfillmentOrder/1",
                                         "fulfillmentOrderLineItems": [
                                             {"id": "gid://shopify/FulfillmentOrderLineItem/1", "quantity": 1}]}],
    }},
    scopes=("write_merchant_managed_fulfillment_orders",),
))

FULFILLMENT_TRACKING_UPDATE = register(Document(
    "EhFulfillmentTrackingUpdate",
    """
mutation EhFulfillmentTrackingUpdate($fulfillmentId: ID!, $trackingInfoInput: FulfillmentTrackingInput!, $notifyCustomer: Boolean) {
  fulfillmentTrackingInfoUpdate(fulfillmentId: $fulfillmentId, trackingInfoInput: $trackingInfoInput, notifyCustomer: $notifyCustomer) {
    fulfillment { id trackingInfo(first: 10) { company number url } }
    userErrors { field message }
  }
}
""",
    payload_key="fulfillmentTrackingInfoUpdate",
    sample_variables={"fulfillmentId": "gid://shopify/Fulfillment/1",
                      "trackingInfoInput": {"company": "UPS", "numbers": ["1Z"]}, "notifyCustomer": False},
    scopes=("write_merchant_managed_fulfillment_orders",),
))

FULFILLMENT_ORDER_MOVE = register(Document(
    "EhFulfillmentOrderMove",
    """
mutation EhFulfillmentOrderMove($id: ID!, $newLocationId: ID!, $fulfillmentOrderLineItems: [FulfillmentOrderLineItemInput!]) {
  fulfillmentOrderMove(id: $id, newLocationId: $newLocationId, fulfillmentOrderLineItems: $fulfillmentOrderLineItems) {
    movedFulfillmentOrder { id status assignedLocation { location { id } } }
    remainingFulfillmentOrder { id }
    userErrors { field message }
  }
}
""",
    payload_key="fulfillmentOrderMove",
    sample_variables={"id": "gid://shopify/FulfillmentOrder/1", "newLocationId": "gid://shopify/Location/2",
                      "fulfillmentOrderLineItems": [{"id": "gid://shopify/FulfillmentOrderLineItem/1", "quantity": 1}]},
    scopes=("write_merchant_managed_fulfillment_orders",),
))

FULFILLMENT_ORDER_ORDER = register(Document(
    "EhFulfillmentOrderOrder",
    """
query EhFulfillmentOrderOrder($id: ID!) {
  node(id: $id) {
    ... on FulfillmentOrder { id orderId status }
  }
}
""",
    sample_variables={"id": "gid://shopify/FulfillmentOrder/1"},
    scopes=("read_merchant_managed_fulfillment_orders",),
))

FULFILLMENT_ORDER_PREPARED_FOR_PICKUP = register(Document(
    "EhPreparedForPickup",
    """
mutation EhPreparedForPickup($input: FulfillmentOrderLineItemsPreparedForPickupInput!) {
  fulfillmentOrderLineItemsPreparedForPickup(input: $input) {
    userErrors { field message }
  }
}
""",
    payload_key="fulfillmentOrderLineItemsPreparedForPickup",
    sample_variables={"input": {"lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": "gid://shopify/FulfillmentOrder/1"}]}},
    scopes=("write_merchant_managed_fulfillment_orders",),
))

ORDER_STATE = register(Document(
    "EhOrderState",
    """
query EhOrderState($id: ID!) {
  order(id: $id) {
    id name cancelledAt displayFulfillmentStatus displayFinancialStatus
    netPaymentSet { shopMoney { amount currencyCode } }
  }
}
""",
    sample_variables={"id": "gid://shopify/Order/1"},
    scopes=("read_orders",),
))

ORDER_CANCEL = register(Document(
    "EhOrderCancel",
    """
mutation EhOrderCancel($orderId: ID!, $reason: OrderCancelReason!, $restock: Boolean!, $notifyCustomer: Boolean, $staffNote: String, $refundMethod: OrderCancelRefundMethodInput) {
  orderCancel(orderId: $orderId, reason: $reason, restock: $restock, notifyCustomer: $notifyCustomer, staffNote: $staffNote, refundMethod: $refundMethod) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}
""",
    payload_key="orderCancel",
    sample_variables={"orderId": "gid://shopify/Order/1", "reason": "CUSTOMER", "restock": True,
                      "notifyCustomer": False, "refundMethod": {"originalPaymentMethodsRefund": True}},
    scopes=("write_orders",),
))

ORDER_REFUND_IDS = register(Document(
    "EhOrderRefundIds",
    """
query EhOrderRefundIds($id: ID!) {
  order(id: $id) {
    id
    refunds(first: 250) { id createdAt note }
  }
}
""",
    sample_variables={"id": "gid://shopify/Order/1"},
    scopes=("read_orders",),
))

REFUND_BAG_FRAGMENT = """
fragment EhRefundBag on MoneyBag {
  shopMoney { amount currencyCode }
  presentmentMoney { amount currencyCode }
}
"""

REFUND = register(Document(
    "EhRefund",
    """
query EhRefund($id: ID!) {
  node(id: $id) {
    ... on Refund {
      id legacyResourceId createdAt processedAt note
      order { id }
      return { id }
      totalRefundedSet { ...EhRefundBag }
      refundLineItems(first: 50) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id quantity restockType restocked location { id } lineItem { id }
          priceSet { ...EhRefundBag } subtotalSet { ...EhRefundBag } totalTaxSet { ...EhRefundBag }
        }
      }
      refundShippingLines(first: 10) {
        pageInfo { hasNextPage endCursor }
        nodes { id shippingLine { id } subtotalAmountSet { ...EhRefundBag } taxAmountSet { ...EhRefundBag } }
      }
      duties { originalDuty { id } amountSet { ...EhRefundBag } }
      orderAdjustments(first: 10) {
        pageInfo { hasNextPage endCursor }
        nodes { id reason amountSet { ...EhRefundBag } taxAmountSet { ...EhRefundBag } }
      }
      transactions(first: 20) {
        pageInfo { hasNextPage endCursor }
        nodes { id kind status gateway processedAt amountSet { ...EhRefundBag } parentTransaction { id } }
      }
    }
  }
}
""" + REFUND_BAG_FRAGMENT,
    sample_variables={"id": "gid://shopify/Refund/1"},
    scopes=("read_orders",),
))

SUGGEST_REFUND = register(Document(
    "EhSuggestRefund",
    """
query EhSuggestRefund($id: ID!, $lines: [RefundLineItemInput!], $shippingAmount: Money) {
  order(id: $id) {
    id
    suggestedRefund(refundLineItems: $lines, shippingAmount: $shippingAmount) {
      amountSet { ...EhRefundBag }
      maximumRefundableSet { ...EhRefundBag }
      subtotalSet { ...EhRefundBag }
      totalTaxSet { ...EhRefundBag }
      shipping { amountSet { ...EhRefundBag } maximumRefundableSet { ...EhRefundBag } }
      suggestedTransactions {
        gateway kind parentTransaction { id }
        amountSet { ...EhRefundBag } maximumRefundableSet { ...EhRefundBag }
      }
    }
  }
}
""" + REFUND_BAG_FRAGMENT,
    sample_variables={"id": "gid://shopify/Order/1", "lines": []},
    scopes=("read_orders",),
))

REFUND_CREATE = register(Document(
    "EhRefundCreate",
    """
mutation EhRefundCreate($input: RefundInput!, $idempotencyKey: String!) {
  refundCreate(input: $input) @idempotent(key: $idempotencyKey) {
    refund { id legacyResourceId note totalRefundedSet { ...EhRefundBag } }
    userErrors { field message }
  }
}
""" + REFUND_BAG_FRAGMENT,
    payload_key="refundCreate",
    idempotent=True,
    sample_variables={"input": {"orderId": "gid://shopify/Order/1", "note": "Credit note", "notify": False,
                                "transactions": []}},
    scopes=("write_orders",),
))

RETURN_LINE_ITEMS = register(Document(
    "EhReturnLineItems",
    """
query EhReturnLineItems($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Return {
      returnLineItems(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id quantity processedQuantity
          ... on ReturnLineItem { fulfillmentLineItem { lineItem { id } } }
        }
      }
    }
  }
}
""",
    connection=("node", "returnLineItems"),
    sample_variables={"id": "gid://shopify/Return/1", "first": 50},
    scopes=("read_returns",),
    page_size=50,
))

RETURN_REVERSE_ORDERS = register(Document(
    "EhReturnReverseOrders",
    """
query EhReturnReverseOrders($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Return {
      reverseFulfillmentOrders(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id status
          lineItems(first: 50) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id totalQuantity
              dispositions { type quantity location { id } }
              fulfillmentLineItem { lineItem { id } }
            }
          }
        }
      }
    }
  }
}
""",
    connection=("node", "reverseFulfillmentOrders"),
    sample_variables={"id": "gid://shopify/Return/1", "first": 10},
    scopes=("read_returns",),
    page_size=10,
))

REVERSE_ORDER_LINES = register(Document(
    "EhReverseOrderLines",
    """
query EhReverseOrderLines($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on ReverseFulfillmentOrder {
      lineItems(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id totalQuantity
          dispositions { type quantity location { id } }
          fulfillmentLineItem { lineItem { id } }
        }
      }
    }
  }
}
""",
    connection=("node", "lineItems"),
    sample_variables={"id": "gid://shopify/ReverseFulfillmentOrder/1", "first": 50},
    scopes=("read_returns",),
    page_size=50,
))

RETURN_EXCHANGE_LINES = register(Document(
    "EhReturnExchangeLines",
    """
query EhReturnExchangeLines($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Return {
      exchangeLineItems(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id quantity variantId
          lineItems {
            id name sku quantity
            variant { id sku barcode }
            originalUnitPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          }
        }
      }
    }
  }
}
""",
    connection=("node", "exchangeLineItems"),
    sample_variables={"id": "gid://shopify/Return/1", "first": 20},
    scopes=("read_returns",),
    page_size=20,
))

ORDER_RETURN_IDS = register(Document(
    "EhOrderReturnIds",
    """
query EhOrderReturnIds($id: ID!) {
  order(id: $id) {
    id
    returns(first: 50) { nodes { id status } }
  }
}
""",
    sample_variables={"id": "gid://shopify/Order/1"},
    scopes=("read_returns",),
))

RETURN = register(Document(
    "EhReturn",
    """
query EhReturn($id: ID!) {
  node(id: $id) {
    ... on Return {
      id name status totalQuantity
      order { id }
      returnLineItems(first: 50) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id quantity processedQuantity
          ... on ReturnLineItem { fulfillmentLineItem { lineItem { id } } }
        }
      }
      reverseFulfillmentOrders(first: 10) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id status
          lineItems(first: 50) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id totalQuantity
              dispositions { type quantity location { id } }
              fulfillmentLineItem { lineItem { id } }
            }
          }
        }
      }
      exchangeLineItems(first: 20) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id quantity variantId
          lineItems {
            id name sku quantity
            variant { id sku barcode }
            originalUnitPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
          }
        }
      }
    }
  }
}
""",
    sample_variables={"id": "gid://shopify/Return/1"},
    scopes=("read_returns",),
))

REFUND_LINE_ITEMS = register(Document(
    "EhRefundLineItems",
    """
query EhRefundLineItems($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Refund {
      refundLineItems(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id quantity restockType restocked location { id } lineItem { id }
          priceSet { ...EhRefundBag } subtotalSet { ...EhRefundBag } totalTaxSet { ...EhRefundBag }
        }
      }
    }
  }
}
""" + REFUND_BAG_FRAGMENT,
    connection=("node", "refundLineItems"),
    sample_variables={"id": "gid://shopify/Refund/1", "first": 100},
    scopes=("read_orders",),
    page_size=100,
))

TAGS_ADD = register(Document(
    "EhTagsAdd",
    """
mutation EhTagsAdd($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) {
    node { id }
    userErrors { field message }
  }
}
""",
    payload_key="tagsAdd",
    sample_variables={"id": "gid://shopify/Order/1", "tags": ["odoo:S00001"]},
    scopes=("write_orders",),
))

# ---------------------------------------------------------------------------
# Inventory (Phase 1)
# ---------------------------------------------------------------------------
INVENTORY_SET = register(Document(
    "EhInventorySet",
    """
mutation EhInventorySet($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    inventoryAdjustmentGroup {
      id reason
      changes { name delta quantityAfterChange item { id } location { id } }
    }
    userErrors { field message code }
  }
}
""",
    payload_key="inventorySetQuantities",
    idempotent=True,
    sample_variables={"input": {"name": "available", "reason": "correction",
                                "referenceDocumentUri": "gid://erp-heritage/StockSync/1",
                                "quantities": [{"inventoryItemId": "gid://shopify/InventoryItem/1",
                                                "locationId": "gid://shopify/Location/1",
                                                "quantity": 5, "changeFromQuantity": 4}]}},
    scopes=("write_inventory",),
))

INVENTORY_ACTIVATE = register(Document(
    "EhInventoryActivate",
    """
mutation EhInventoryActivate($inventoryItemId: ID!, $locationId: ID!, $idempotencyKey: String!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) @idempotent(key: $idempotencyKey) {
    inventoryLevel { id }
    userErrors { field message }
  }
}
""",
    payload_key="inventoryActivate",
    idempotent=True,
    sample_variables={"inventoryItemId": "gid://shopify/InventoryItem/1", "locationId": "gid://shopify/Location/1"},
    scopes=("write_inventory",),
))

INVENTORY_LEVELS = register(Document(
    "EhInventoryLevels",
    """
query EhInventoryLevels($ids: [ID!]!, $locationId: ID!) {
  nodes(ids: $ids) {
    ... on InventoryItem {
      id sku tracked
      inventoryLevel(locationId: $locationId) {
        id
        quantities(names: ["available", "on_hand", "committed", "reserved"]) { name quantity }
      }
    }
  }
}
""",
    sample_variables={"ids": ["gid://shopify/InventoryItem/1"], "locationId": "gid://shopify/Location/1"},
    scopes=("read_inventory",),
))


# Shopify Payments. Payouts have no webhook topic, so they are polled. The
# balance transactions of one payout are filtered by its legacy id.
PAYOUTS = register(Document(
    "EhPayouts",
    """
query EhPayouts($first: Int!, $after: String, $query: String) {
  shopifyPaymentsAccount {
    id
    defaultCurrency
    payouts(first: $first, after: $after, query: $query, sortKey: ISSUED_AT) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id legacyResourceId status issuedAt transactionType externalTraceId
        net { amount currencyCode }
        summary {
          chargesGross { amount } chargesFee { amount } refundsFeeGross { amount } refundsFee { amount }
          adjustmentsGross { amount } adjustmentsFee { amount } reservedFundsGross { amount } reservedFundsFee { amount }
          retriedPayoutsGross { amount } retriedPayoutsFee { amount } advanceGross { amount } advanceFees { amount }
        }
      }
    }
  }
}
""",
    connection=("shopifyPaymentsAccount", "payouts"),
    sample_variables={"first": 50, "query": "issued_at:>='2026-01-01'"},
    scopes=("read_shopify_payments_payouts",),
    page_size=100,
))

PAYOUT_TRANSACTIONS = register(Document(
    "EhPayoutTransactions",
    """
query EhPayoutTransactions($first: Int!, $after: String, $query: String) {
  shopifyPaymentsAccount {
    id
    balanceTransactions(first: $first, after: $after, query: $query, sortKey: PROCESSED_AT, hideTransfers: true) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id type sourceType sourceId sourceOrderTransactionId adjustmentReason transactionDate test
        amount { amount currencyCode }
        fee { amount currencyCode }
        net { amount currencyCode }
        associatedOrder { id name }
        associatedPayout { id status }
      }
    }
  }
}
""",
    connection=("shopifyPaymentsAccount", "balanceTransactions"),
    sample_variables={"first": 50, "query": "payments_transfer_id:1"},
    scopes=("read_shopify_payments_payouts",),
    page_size=250,
))

DISPUTE_FRAGMENT = """
fragment EhDisputeFields on ShopifyPaymentsDispute {
  id legacyResourceId status type initiatedAt evidenceDueBy evidenceSentOn finalizedOn
  amount { amount currencyCode }
  reasonDetails { reason networkReasonCode }
  order { id legacyResourceId name }
}
"""

# evidenceDueBy, evidenceSentOn and finalizedOn are dates on 2026-01 and date
# times from 2026-04; both are read as dates.
DISPUTES = register(Document(
    "EhDisputes",
    """
query EhDisputes($first: Int!, $after: String, $query: String) {
  disputes(first: $first, after: $after, query: $query) {
    pageInfo { hasNextPage endCursor }
    nodes { ...EhDisputeFields }
  }
}
""" + DISPUTE_FRAGMENT,
    connection=("disputes",),
    sample_variables={"first": 50, "query": "initiated_at:>='2026-01-01'"},
    scopes=("read_shopify_payments_disputes",),
    page_size=100,
))

DISPUTE = register(Document(
    "EhDispute",
    """
query EhDispute($id: ID!) {
  dispute(id: $id) { ...EhDisputeFields }
}
""" + DISPUTE_FRAGMENT,
    sample_variables={"id": "gid://shopify/ShopifyPaymentsDispute/1"},
    scopes=("read_shopify_payments_disputes",),
))


# Mapped metafields: only the mapped keys are read, variants in one request.
PRODUCT_METAFIELDS = register(Document(
    "EhProductMetafields",
    """
query EhProductMetafields($id: ID!, $keys: [String!]) {
  product(id: $id) {
    id
    metafields(first: 50, keys: $keys) { nodes { id namespace key type value compareDigest } }
  }
}
""",
    sample_variables={"id": "gid://shopify/Product/1", "keys": ["custom.care"]},
    scopes=("read_products",),
))

VARIANT_METAFIELDS = register(Document(
    "EhVariantMetafields",
    """
query EhVariantMetafields($ids: [ID!]!, $keys: [String!]) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      metafields(first: 50, keys: $keys) { nodes { id namespace key type value compareDigest } }
    }
  }
}
""",
    sample_variables={"ids": ["gid://shopify/ProductVariant/1"], "keys": ["custom.weight"]},
    scopes=("read_products",),
))

CUSTOMER_METAFIELDS = register(Document(
    "EhCustomerMetafields",
    """
query EhCustomerMetafields($id: ID!, $keys: [String!]) {
  customer(id: $id) {
    id
    metafields(first: 50, keys: $keys) { nodes { id namespace key type value } }
  }
}
""",
    sample_variables={"id": "gid://shopify/Customer/1", "keys": ["custom.job_title"]},
    scopes=("read_customers",),
))

ORDER_METAFIELDS = register(Document(
    "EhOrderMetafields",
    """
query EhOrderMetafields($id: ID!, $keys: [String!]) {
  order(id: $id) {
    id
    metafields(first: 50, keys: $keys) { nodes { id namespace key type value } }
  }
}
""",
    sample_variables={"id": "gid://shopify/Order/1", "keys": ["custom.gift_message"]},
    scopes=("read_orders",),
))

METAFIELDS_SET = register(Document(
    "EhMetafieldsSet",
    """
mutation EhMetafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id namespace key compareDigest }
    userErrors { field message code elementIndex }
  }
}
""",
    payload_key="metafieldsSet",
    sample_variables={"metafields": [{"ownerId": "gid://shopify/Product/1", "namespace": "custom", "key": "care",
                                      "type": "single_line_text_field", "value": "Hand wash", "compareDigest": None}]},
    scopes=("write_products",),
))


# Market and B2B price lists: Odoo pricelists are sent as fixed variant prices.
PRICE_LISTS = register(Document(
    "EhPriceLists",
    """
query EhPriceLists($first: Int!, $after: String) {
  priceLists(first: $first, after: $after, sortKey: NAME) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name currency fixedPricesCount
      catalog { id title status }
    }
  }
}
""",
    connection=("priceLists",),
    sample_variables={"first": 50},
    scopes=("read_products",),
    page_size=100,
))

PRICE_LIST_FIXED_PRICES = register(Document(
    "EhPriceListFixedPrices",
    """
query EhPriceListFixedPrices($id: ID!, $first: Int!, $after: String) {
  priceList(id: $id) {
    id
    prices(first: $first, after: $after, originType: FIXED) {
      pageInfo { hasNextPage endCursor }
      nodes {
        variant { id }
        price { amount currencyCode }
        compareAtPrice { amount currencyCode }
        quantityPriceBreaks(first: 10) {
          pageInfo { hasNextPage endCursor }
          nodes { id minimumQuantity price { amount currencyCode } }
        }
      }
    }
  }
}
""",
    connection=("priceList", "prices"),
    sample_variables={"id": "gid://shopify/PriceList/1", "first": 50},
    scopes=("read_products",),
    page_size=250,
))

PRICE_LIST_FIXED_PRICES_UPDATE = register(Document(
    "EhPriceListFixedPricesUpdate",
    """
mutation EhPriceListFixedPricesUpdate($priceListId: ID!, $pricesToAdd: [PriceListPriceInput!]!,
                                      $variantIdsToDelete: [ID!]!) {
  priceListFixedPricesUpdate(priceListId: $priceListId, pricesToAdd: $pricesToAdd,
                             variantIdsToDelete: $variantIdsToDelete) {
    pricesAdded { variant { id } }
    deletedFixedPriceVariantIds
    userErrors { field message code }
  }
}
""",
    payload_key="priceListFixedPricesUpdate",
    sample_variables={"priceListId": "gid://shopify/PriceList/1",
                      "pricesToAdd": [{"variantId": "gid://shopify/ProductVariant/1",
                                       "price": {"amount": "10.00", "currencyCode": "USD"}}],
                      "variantIdsToDelete": []},
    scopes=("write_products",),
))


QUANTITY_PRICING_UPDATE = register(Document(
    "EhQuantityPricingUpdate",
    """
mutation EhQuantityPricingUpdate($priceListId: ID!, $input: QuantityPricingByVariantUpdateInput!) {
  quantityPricingByVariantUpdate(priceListId: $priceListId, input: $input) {
    productVariants { id }
    userErrors { field message code }
  }
}
""",
    payload_key="quantityPricingByVariantUpdate",
    sample_variables={"priceListId": "gid://shopify/PriceList/1",
                      "input": {"quantityPriceBreaksToAdd": [
                          {"variantId": "gid://shopify/ProductVariant/1", "minimumQuantity": 10,
                           "price": {"amount": "8.00", "currencyCode": "USD"}}],
                          "quantityPriceBreaksToDeleteByVariantId": ["gid://shopify/ProductVariant/1"],
                          "quantityPriceBreaksToDelete": [], "quantityRulesToAdd": [],
                          "quantityRulesToDeleteByVariantId": [], "pricesToAdd": [],
                          "pricesToDeleteByVariantId": []}},
    scopes=("write_products",),
))


# Customer and company updates after the first order.
CUSTOMER = register(Document(
    "EhCustomer",
    """
query EhCustomer($id: ID!) {
  customer(id: $id) {
    id displayName firstName lastName locale taxExempt tags note state updatedAt
    defaultEmailAddress { emailAddress marketingState marketingUpdatedAt }
    defaultPhoneNumber { phoneNumber marketingState }
    defaultAddress { firstName lastName company address1 address2 city zip provinceCode countryCodeV2 phone }
  }
}
""",
    sample_variables={"id": "gid://shopify/Customer/1"},
    scopes=("read_customers",),
))

COMPANY = register(Document(
    "EhCompany",
    """
query EhCompany($id: ID!) {
  company(id: $id) { id name externalId note }
}
""",
    sample_variables={"id": "gid://shopify/Company/1"},
    scopes=("read_customers",),
))



# Gift card balances owed before Odoo followed the store.
GIFT_CARDS = register(Document(
    "EhGiftCards",
    """
query EhGiftCards($first: Int!, $after: String, $query: String) {
  giftCards(first: $first, after: $after, query: $query) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id lastCharacters enabled createdAt
      balance { amount currencyCode }
      order { id createdAt }
      transactions(first: 25) {
        pageInfo { hasNextPage endCursor }
        nodes { __typename processedAt amount { amount currencyCode } }
      }
    }
  }
}
""",
    connection=("giftCards",),
    sample_variables={"first": 25, "query": "status:enabled"},
    scopes=("read_gift_cards",),
    page_size=25,
))

GIFT_CARD_TRANSACTIONS = register(Document(
    "EhGiftCardTransactions",
    """
query EhGiftCardTransactions($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on GiftCard {
      transactions(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { __typename processedAt amount { amount currencyCode } }
      }
    }
  }
}
""",
    connection=("node", "transactions"),
    sample_variables={"id": "gid://shopify/GiftCard/1", "first": 100},
    scopes=("read_gift_cards",),
    page_size=100,
))


# Quantity changes made in Odoo sent to Shopify as an order edit.
ORDER_EDIT_BEGIN = register(Document(
    "EhOrderEditBegin",
    """
mutation EhOrderEditBegin($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder {
      id
      lineItems(first: 250) { nodes { id quantity editableQuantity variant { id } } }
    }
    userErrors { field message }
  }
}
""",
    payload_key="orderEditBegin",
    sample_variables={"id": "gid://shopify/Order/1"},
    scopes=("write_order_edits",),
))

ORDER_EDIT_SET_QUANTITY = register(Document(
    "EhOrderEditSetQuantity",
    """
mutation EhOrderEditSetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!) {
  orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity, restock: true) {
    calculatedLineItem { id quantity }
    userErrors { field message }
  }
}
""",
    payload_key="orderEditSetQuantity",
    sample_variables={"id": "gid://shopify/CalculatedOrder/1", "lineItemId": "gid://shopify/CalculatedLineItem/1",
                      "quantity": 2},
    scopes=("write_order_edits",),
))

ORDER_EDIT_ADD_VARIANT = register(Document(
    "EhOrderEditAddVariant",
    """
mutation EhOrderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
  orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity, allowDuplicates: false) {
    calculatedLineItem { id quantity }
    userErrors { field message }
  }
}
""",
    payload_key="orderEditAddVariant",
    sample_variables={"id": "gid://shopify/CalculatedOrder/1", "variantId": "gid://shopify/ProductVariant/1",
                      "quantity": 1},
    scopes=("write_order_edits",),
))

ORDER_EDIT_COMMIT = register(Document(
    "EhOrderEditCommit",
    """
mutation EhOrderEditCommit($id: ID!, $notifyCustomer: Boolean, $staffNote: String) {
  orderEditCommit(id: $id, notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
    order { id lineItems(first: 250) { nodes { id currentQuantity variant { id } } } }
    userErrors { field message }
  }
}
""",
    payload_key="orderEditCommit",
    sample_variables={"id": "gid://shopify/CalculatedOrder/1", "notifyCustomer": False, "staffNote": "From Odoo"},
    scopes=("write_order_edits",),
))
