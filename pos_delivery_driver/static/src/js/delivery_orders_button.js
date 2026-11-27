/** @odoo-module **/
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";
import { patch } from "@web/core/utils/patch";

const DELIVERY_PRODUCT_ID = 38;
const DELIVERY_PRODUCT_BARCODE = "10002";

// =====================================================
// DELIVERY HELPERS
// =====================================================

function hasDeliveryProduct(order) {
    const lines = typeof order.getOrderlines === "function"
        ? order.getOrderlines()
        : order.lines || [];

    return lines.some((line) => {
        const product = line.product_id;

        return (
            product?.id === DELIVERY_PRODUCT_ID ||
            product?.barcode === DELIVERY_PRODUCT_BARCODE
        );
    });
}

// =====================================================
// NAVBAR
// =====================================================

patch(Navbar.prototype, {

    onDeliveryOrdersClick() {
        this.pos.deliveryOrdersMode = true;

        // ALL is the default delivery filter.
        this.pos.deliveryOrdersFilter = "ALL";

        this.pos.navigate("TicketScreen");
    },

    onTicketButtonClick() {
        this.pos.deliveryOrdersMode = false;
        this.pos.deliveryOrdersFilter = null;

        return super.onTicketButtonClick(...arguments);
    },

    onClickRegister() {
        this.pos.deliveryOrdersMode = false;
        this.pos.deliveryOrdersFilter = null;

        return super.onClickRegister(...arguments);
    },

});

// =====================================================
// TICKET SCREEN
// =====================================================

patch(TicketScreen.prototype, {

    // -------------------------------------------------
    // FILTER DELIVERY ORDERS
    // -------------------------------------------------

    getFilteredOrderList() {

        const orders = super.getFilteredOrderList(...arguments);

        // Normal Odoo Orders: no modifications.
        if (!this.pos.deliveryOrdersMode) {
            return orders;
        }

        // Delivery mode: only delivery products.
        // Do not restrict by order state.
        const filter = this.pos.deliveryOrdersFilter || "ALL";

        let result = orders.filter(hasDeliveryProduct);

        if (filter === "UNPAID") {
            result = result.filter((order) =>
                order.state === "draft"
            );
        }

        if (filter === "INVOICE_UNPAID") {
            result = result.filter((order) => {
                const invoiceState =
                    order.account_move_payment_state ||
                    order.account_move?.payment_state;

                return (
                    ["paid", "done"].includes(order.state) &&
                    ["not_paid", "partial"].includes(invoiceState)
                );
            });
        }

        if (filter === "CANCELLED") {
            result = result.filter(
                (order) => order.state === "cancel"
            );
        }

        // ALL: no state filtering.
        return result;
    },

    // -------------------------------------------------
    // LOAD DELIVERY ORDERS DOMAIN
    // -------------------------------------------------

    _computeSyncedOrdersDomain() {

        const domain = super._computeSyncedOrdersDomain(...arguments);

        // Normal Orders: preserve original Odoo behavior.
        if (!this.pos.deliveryOrdersMode) {
            return domain;
        }

        // Remove state restrictions in delivery mode.
        const filteredDomain = domain.filter((condition) => {
            return !(
                Array.isArray(condition) &&
                condition[0] === "state"
            );
        });

        // Only delivery product restriction.
        // No order state restriction for ALL.
        return [
            ...filteredDomain,
            "|",
            ["lines.product_id", "=", DELIVERY_PRODUCT_ID],
            ["lines.product_id.barcode", "=", DELIVERY_PRODUCT_BARCODE],
        ];
    },

    // -------------------------------------------------
    // OPEN ORDER IN BACKEND
    // -------------------------------------------------

    onOpenDeliveryOrderBackend(order) {

        if (!order) {
            this.env.services.notification.add(
                "لم يتم تحديد الأوردر",
                { type: "warning" }
            );
            return;
        }

        const orderId = Number(order.id);

        if (!Number.isInteger(orderId) || orderId <= 0) {
            this.env.services.notification.add(
                "This order has not been saved or processed yet.",
                { type: "warning" }
            );
            return;
        }

        window.open(
            `/web#id=${orderId}&model=pos.order&view_type=form`,
            "_blank"
        );
    },

});

console.log("POS Delivery Orders: Loaded Successfully");
