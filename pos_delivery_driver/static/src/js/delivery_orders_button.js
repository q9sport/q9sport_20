/** @odoo-module **/
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";
import { patch } from "@web/core/utils/patch";

const DELIVERY_PRODUCT_ID = 38;
const DELIVERY_PRODUCT_BARCODE = "10002";

const DELIVERY_ORDER_STATES = [
    "draft",
    "cancel",
    "paid",
    "done",
];

// =====================================================
// NAVBAR - DELIVERY ORDERS BUTTON
// =====================================================

patch(Navbar.prototype, {

    onDeliveryOrdersClick() {
        // Activate delivery-only mode
        this.pos.deliveryOrdersMode = true;

        // Open the original Odoo Orders screen
        this.pos.navigate("TicketScreen");
    },

    onTicketButtonClick() {
        // Restore normal Orders screen
        this.pos.deliveryOrdersMode = false;

        return super.onTicketButtonClick(...arguments);
    },

    onClickRegister() {
        // Restore normal Register screen
        this.pos.deliveryOrdersMode = false;

        return super.onClickRegister(...arguments);
    },

});

// =====================================================
// TICKET SCREEN - FILTER DELIVERY ORDERS
// =====================================================

patch(TicketScreen.prototype, {

    // -------------------------------------------------
    // FILTER LOADED ORDERS
    // -------------------------------------------------

    getFilteredOrderList() {
        const orders = super.getFilteredOrderList(...arguments);

        if (!this.pos.deliveryOrdersMode) {
            return orders;
        }

        return orders.filter((order) => {

            // Check order state
            const orderState = order.state;

            if (!DELIVERY_ORDER_STATES.includes(orderState)) {
                return false;
            }

            // Check delivery product
            const lines = order.lines || [];

            const hasDeliveryProduct = lines.some((line) => {
                const product = line.product_id;

                return (
                    product?.id === DELIVERY_PRODUCT_ID ||
                    product?.barcode === DELIVERY_PRODUCT_BARCODE
                );
            });

            return hasDeliveryProduct;
        });
    },

    // -------------------------------------------------
    // DOMAIN - LOAD SYNCED DELIVERY ORDERS
    // -------------------------------------------------

    _computeSyncedOrdersDomain() {
        const domain = super._computeSyncedOrdersDomain(...arguments);

        if (!this.pos.deliveryOrdersMode) {
            return domain;
        }

        // Remove the default state condition
        const filteredDomain = domain.filter((condition) => {
            return !(
                Array.isArray(condition) &&
                condition[0] === "state"
            );
        });

        // Include all requested states and delivery product
        return [
            ...filteredDomain,

            ["state", "in", DELIVERY_ORDER_STATES],

            "|",
            ["lines.product_id", "=", DELIVERY_PRODUCT_ID],
            ["lines.product_id.barcode", "=", DELIVERY_PRODUCT_BARCODE],
        ];
    },

    // -------------------------------------------------
    // OPEN SELECTED DELIVERY ORDER IN BACKEND
    // -------------------------------------------------

    onOpenDeliveryOrderBackend(order) {

        if (!order) {
            this.env.services.notification.add(
                "لم يتم تحديد الأوردر",
                { type: "warning" }
            );
            return;
        }

        // Diagnostic information
        console.log("DELIVERY ROW ORDER", {
            id: order?.id,
            uuid: order?.uuid,
            name: order?.pos_reference,
            state: order?.state,
        });

        const orderId = Number(order.id);

        if (!Number.isInteger(orderId) || orderId <= 0) {
            this.env.services.notification.add(
                "This order has not been saved or processed yet.",
                { type: "warning" }
            );
            return;
        }

        // Open the exact selected POS order in backend
        const url = `/web#id=${orderId}&model=pos.order&view_type=form`;

        window.open(url, "_blank");
    },

});

console.log("POS Delivery Orders: Loaded Successfully");
