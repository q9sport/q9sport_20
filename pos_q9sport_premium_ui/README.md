# Q9SPORT Premium POS UI — Odoo 20

A standalone visual addon inspired by the supplied TIVA POS reference.

## Layout
- Product catalog and categories occupy the TOP area.
- Current order, order lines, totals, numpad and native payment action occupy the BOTTOM area.
- Dark navy surfaces, gold accents, product-card grid and green payment action.

## Technical approach
The addon targets the Odoo 20 `point_of_sale.ProductScreen` DOM:
- `.product-screen`
- `.leftpane` (native OrderSummary + pads)
- `.rightpane` (native CategorySelector + ProductCard grid)

CSS flex ordering places `.rightpane` first and `.leftpane` second. The native OWL components remain mounted and their original click handlers, order logic, payment validation, barcode handling, and delivery extensions are not replaced.

## Install / upgrade
1. Remove the earlier broken `pos_q9sport_premium_ui` version if it is installed.
2. Replace its module folder with this version.
3. Restart the Odoo.sh staging build.
4. Update Apps List, then Upgrade the module.
5. Open POS using `?debug=assets` once and hard refresh with Ctrl+Shift+R.

## Important
This is built against the publicly available Odoo 20 Community ProductScreen template structure. Your Enterprise database may include additional custom POS modules that alter the same screen. Test on `stage1` first. The addon is styling/layout only and has not been executed against your private Odoo.sh database.
