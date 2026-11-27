# POS Fast Retail - Minimal Dark UI (Odoo 20)

A presentation-only theme for Odoo 20 Point of Sale inspired by the supplied TIVA POS reference:
- Deep navy surfaces, restrained gold accents and green payment emphasis.
- Compact typography, restrained borders and shadows.
- Keeps the native Odoo POS screens, order flow, payment logic and backend models.

## Install
1. Copy `pos_fast_retail_20` into your Odoo 20 custom addons path.
2. Restart Odoo and update the Apps list.
3. Install **POS Fast Retail - Minimal Dark UI**.
4. Hard-refresh the POS browser tab (Ctrl+Shift+R). If assets are cached, restart with asset regeneration/update the module.

## Important
This first release is a CSS theme layer, not a replacement POS application. Odoo 20 deployments can differ in frontend markup between builds and installed POS extensions. Validate on a staging database first. If a specific screen element does not pick up the styling, provide a screenshot and the exact Odoo build so its selector can be tuned. No Python business logic is overridden.
