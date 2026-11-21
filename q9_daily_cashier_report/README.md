# Q9 Daily Cashier Report — Odoo 20

Adds a Daily Cashier report wizard with PDF and XLSX exports, date/POS/cashier filters, and sections modeled after the uploaded WEBZ report.

Sections: payment-method summary, collections, credit accounts (credit-payment customers), items, returns, cashier performance, delivery performance (only if a recognized delivery-driver field exists), hourly totals, POS-by-hour, top-selling items.

## Install
Copy this addon into the custom addons directory, push to Odoo.sh, update Apps list, install `Q9 Daily Cashier Report`.

## Important validation notes
This is a first implementation based on standard Odoo POS models and common custom delivery-field names. It has not been run against the Q9SPORT database. Credit-account rows are inferred from credit-like payment method names and the order partner. Driver performance requires a delivery-driver field on `pos.order` with one of the recognized names; if Q9SPORT uses a different custom field, update the candidate list. Hourly channel groups are POS configurations, not external marketplace channels unless those are configured as separate POSs.
