# POS Split Payment & Qatar Customer Details (Odoo 20)

## Included
- Native POS payment lines with one manual amount input per eligible payment method.
- Prevents the total allocated across methods from exceeding the order total.
- Shows Sale, Paid and Remaining totals with compact visual indicators.
- Payment methods stay on the LEFT; order summary and customer form stay on the RIGHT on desktop.
- Only the Qatar delivery number fields are added: Zone No, Street No and Building No.
- Customer phone lookup runs automatically and loads the existing customer data when a matching phone is found.
- Saves or updates the actual `res.partner` record and links it to the current POS order.
- Includes customer name, optional date of birth, GPS coordinates and an Arabic geographic-place description when reverse geocoding is available.
- The geographic place name is resolved from GPS through OpenStreetMap Nominatim. If the external geocoder is unavailable, GPS coordinates are still saved and shown.
- Uses `res.partner.phone` only. It deliberately does not search the `mobile` field because it is not present in the target database.
- Keeps the native POS refund/payment actions; adds a visible +/- mark and refund arrow.

- Uses Owl `proxy` from `@odoo/owl` for reactive customer inputs; this avoids the `useState is not a function` runtime crash on the target saas-19.5 POS bundle.
- Phone lookup searches `res.partner.phone` only; there is no reference to the absent `res.partner.mobile` field.
- Does not extend `point_of_sale.OrderReceipt`, avoiding the missing-parent receipt template warning.

## Installation / update
1. Remove the old `pos_split_textbox_20` addon directory from the custom-addons branch, then copy this complete folder in its place. Do not merge old files into it.
2. Restart the Odoo.sh staging build.
3. Upgrade `pos_split_textbox_20` from Apps.
4. Confirm the deployed `__manifest__.py` version is `20.0.13.0.0`, then open POS with `?debug=assets` and hard-refresh / clear the POS cache.
5. Test on a staging POS session before production.

## Important
The Arabic place-name lookup needs outbound HTTPS access from the Odoo server to `nominatim.openstreetmap.org`. If that service is unreachable or has no Arabic place label for the GPS point, the module falls back to displaying the saved GPS coordinates. No separate city/district/street-name input fields are created.

The module still expects account `100201` to be configured as a QAR receivable account for the active company, matching the existing POS customer-creation requirement.
