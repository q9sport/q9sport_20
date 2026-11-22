# Stock Transaction Approval 20

Approval workflow for **internal transfers only** in Odoo 20.

## Existing approval group
Uses the existing Odoo Inventory Administrator group:
- XML ID: `stock.group_stock_manager`
- Database ID shown in the target database: `46` (numeric IDs can differ between databases)

Only members of this group see **Approve** and **Reject**, and the server checks the same
group before allowing either action. No new group is created.

## Workflow
1. An inventory user creates an internal transfer.
2. The user clicks **Submit for Approval**.
3. Members of `stock.group_stock_manager` see **Approve** and **Reject**.
4. Validate is allowed only after approval.

Receipts, delivery orders, and inventory adjustments are excluded.

## Security
- Workflow status and audit fields are protected against ordinary direct ORM/RPC writes.
- Approval actions check membership in `stock.group_stock_manager`.
- Both `button_validate()` and `_action_done()` enforce approval before completion.

## Install
Copy this folder into an addons path, restart Odoo, update the Apps List, and install
**Stock Internal Transfer Approval**.

Test on a staging database before production. It has not been tested against a live
Odoo 20 server in this environment.
