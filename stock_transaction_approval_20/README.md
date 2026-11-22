# Stock Transaction Approval 20

Approval workflow for **Receipts and Internal Transfers only** in Odoo 20.

## Approval group
Uses the existing Odoo Inventory Administrator group:
- XML ID: `stock.group_stock_manager`
- Database ID in the supplied database: `46` (numeric IDs may differ between databases).

Only members of this group see **Approve** and **Reject** and can execute those actions.
No new security group is created.

## Workflow
1. An inventory user creates a Receipt or Internal Transfer.
2. The user clicks **Submit for Approval**.
3. Members of `stock.group_stock_manager` see **Approve** and **Reject**.
4. Validate is allowed only after approval.

Delivery Orders and Inventory Adjustments are excluded and continue to work normally.

## Security
- Approval status and audit fields are protected against ordinary direct ORM/RPC writes.
- Approval actions enforce membership in `stock.group_stock_manager`.
- Both `button_validate()` and `_action_done()` enforce approval before completion.

## Install
Copy this folder into an addons path, restart Odoo, update the Apps List, and upgrade/install
**Stock Internal Transfer Approval**.

Test on a staging database before production. This package has not been tested against a
live Odoo 20 server in this environment.
