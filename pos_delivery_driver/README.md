POS Delivery Driver Management - Odoo 20

Fix in this build:
- Replaced the removed Owl useState hook with Owl 3 proxy reactivity in DeliveryDriverPopup.
- The popup is triggered automatically when a product whose name contains the standalone word "Delivery" is added.
- No separate Delivery control button is included in the POS assets.

Install by replacing the module folder, committing/pushing, upgrading the module, then opening POS with a fresh asset reload.
