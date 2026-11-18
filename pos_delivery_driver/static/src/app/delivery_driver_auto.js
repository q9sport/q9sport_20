/** @odoo-module **/
import { patch } from "@web/core/utils/patch";

import { PosStore } from "@point_of_sale/app/services/pos_store";

import { makeAwaitable } from
    "@point_of_sale/app/utils/make_awaitable_dialog";

import { DeliveryDriverPopup } from
    "@pos_delivery_driver/app/delivery_driver_popup";


const DELIVERY_PRODUCT_ID = 38;
const DELIVERY_PRODUCT_BARCODE = "10002";

const processingOrders = new WeakSet();


function getProduct(vals) {
    const product = vals?.product_id;

    if (product && typeof product === "object") {
        return product;
    }

    return false;
}


function isDeliveryProduct(vals) {
    const product = getProduct(vals);

    if (!product) {
        return false;
    }

    const productId = Number(product.id || 0);

    const barcode = String(
        product.barcode || ""
    ).trim();

    return (
        productId === DELIVERY_PRODUCT_ID ||
        barcode === DELIVERY_PRODUCT_BARCODE
    );
}


function isDeliveryAlreadyInOrder(order) {
    const lines = order?.lines || [];

    return lines.some((line) => {
        const product = line.product_id;

        if (!product) {
            return false;
        }

        return (
            Number(product.id || 0) === DELIVERY_PRODUCT_ID ||
            String(product.barcode || "").trim() === DELIVERY_PRODUCT_BARCODE
        );
    });
}


function getOrderLabel(order) {
    if (!order) {
        return "New Order";
    }

    if (order.tracking_number) {
        return String(order.tracking_number);
    }

    if (order.name && order.name !== "/") {
        return String(order.name);
    }

    if (order.pos_reference) {
        return String(order.pos_reference);
    }

    if (order.uuid) {
        return String(order.uuid).substring(0, 8);
    }

    return "New Order";
}


patch(PosStore.prototype, {

    async addLineToCurrentOrder(vals, opts = {}, configure = true) {

        // Normal products are not affected.
        if (!isDeliveryProduct(vals)) {
            return await super.addLineToCurrentOrder(...arguments);
        }

        const order = this.getOrder();

        if (!order) {
            return await super.addLineToCurrentOrder(...arguments);
        }

        // Prevent duplicate popup for the same order.
        if (processingOrders.has(order)) {
            return false;
        }

        processingOrders.add(order);

        try {

            // Prevent adding delivery product more than once.
            if (isDeliveryAlreadyInOrder(order)) {

                await makeAwaitable(
                    this.dialog,
                    DeliveryDriverPopup,
                    {
                        orderName: getOrderLabel(order),
                        alreadyAdded: true,
                        isCashOnDelivery:
                            order.is_cash_on_delivery !== false,
                        employees: [],
                        selectedDriverId:
                            order.delivery_employee_id || false,
                    }
                );

                return false;
            }

            // Load active drivers.
            let employees = [];

            try {
                employees = await this.orm.call(
                    "pos.order",
                    "get_active_delivery_employees",
                    [],
                    {}
                );
            } catch (error) {
                console.warn(
                    "Could not load delivery drivers:",
                    error
                );
            }

            // Open delivery confirmation popup.
            const payload = await makeAwaitable(
                this.dialog,
                DeliveryDriverPopup,
                {
                    orderName: getOrderLabel(order),
                    alreadyAdded: false,
                    isCashOnDelivery:
                        order.is_cash_on_delivery !== false,
                    employees: employees,
                    selectedDriverId:
                        order.delivery_employee_id || false,
                }
            );

            if (!payload || !payload.confirmed) {
                return false;
            }

            // Check again before adding.
            if (isDeliveryAlreadyInOrder(order)) {
                return false;
            }

            // Add product using Odoo native behavior.
            // No price or quantity modifications.
            const result = await super.addLineToCurrentOrder(
                ...arguments
            );

            if (result) {

                order.is_cash_on_delivery =
                    Boolean(payload.isCashOnDelivery);

                order.delivery_employee_id =
                    payload.driverId || false;

                order.delivery_status =
                    payload.driverId ? "assigned" : "pending";

                if (typeof order._markDirty === "function") {
                    order._markDirty();
                } else if (typeof order.setDirty === "function") {
                    order.setDirty();
                }

                if (typeof this.save_to_db === "function") {
                    await this.save_to_db();
                }
            }

            return result;

        } catch (error) {

            console.error(
                "POS Delivery Service Error:",
                error
            );

            throw error;

        } finally {

            processingOrders.delete(order);

        }
    },

});
