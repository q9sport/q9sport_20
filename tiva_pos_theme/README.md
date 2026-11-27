# TIVA POS - Dark Navy & Gold (Odoo 20)

This addon applies the TIVA dark navy visual treatment to the native Odoo 20 POS. Product tiles/cards are explicitly forced to dark navy gradients (including common nested white/card-body surfaces), with subtle gold hover/selection accents. Cart panels, category controls, buttons, dialogs, search fields and payment controls are also styled.

## Install
1. Copy `tiva_pos_theme` into the custom addons path used by the Odoo 20 database.
2. Restart the Odoo service/build.
3. Update the Apps list and install **TIVA POS - Elegant Dark UI**.
4. Hard refresh the POS browser tab (`Ctrl+Shift+R`). If assets are cached, upgrade the module and clear browser/site assets cache.

## Important scope
This package is a visual theme for the native Odoo 20 POS. It does not yet replace the ProductScreen OWL template to force the reference's exact three-column geometry (cart left / products center / categories right) or create a new fixed bottom action bar. Those structural changes require validation against the exact Odoo 20 source/build deployed in the target database. Native POS workflows are intentionally not replaced by this CSS-only addon.
