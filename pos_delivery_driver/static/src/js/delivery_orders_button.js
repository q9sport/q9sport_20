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
// NAVBAR - DELIVERY + CURRENT ORDER ACTIONS
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

    onClearProductSearch() {
        this.pos.searchProductWord = "";

        if (this.inputRef) {
            const input = this.inputRef();
            if (input) {
                input.focus();
            }
        }
    },

    // -------------------------------------------------
    // CANCEL CURRENT ACTIVE ORDER
    // Uses Odoo 20 native delete confirmation/workflow
    // -------------------------------------------------

    async onCancelCurrentOrder() {
        const order = this.pos.getOrder();

        if (!order) {
            this.notification.add(
                "لا يوجد طلب حالي لإلغائه",
                { type: "warning" }
            );
            return;
        }

        if (order.finalized) {
            this.notification.add(
                "لا يمكن إلغاء طلب مدفوع أو مغلق من هنا",
                { type: "warning" }
            );
            return;
        }

        try {
            await this.pos.onDeleteOrder(order);
        } catch (error) {
            console.error(
                "Q9SPORT: Current order cancellation failed",
                error
            );

            this.notification.add(
                "تعذر إلغاء الطلب الحالي",
                { type: "danger" }
            );
        }
    },

});

// =====================================================
// ORDER SUMMARY - DELETE INDIVIDUAL PRODUCT LINE
// =====================================================

patch(OrderSummary.prototype, {

    onRemoveOrderLine(line) {
        const order = this.currentOrder;

        if (!order || !line) {
            return;
        }

        try {
            order.assertEditable();

            // If this is a combo child, remove its parent.
            const targetLine = line.combo_parent_id || line;

            order.removeOrderline(targetLine);

            this.numberBuffer.reset();

        } catch (error) {
            console.error(
                "Q9SPORT: Unable to remove order line",
                error
            );

            this.env.services.notification.add(
                "لا يمكن حذف هذا المنتج",
                { type: "warning" }
            );
        }
    },

});


// =====================================================
// TICKET SCREEN - DELIVERY ORDERS
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

        const url =
            `/web#id=${orderId}&model=pos.order&view_type=form`;

        window.open(url, "_blank");
    },

});

console.log("Q9SPORT POS Delivery Orders: Loaded Successfully");
console.log("Q9SPORT POS Order Actions: Loaded Successfully");
