/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";

import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";

import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";

import { DeliveryDriverPopup } from "@pos_delivery_driver/app/delivery_driver_popup";

import { _t } from "@web/core/l10n/translation";


function isDeliveryProduct(product) {

    const name = String(
        product?.name ||
        product?.display_name ||
        ""
    ).trim();

    return /(^|[\s_-])delivery([\s_-]|$)/i.test(name);
}


patch(ProductScreen.prototype, {

    setup() {

        super.setup(...arguments);

        this.deliveryDriverOrm = useService("orm");

        this.deliveryDriverDialog = useService("dialog");

        this.deliveryDriverNotification =
            useService("notification");
    },


    async addProductToOrder(product, ...args) {

        // Normal products use standard POS behavior.
        if (!isDeliveryProduct(product)) {

            return await super.addProductToOrder(
                product,
                ...args
            );
        }

        const order = this.currentOrder;

        if (!order) {
            return;
        }

        // =====================================
        // LOAD DELIVERY EMPLOYEES
        // =====================================

        let employees = [];

        try {

            employees = await this.deliveryDriverOrm.call(
                "pos.order",
                "get_active_delivery_employees",
                []
            );

        } catch (error) {

            console.error(
                "POS Delivery Driver: employee loading error",
                error
            );

            this.deliveryDriverNotification.add(
                _t("Could not load delivery drivers. Please check server logs."),
                {
                    type: "danger",
                }
            );

            return;
        }

        // =====================================
        // CHECK EMPLOYEES
        // =====================================

        if (
            !Array.isArray(employees) ||
            employees.length === 0
        ) {

            this.deliveryDriverNotification.add(
                _t("No active delivery drivers found. Please activate Delivery Driver on an employee."),
                {
                    type: "warning",
                }
            );

            return;
        }

        // =====================================
        // OPEN POPUP
        // =====================================

        let payload;

        try {

            payload = await makeAwaitable(
                this.deliveryDriverDialog,
                DeliveryDriverPopup,
                {
                    title: _t("Choose Delivery Driver"),

                    employees: employees,

                    selectedDriverId:
                        order.delivery_employee_id || false,

                    isCashOnDelivery:
                        Boolean(order.is_cash_on_delivery),
                }
            );

        } catch (error) {

            console.error(
                "POS Delivery Driver: popup error",
                error
            );

            return;
        }

        // =====================================
        // CANCEL
        // =====================================

        if (!payload || !payload.driverId) {

            // Do not add Delivery product on cancellation.
            return;
        }

        // =====================================
        // ADD DELIVERY PRODUCT
        // =====================================

        const result = await super.addProductToOrder(
            product,
            ...args
        );

        // =====================================
        // SAVE DRIVER ON CURRENT ORDER
        // =====================================

        order.delivery_employee_id =
            Number(payload.driverId);

        order.is_cash_on_delivery =
            Boolean(payload.isCashOnDelivery);

        order.delivery_status = "assigned";

        if (typeof order.setDirty === "function") {
            order.setDirty();
        }

        if (typeof order.save_to_db === "function") {
            await order.save_to_db();
        }

        // =====================================
        // NOTIFICATION
        // =====================================

        this.deliveryDriverNotification.add(
            _t("Delivery driver assigned: ") +
            (payload.driverName || ""),
            {
                type: "success",
            }
        );

        return result;
    },

});
