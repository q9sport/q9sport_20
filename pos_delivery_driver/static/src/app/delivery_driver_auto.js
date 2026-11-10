/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";
import { DeliveryDriverPopup } from "@pos_delivery_driver/app/delivery_driver_popup";
import { _t } from "@web/core/l10n/translation";

/**
 * Delivery driver selection is triggered by adding the Delivery service product.
 * The product name is checked as a whole word, so ordinary products containing
 * similar text are not accidentally treated as delivery services.
 */
function isDeliveryProduct(product) {
    const name = String(product?.name || product?.display_name || "").trim();
    return /(^|[\s_-])delivery([\s_-]|$)/i.test(name);
}

patch(ProductScreen.prototype, {
    setup() {
        super.setup(...arguments);
        this.deliveryDriverOrm = useService("orm");
    },

    async addProductToOrder(product) {
        const line = await super.addProductToOrder(...arguments);
        if (line && isDeliveryProduct(product)) {
            await this._openDeliveryDriverSelection();
        }
        return line;
    },

    async _barcodeProductAction(code) {
        // Barcode additions use a separate Odoo 20 path from product-card clicks.
        const product = await this._getProductByBarcode(code);
        const result = await super._barcodeProductAction(...arguments);
        const template = product?.product_tmpl_id || product;
        if (product && isDeliveryProduct(template)) {
            await this._openDeliveryDriverSelection();
        }
        return result;
    },

    async _openDeliveryDriverSelection() {
        const order = this.currentOrder;
        if (!order) {
            return;
        }

        let employees;
        try {
            employees = await this.deliveryDriverOrm.call(
                "pos.order",
                "get_active_delivery_employees",
                []
            );
        } catch (error) {
            console.error("POS Delivery Driver: could not load drivers", error);
            this.notification.add(
                _t("Could not load delivery drivers. Please try again."),
                { type: "danger" }
            );
            return;
        }

        if (!employees || !employees.length) {
            this.notification.add(
                _t("No active delivery drivers found. Enable Delivery Driver on an employee first."),
                { type: "warning" }
            );
            return;
        }

        const payload = await makeAwaitable(this.dialog, DeliveryDriverPopup, {
            title: _t("Choose Delivery Driver"),
            employees,
            selectedDriverId: order.delivery_employee_id || false,
            isCashOnDelivery: Boolean(order.is_cash_on_delivery),
        });

        if (!payload) {
            return;
        }

        // These are custom POS order properties exported by delivery_driver_order.js.
        // Assign directly instead of calling update() with fields unknown to the
        // core POS model registry. The values are included in export_as_JSON().
        order.delivery_employee_id = payload.driverId || false;
        order.is_cash_on_delivery = Boolean(payload.isCashOnDelivery);
        order.delivery_status = payload.driverId ? "assigned" : "pending";

        if (typeof order.setDirty === "function") {
            order.setDirty();
        }
        if (typeof order.save_to_db === "function") {
            order.save_to_db();
        }

        this.notification.add(
            payload.driverName
                ? _t("Delivery driver assigned: ") + payload.driverName
                : _t("Delivery driver removed from this order."),
            { type: "success" }
        );
    },
});
