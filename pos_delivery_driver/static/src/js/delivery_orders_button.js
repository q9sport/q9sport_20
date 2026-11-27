/** @odoo-module **/
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { fuzzyLookup } from "@web/core/utils/search";

// =====================================================
// CONFIGURATION
// =====================================================

const DELIVERY_PRODUCT_ID = 38;
const DELIVERY_PRODUCT_BARCODE = "10002";

const DELIVERY_FILTERS = {
    ALL: "DELIVERY_ALL",
    UNPAID: "DELIVERY_UNPAID",
    INVOICE_UNPAID: "DELIVERY_INVOICE_UNPAID",
    CANCELLED: "DELIVERY_CANCELLED",
};

const DELIVERY_FILTER_OPTIONS = new Map([
    [DELIVERY_FILTERS.ALL, { text: _t("All") }],
    [DELIVERY_FILTERS.UNPAID, { text: _t("Unpaid") }],
    [
        DELIVERY_FILTERS.INVOICE_UNPAID,
        { text: _t("Paid - Invoice Unpaid") },
    ],
    [DELIVERY_FILTERS.CANCELLED, { text: _t("Cancelled") }],
]);

// =====================================================
// HELPERS
// =====================================================

function getOrderLines(order) {
    if (typeof order.getOrderlines === "function") {
        return order.getOrderlines();
    }

    return order.lines || [];
}

function isDeliveryOrder(order) {
    return getOrderLines(order).some((line) => {
        const product = line.product_id;

        return (
            product?.id === DELIVERY_PRODUCT_ID ||
            product?.barcode === DELIVERY_PRODUCT_BARCODE
        );
    });
}

function getInvoicePaymentState(order) {
    return (
        order.account_move_payment_state ||
        order.account_move?.payment_state ||
        null
    );
}

function isInvoiceUnpaid(order) {
    const invoiceState = getInvoicePaymentState(order);

    return (
        ["paid", "done"].includes(order.state) &&
        ["not_paid", "partial"].includes(invoiceState)
    );
}

function isUnpaidOrder(order) {
    return (
        order.state === "draft" ||
        order.state === "waiting"
    );
}

function isCancelledOrder(order) {
    return order.state === "cancel";
}

// =====================================================
// NAVBAR
// =====================================================

patch(Navbar.prototype, {

    onDeliveryOrdersClick() {
        this.pos.deliveryOrdersMode = true;
        this.pos.deliveryOrdersFilter = DELIVERY_FILTERS.ALL;

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

});

// =====================================================
// TICKET SCREEN
// =====================================================

patch(TicketScreen.prototype, {

    // -------------------------------------------------
    // FILTER INITIALIZATION
    // -------------------------------------------------

    setup() {
        super.setup(...arguments);

        if (this.pos.deliveryOrdersMode) {
            this.pos.deliveryOrdersFilter =
                DELIVERY_FILTERS.ALL;
        }
    },

    // -------------------------------------------------
    // DELIVERY FILTER OPTIONS
    // -------------------------------------------------

    _getOrderStates() {
        if (!this.pos.deliveryOrdersMode) {
            return super._getOrderStates(...arguments);
        }

        return DELIVERY_FILTER_OPTIONS;
    },

    _getFilterOptions() {
        if (!this.pos.deliveryOrdersMode) {
            return super._getFilterOptions(...arguments);
        }

        return DELIVERY_FILTER_OPTIONS;
    },

    // -------------------------------------------------
    // SELECT DELIVERY FILTER
    // -------------------------------------------------

    async onFilterSelected(selectedFilter) {
        if (!this.pos.deliveryOrdersMode) {
            return super.onFilterSelected(...arguments);
        }

        const validFilter = Object.values(DELIVERY_FILTERS)
            .includes(selectedFilter);

        this.pos.deliveryOrdersFilter = validFilter
            ? selectedFilter
            : DELIVERY_FILTERS.ALL;

        if (this.state.page !== undefined) {
            this.state.page = 1;
        }

        if (typeof this._fetchSyncedOrders === "function") {
            await this._fetchSyncedOrders();
        }
    },

    // -------------------------------------------------
    // SERVER DOMAIN
    // -------------------------------------------------

    _computeSyncedOrdersDomain() {
        const domain = super._computeSyncedOrdersDomain(...arguments);

        if (!this.pos.deliveryOrdersMode) {
            return domain;
        }

        const filter =
            this.pos.deliveryOrdersFilter || DELIVERY_FILTERS.ALL;

        const deliveryDomain = [
            "|",
            ["lines.product_id", "=", DELIVERY_PRODUCT_ID],
            ["lines.product_id.barcode", "=", DELIVERY_PRODUCT_BARCODE],
        ];

        if (filter === DELIVERY_FILTERS.UNPAID) {
            return [
                ...deliveryDomain,
                ["state", "=", "draft"],
            ];
        }

        if (filter === DELIVERY_FILTERS.INVOICE_UNPAID) {
            return [
                ...deliveryDomain,
                ["state", "in", ["paid", "done"]],
                [
                    "account_move_payment_state",
                    "in",
                    ["not_paid", "partial"],
                ],
            ];
        }

        if (filter === DELIVERY_FILTERS.CANCELLED) {
            return [
                ...deliveryDomain,
                ["state", "=", "cancel"],
            ];
        }

        // ALL: no state restriction.
        return deliveryDomain;
    },

    // -------------------------------------------------
    // FILTER LOADED ORDERS
    // -------------------------------------------------

    getFilteredOrderList() {
        if (!this.pos.deliveryOrdersMode) {
            return super.getFilteredOrderList(...arguments);
        }

        const filter =
            this.pos.deliveryOrdersFilter || DELIVERY_FILTERS.ALL;

        const orders = super.getFilteredOrderList(...arguments);

        let deliveryOrders = orders.filter(isDeliveryOrder);

        if (filter === DELIVERY_FILTERS.UNPAID) {
            deliveryOrders = deliveryOrders.filter(isUnpaidOrder);
        }

        if (filter === DELIVERY_FILTERS.INVOICE_UNPAID) {
            deliveryOrders = deliveryOrders.filter(isInvoiceUnpaid);
        }

        if (filter === DELIVERY_FILTERS.CANCELLED) {
            deliveryOrders = deliveryOrders.filter(isCancelledOrder);
        }

        return deliveryOrders;
    },

    // -------------------------------------------------
    // OPEN DELIVERY ORDER IN BACKEND
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

console.log("POS Delivery Orders Filters: Loaded Successfully");
