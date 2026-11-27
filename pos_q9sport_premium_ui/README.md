# Q9SPORT Premium POS UI — Odoo 20

## Contents
- `__manifest__.py`
- `__init__.py`
- `static/src/scss/premium_pos.scss`

## What it changes
This addon adds a visual theme and attempts a vertical split layout:
- Product/catalog region on top.
- Current order region below.
- Navy, soft gray and gold styling.
- Product cards, category buttons and order lines receive cleaner styling.

## What it deliberately does NOT change
- Product selection logic
- Order-line quantity logic
- Taxes, discounts or pricing
- Payment validation or payment methods
- Delivery module logic
- Receipt printing or synchronization

## Installation
1. Copy `pos_q9sport_premium_ui` into a custom addons directory.
2. Restart/update the Apps list.
3. Install **Q9SPORT Premium POS UI**.
4. Open POS with `?debug=assets` for the first test, then hard-refresh the browser.

## Important Odoo 20 note
Odoo 20 Enterprise builds may have customized POS DOM structure. This package uses standard POS container selectors and is a styling-only first pass. Test it on the staging database before production. If the split does not apply exactly, send the POS screen's DOM/classes or a screenshot with browser inspector and the selectors can be aligned to the exact Enterprise build.

The addon does not claim to be runtime-tested against your private Odoo.sh build.
