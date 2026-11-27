# POS Fast Retail 20 - TIVA-inspired compact UI

This release targets the Odoo 20 native ProductScreen layout:
- Cart/order pane stays on the left.
- Product cards occupy the center.
- Category selector is styled as a compact right rail.
- Dark navy surfaces, restrained gold accents and green payment emphasis.
- Small typography, compact controls, no heavy shadows.

## Installation
1. Remove the previous `pos_fast_retail_20` folder from the custom addons path (or replace it with this folder).
2. Copy this module folder into the custom addons path.
3. Restart Odoo.
4. Update Apps List and upgrade/install **POS Fast Retail 20 - TIVA Style**.
5. Open POS with `?debug=assets` once, then hard-refresh (Ctrl+Shift+R).

## Important compatibility note
The module styles the native Odoo 20 ProductScreen and does not replace POS models or payment logic. Odoo builds and other POS customizations can change markup/classes. The supplied screenshot shows additional POS extensions (for example, extra header/order controls and payment methods), so those controls are kept native and are not recreated by this theme. Validate on a staging database first.
