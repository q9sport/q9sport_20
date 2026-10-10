# Shopify Connector changelog

One module serves Odoo 16, 17, 18, 19 and 20. Each series carries the same features and the same test suite; the version notes below list what differs underneath so a merchant or partner upgrading Odoo knows what the connector handles for them.

## 1.0.0 for Odoo 20, 2026-09-19 (20.0.1.0.0)

The Odoo 20 release of 1.0.0, with the same features and the same test suite, plus checks that open every screen of the app in the browser, drive the Connect a Store dialog from start to finish, and finish an install link through the web server. What differs underneath is in the version notes.

## 1.0.0, 2026-09-16 (16.0.1.0.0, 17.0.1.0.0, 18.0.1.0.0, 19.0.1.0.0)

First release, on Shopify Admin GraphQL API 2026-01 to 2026-07, validated on the 2026-10 release candidate.

### Connection and engine

- Three ways to connect: a Dev Dashboard app with client credentials, an install link, or an existing admin token. Credentials are stored encrypted.
- Signed webhooks with exact shop domain match, delivery deduplication and an hourly self heal; scheduled polling covers anything a webhook missed.
- Durable job queue with retries, backoff, Shopify cost limit handling, jobs of one resource run in order, and one exceptions inbox.
- Every GraphQL document is checked against each supported API version by the contract check before release.
- Shopify privacy requests are handled: a request for a copy of customer data becomes a to-do, a customer erasure
  clears what the connector stored about them, and a shop erasure removes the stored deliveries and the credentials.
- Deliveries and job payloads that stay for review lose their personal data after the retention period, keeping the
  reason they failed.
- An offline throughput benchmark ships with the tests and runs on request through the test runner.

### Orders and customers

- Orders with taxes, discounts, shipping, every payment and amount parity against Shopify totals.
- Customers matched by link, exact email or exact phone; B2B companies, company locations and payment terms.
- Customer and company webhooks: later changes follow in Odoo, Shopify tags become contact tags, deletions keep the contact, merges move the link.
- Optional email consent mirroring to the Odoo blacklist, lifting only entries the connector added.
- Customer and order metafields imported into the contact and the sales order.
- Order edits from Shopify until the order ships or is invoiced, and quantity changes made in Odoo saved in Shopify as an order edit with a preview; point of sale orders with a walk-in contact and cash net of change.
- Customer and order history import through bulk exports.

### Fulfilment, stock and catalogue

- Fulfilment in both directions with tracking, local pickup, holds, fulfillment order moves and cancellations.
- Stock push of what Odoo can sell minus units Shopify has committed, with per location rules and a stock comparison.
- Two way catalogue: linking by SKU or barcode, product creation, publishing, master policy per group, images, product and variant metafields, bulk import with resumable downloads and a nightly deletion sweep.
- Odoo pricelists sent to Shopify market and B2B price lists, with compare at prices and quantity price breaks.

### Money after the sale

- Refunds as credit notes per invoice with refund payments; Odoo credit notes refunded in Shopify after a preview.
- Returns and exchanges; restocked units received at the warehouse of the Shopify location, and units not restocked received into an optional location.
- Shopify Payments payouts as one journal entry each through a transit account, in the company currency or another, matched to the bank deposit in the payout currency; disputes as to-dos before the evidence deadline; gift cards and store credit as liabilities, with opening balances for gift cards sold before installation.

### Version notes

**Odoo 20.** Model access and record rules live in one `ir.access` table: the group permissions and the company and owner restrictions of the connector are carried over row for row. Configuration parameters are read with the typed `get_str` and `get_int`. Binary fields hold binary values, so product images are compared and written as bytes instead of base64. Returns come from the delivery through `_create_return`, which replaced the return wizard; lines that return nothing are dropped and the transfer is confirmed without the zero demand warning. Stock moves carry their unit in `uom_id`, and a product tracked by quantity only has no `tracking` value. A posted payment is `paid` until its bank line is matched, then `reconciled`. The tax id format check of a contact moved into the base module, so tax ids and addresses from a Shopify checkout are stored as Shopify sends them instead of stopping the order. The raw Shopify codes on orders and payouts are labelled apart from their readable versions. The web client no longer ships Font Awesome, so the buttons of the store, order, job, export and catalogue check screens use the icon names of its own icon font.

**Odoo 19.** Sale order lines use `product_uom_id`; groups and users use `user_ids` and `group_ids`. Odoo archives an activity when it is marked done, so the connector clears its own link to a finished dispute activity. Accounts belong to several companies through `company_ids`. A changed ordered quantity keeps its price through `technical_price_unit`.

**Odoo 18.** Access rights and record rules are checked together through `check_access`. Accounts belong to several companies through `company_ids`. A changed ordered quantity keeps its price through `technical_price_unit`. Views and crons are backported to the 18 syntax by the build tool.

**Odoo 17.** Stock moves record `quantity` and `picked`. Payment terms are created as a 100 percent line due a number of days after the invoice. Chatter, kanban cards and crons use the 17 syntax. Odoo reprices a sale line when its quantity changes, so the connector writes price and discount together with the quantity.

**Odoo 16.** Stock moves record `quantity_done`. Payment terms are created as a balance line due a number of days after the invoice. The shipping module is `delivery` instead of `stock_delivery`. Chatter, kanban cards and crons use the 16 syntax. Odoo reprices a sale line when its quantity changes, so the connector writes price and discount together with the quantity.

### Known limits

- SMS marketing consent is not mirrored.
