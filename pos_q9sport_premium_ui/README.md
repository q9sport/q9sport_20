# Q9SPORT Premium POS UI — Odoo 20

## Layout target
This version targets the Odoo ProductScreen structure where `.screen-full-width`
contains `.leftpane` (order/checkout) and `.rightpane` (products/categories).

- Products and categories: TOP
- Order lines, totals and native checkout controls: BOTTOM
- Dark navy, gold accents, green native payment action styling
- Responsive product grid

## Safety scope
This addon contains only one SCSS asset. It does not define Python models,
XML template overrides, JavaScript patches, RPC calls, SQL constraints, or
payment/order logic. It therefore does not modify payment calculations,
taxes, discounts, stock, barcode handling, receipts, synchronization, or
delivery methods.

## Install safely
1. Take a staging backup.
2. If an older version of this addon is installed, upgrade/replace it with this
   folder; do not keep duplicate copies in addons_path.
3. Restart the Odoo.sh staging build.
4. Update Apps List and Upgrade this module.
5. Test the POS on stage1 with `?debug=assets`, then hard-refresh.

## Compatibility note
The CSS selectors target the Odoo ProductScreen wrapper `.screen-full-width`
and its `.leftpane` / `.rightpane` children. Enterprise/custom addons can
alter the DOM or sizing rules. This package has been syntax-checked and the
ZIP integrity checked, but it has NOT been runtime-tested on the private
Q9SPORT Odoo.sh database. No one can honestly guarantee zero issues without
testing against that exact build and its installed POS extensions.
