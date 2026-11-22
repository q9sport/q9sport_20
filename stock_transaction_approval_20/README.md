# Stock Internal Transfer Approval (Odoo 20)

A small approval workflow for **internal transfers only**.

## Workflow
1. Inventory users create an internal transfer.
2. They click **Submit for Approval**.
3. A member of the standard **Inventory / Administrator** (technical group:
   `stock.group_stock_manager`) clicks **Approve** or **Reject**.
4. The transfer can be validated only after approval.

Receipts, delivery orders, and inventory adjustments are not part of this workflow.

## Installation
1. Copy `stock_transaction_approval_20` into a directory in the Odoo addons path.
2. Restart the Odoo service.
3. Update the Apps List.
4. Install **Stock Internal Transfer Approval**.

## Important
- The module uses the standard Odoo Inventory Manager group.
- Approval is checked in both `button_validate()` and `_action_done()` to prevent
  direct server-side validation calls from bypassing the approval step.
- A backorder created from an approved transfer is expected to start a fresh
  approval cycle because the approval state is not copied.

Test this module on a staging database before installing it in production.
