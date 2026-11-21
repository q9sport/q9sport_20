# Q9 POS Sales Details Extended (Odoo 20)

This addon extends the native `point_of_sale.report_saledetails` QWeb report. It does not create a second cashier wizard, report action, menu, or security/access CSV.

## Install / Upgrade
1. Copy `q9_daily_cashier_report` into the custom addons directory (replace the old addon folder).
2. Restart/update Apps List and upgrade `Q9 POS Sales Details Extended` / technical module `q9_daily_cashier_report`.
3. Open Point of Sale → Reporting → Sales Details and print the standard report.

The extra sections use the native `pos.details.wizard` record and search POS orders for its date/config/session filters. The addon intentionally contains no `security` directory and no custom ACLs.

## Notes
The module was statically checked only; it has not been installed or executed against the target Q9SPORT Odoo.sh database. The Odoo 20 native QWeb template external ID is expected to be `point_of_sale.report_saledetails`; confirm against the exact deployed build if Odoo reports an XML ID inheritance error.
