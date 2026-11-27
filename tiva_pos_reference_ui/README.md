# TIVA POS Reference UI — Odoo 20

## Install
1. Copy `tiva_pos_reference_ui` into the custom addons directory.
2. Restart/update the Odoo.sh branch.
3. Update Apps List and install **TIVA POS Reference UI**.
4. Upgrade the module after changes and hard-refresh the POS (`Ctrl+Shift+R`).

## Design
- Dark navy/near-black surfaces only; no white cards or white panel backgrounds.
- Restrained gold borders/active states.
- Dark product tiles, dark category controls, dark cart and dialogs.
- Green primary payment action.
- Responsive styling for POS screens.

## Scope / compatibility note
This addon is a standalone visual layer and deliberately preserves Odoo's native POS components and workflows. Odoo 20's exact DOM/template structure can vary with edition/build and installed POS addons; the visual selectors are defensive. It has not been installed against the user's live database, so validate on a staging branch before production. This version does not replace native ProductScreen OWL markup to force a pixel-identical three-column geometry or introduce new bottom-bar actions; those require checking the exact deployed Odoo 20 ProductScreen template and binding each action to its native handler.
