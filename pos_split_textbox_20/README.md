# POS Split Payment & Qatar Customer Details — Odoo 20

## What's included
- Native Odoo POS payment lines with manual split amount inputs.
- Brand-style payment artwork for Cash, Visa, Mastercard, mada, Apple Pay, Google Pay, bank transfer, wallet and generic card/online methods.
- Modern emerald, navy and champagne styling for payment, summary, customer and cashier surfaces.
- Qatar customer details saved on the real `res.partner` record and linked to the current POS order.
- Existing customer lookup by phone; exact last-eight-digit matching avoids the previous last-four-digit false matches.
- Qatar address fields are stored independently: Zone No, Street No and Building No.
- Customer building coordinates are resolved from QNAS using Zone / Street / Building. The cashier device GPS is not used.
- No hard-coded receivable account code. Odoo uses the company's normal receivable-account configuration.

## Important address behavior
Qatar Zone, Street and Building identifiers are resolved through QNAS. The module displays a map pin only when QNAS returns valid coordinates for that exact building.

## Installation
1. Back up the staging database and POS custom-addons branch.
2. Replace the old `pos_split_textbox_20` folder completely with this folder.
3. Push the change to the Odoo.sh staging branch and wait for a successful build.
4. Upgrade `pos_split_textbox_20` from Apps.
5. Open POS with `?debug=assets`, hard-refresh, and start a new POS session.
6. Test customer creation, existing-customer lookup, address update, GPS capture, and each payment method on staging before production.

## Configuration / limitations
- The cashier must have the Point of Sale user group.
- Exact customer building coordinates require an active QNAS API token and the registered domain. The module cannot generate these credentials.

## Version 20.0.20 – customer lookup and save confirmation fix

- Phone matching now checks `phone`, `mobile`, and `phone_sanitized` where available, including archived customer records and company-assigned contacts.
- Qatar numbers are normalized to their final eight digits; Arabic-Indic and Persian numerals are converted to Western digits before matching.
- The POS customer form reads a customer's `mobile` value when `phone` is empty.
- New/updated customer phone values are written to both `phone` and `mobile` (when `mobile` exists), so later lookups can find the customer regardless of which standard Odoo field was used.
- Saving an existing customer passes the selected partner ID even if the current POS order partner was cleared while the phone was edited.
- The confirmation dialog awaits the save callback and displays server errors rather than silently swallowing them.
- Replaced the SVG wildcard in the asset manifest with explicit SVG paths to address the `*.svg did not resolve to anything` warning from the supplied Odoo.sh log.

### Deployment note
Replace the entire previous addon folder with this version, upgrade the module, rebuild POS assets, and start a fresh POS session. Test with an existing customer whose number is stored only in `mobile`, then edit the name/address and choose **Save changes** in the confirmation dialog. The source log supplied for the previous build contains HTTP 200 status lines only; it does not include RPC response bodies, so it cannot prove whether those earlier calls returned a customer or saved changed values.


## V20.0.21 — POS metadata field compatibility
- Fixed POS startup crash when `res.partner.mobile` is not present in the database.
- `_load_pos_data_fields` now adds optional partner fields only when they exist in `res.partner._fields`.
- `phone_sanitized` is handled with the same existence guard.


## Version 20.0.23 — QNAS address lookup and phone/customer stability

- Replaced the old QNA-Locator endpoint with the documented QNAS endpoint `GET https://qnas.qa/get_location/{zone}/{street}/{building}`.
- QNAS requests use the required `X-Token` and `X-Domain` headers. Official API documentation: https://qnas.qa/apidoc.
- Fixed configuration reading for Odoo builds where `ir.config_parameter.get_param` is not exposed: the code falls back to reading the parameter record directly.
- Required System Parameters (you must obtain the token from QNAS; the module cannot generate or guess credentials):
  - `pos_split_textbox_20.qnas_token` = your QNAS API token
  - `pos_split_textbox_20.qnas_domain` = the registered domain authorized by QNAS
- The cashier screen no longer uses device GPS. The customer's building pin comes only from QNAS using the customer's Zone / Street / Building numbers. No zone centroid, text-search pin, or cashier-device coordinates are substituted.
- If any of the three address numbers is zero (including `0`, `00`, etc.), the location is saved/displayed as `غير معرف`, GPS coordinates are cleared, and no QNAS request is made.
- Phone lookup continues to normalize Qatar numbers, including Arabic/Persian digits and +974, and match the final eight digits in `phone` and optional `mobile`.
- This package was syntax/XML/asset checked, but still requires an Odoo.sh staging test with a valid QNAS token and registered domain.


## Version 20.0.24.0.0
- Fixed the POS reactive-proxy error `SET on proxy ... property id`: server customer IDs are now used only for record lookup/creation and are never reassigned to an existing POS record.
- Both customer lookup and customer save refresh the POS record through one safe helper that updates writable customer fields only.
- Phone normalization treats `+974 30710074`, `97430710074`, and `30710074` as the same Qatar number by matching the final eight digits.
- Server lookup searches available `phone` and optional `mobile` fields, then verifies the complete normalized eight-digit number before returning a customer.


## Version 20.0.25.0.0 — load customer delivery fields in cashier
- Added the standard customer `street` address to the inline POS cashier customer panel.
- Address is loaded from an existing customer during phone lookup and refreshed after save.
- Address is saved to `res.partner.street`; if the cashier leaves it blank, the module generates a readable address from Zone / Street / Building numbers.
- Explicitly includes `street` in POS partner data fields when available.
- The cashier panel includes Phone, Zone No, Street No, Building No, Address, and Date of Birth.


## V20.0.26 — Cashier customer fields and printed receipt

- Keeps the cashier customer editor fields: Phone, Date of Birth, Address, Zone No, Street No and Building No.
- A changed existing customer, or a newly entered customer, triggers a Save / Don't save confirmation before saving from the cashier.
- Choosing Don't save continues payment without writing customer changes.
- Ordinary sales are not blocked when no customer details were entered; if customer details are entered and the cashier chooses Save, required customer fields are validated.
- Extends the POS printed receipt with customer name, phone and a composed address (Address + Zone/Street/Building numbers). Date of birth is intentionally not printed for privacy.
- Receipt fields are added through `export_for_printing`; no `id` property is assigned to reactive POS partner records.

After upgrading, close the POS tab and reopen it with a hard refresh so the updated POS assets are loaded. Test on staging with a newly created customer and an existing customer before production.


## V20.0.27 — Cash tender and Odoo 20 receipt renderer

- Keeps the native POS payment line amount as the actual payment amount.
- Adds a separate informational `cash_received` value on `pos.order`; it is not a `pos.payment` and is not used to calculate accounting or invoice amounts.
- Shows Cash Received and the calculated Change / Remaining only for cash payment methods.
- Adds the cash tender details to the Odoo 20 `point_of_sale.pos_order_receipt` renderer.
- Moves the Qatar receipt extension to the backend QWeb receipt template and supplies the same Qatar/customer and cash fields to the Python and JavaScript receipt data builders.
- The original Qatar customer editor, QNAS integration, payment artwork, split-payment behavior and SCSS design are retained.
