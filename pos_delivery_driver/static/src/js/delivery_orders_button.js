/** @odoo-module **/
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";
import { OrderSummary } from "@point_of_sale/app/screens/product_screen/order_summary/order_summary";
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
        this.pos.deliveryOrdersMode = true;
        this.pos.navigate("TicketScreen");
    },

    onTicketButtonClick() {
        this.pos.deliveryOrdersMode = false;
        return super.onTicketButtonClick(...arguments);
    },

    onClickRegister() {
        this.pos.deliveryOrdersMode = false;
        return super.onClickRegister(...arguments);
    },

    // -------------------------------------------------
    // CLEAR PRODUCT SEARCH
    // -------------------------------------------------

    clearProductSearch() {
        this.pos.searchProductWord = "";
        this.pos.searchProductDBState = null;

        if (this.inputRef && this.inputRef()) {
            this.inputRef().value = "";
        }
    },

});

// =====================================================
// ORDER SUMMARY - CURRENT ORDER ACTIONS
// =====================================================

patch(OrderSummary.prototype, {

    // -------------------------------------------------
    // DELETE ONE PRODUCT LINE
    // -------------------------------------------------

    onRemoveOrderLine(line) {
        const order = this.pos.getOrder();

        if (!order || !line) {
            return;
        }

        try {
            order.assertEditable();

            // Delete the complete combo when a combo child is selected.
            const targetLine = line.combo_parent_id || line;

            order.removeOrderline(targetLine);

            this.numberBuffer.reset();

            this.pos.numpadMode = "quantity";

        } catch (error) {
            console.error(
                "Q9SPORT: Unable to remove order line",
                error
            );

            this.pos.notification.add(
                "تعذر حذف المنتج من الطلب",
                { type: "warning" }
            );
        }
    },

    // -------------------------------------------------
    // CANCEL CURRENT WORKING ORDER
    // -------------------------------------------------

    async onCancelCurrentOrder() {
        const order = this.pos.getOrder();

        if (!order) {
            return;
        }

        try {
            order.assertEditable();

            // Native Odoo 20 confirmation and deletion workflow.
            // A new empty order is selected after successful deletion.
            await this.pos.onDeleteOrder(order);

            this.numberBuffer.reset();

        } catch (error) {
            console.error(
                "Q9SPORT: Unable to cancel current order",
                error
            );

            this.pos.notification.add(
                "تعذر إلغاء الطلب الحالي",
                { type: "warning" }
            );
        }
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

            const orderState = order.state;

            if (!DELIVERY_ORDER_STATES.includes(orderState)) {
                return false;
            }

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

        const filteredDomain = domain.filter((condition) => {
            return !(
                Array.isArray(condition) &&
                condition[0] === "state"
            );
        });

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

        const url = `/web#id=${orderId}&model=pos.order&view_type=form`;

        window.open(url, "_blank");
    },

});

console.log("Q9SPORT POS Delivery Orders: Loaded Successfully");
console.log("Q9SPORT POS Order Actions: Loaded Successfully");
