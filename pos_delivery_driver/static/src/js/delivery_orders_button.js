/** @odoo-module **/
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { fuzzyLookup } from "@web/core/utils/search";

const DELIVERY_PRODUCT_ID = 38;
const DELIVERY_PRODUCT_BARCODE = "10002";

const DELIVERY_FILTERS = {
    ALL: "DELIVERY_ALL",
    UNPAID: "DELIVERY_UNPAID",
    INVOICE_UNPAID: "DELIVERY_INVOICE_UNPAID",
    CANCELLED: "DELIVERY_CANCELLED",
};

// =====================================================
// HELPERS
// =====================================================

function isDeliveryOrder(order) {
    const lines = order.getOrderlines
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

function isInvoiceUnpaid(order) {
    const invoiceState =
        order.account_move_payment_state ||
        order.account_move?.payment_state;

    return (
        order.finalized &&
        ["not_paid", "partial"].includes(invoiceState)
    );
}

function isUnpaidOrder(order) {
    return (
        !order.finalized &&
        order.state !== "cancel"
    );
}

// =====================================================
// NAVBAR
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

});

// =====================================================
// TICKET SCREEN
// =====================================================

patch(TicketScreen.prototype, {

    // -------------------------------------------------
    // INITIALIZE DELIVERY FILTER
    // -------------------------------------------------

    setup() {
        super.setup(...arguments);

        if (this.pos.deliveryOrdersMode) {
            this.state.filter = DELIVERY_FILTERS.ALL;
        }
    },

    // -------------------------------------------------
    // REPLACE DROPDOWN OPTIONS
    // -------------------------------------------------

    _getOrderStates() {

        if (!this.pos.deliveryOrdersMode) {
            return super._getOrderStates(...arguments);
        }

        return new Map([
            [
                DELIVERY_FILTERS.ALL,
                { text: _t("All") },
            ],
            [
                DELIVERY_FILTERS.UNPAID,
                { text: _t("Unpaid") },
            ],
            [
                DELIVERY_FILTERS.INVOICE_UNPAID,
                { text: _t("Paid - Invoice Unpaid") },
            ],
            [
                DELIVERY_FILTERS.CANCELLED,
                { text: _t("Cancelled") },
            ],
        ]);
    },

    _getFilterOptions() {

        if (!this.pos.deliveryOrdersMode) {
            return super._getFilterOptions(...arguments);
        }

        return this._getOrderStates();
    },

    // -------------------------------------------------
    // FILTER SELECTION
    // -------------------------------------------------

    async onFilterSelected(selectedFilter) {

        if (!this.pos.deliveryOrdersMode) {
            return super.onFilterSelected(...arguments);
        }

        this.state.filter =
            Object.values(DELIVERY_FILTERS).includes(selectedFilter)
                ? selectedFilter
                : DELIVERY_FILTERS.ALL;

        this.state.page = 1;

        this.pos.screenState.ticketSCreen.totalCount = 0;
        this.pos.screenState.ticketSCreen.offsetByDomain = {};

        await this._fetchSyncedOrders();
    },

    // -------------------------------------------------
    // SYNCED ORDERS DOMAIN
    // -------------------------------------------------

    _computeSyncedOrdersDomain() {

        const domain = super._computeSyncedOrdersDomain(...arguments);

        if (!this.pos.deliveryOrdersMode) {
            return domain;
        }

        const filter = this.state.filter;

        // Every result must contain the delivery product.
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
                ["account_move_payment_state", "in", ["not_paid", "partial"]],
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
    // DISPLAY DELIVERY ORDERS
    // -------------------------------------------------

    getFilteredOrderList() {

        if (!this.pos.deliveryOrdersMode) {
            return super.getFilteredOrderList(...arguments);
        }

        const filter = this.state.filter;

        // Use all orders currently available in the POS model.
        let orders = this.pos.models["pos.order"].filter(
            (order) => order.uiState?.displayed && isDeliveryOrder(order)
        );

        // Apply selected delivery filter.
        if (filter === DELIVERY_FILTERS.UNPAID) {

            orders = orders.filter(isUnpaidOrder);

        } else if (filter === DELIVERY_FILTERS.INVOICE_UNPAID) {

            orders = orders.filter(isInvoiceUnpaid);

        } else if (filter === DELIVERY_FILTERS.CANCELLED) {

            orders = orders.filter(
                (order) => order.state === "cancel"
            );

        }

        // Search bar.
        if (this.state.search?.searchTerm) {

            const searchField =
                this._getSearchFields()[this.state.search.fieldName];

            if (searchField) {
                orders = fuzzyLookup(
                    this.state.search.searchTerm,
                    orders,
                    searchField.repr
                );
            }
        }

        // Customer filter.
        if (
            this.state.search?.partnerId &&
            this.state.search.fieldName === "PARTNER"
        ) {
            orders = orders.filter(
                (order) =>
                    order.partner_id?.id === this.state.search.partnerId
            );
        }

        // Sort newest first.
        orders.sort((a, b) => {
            const dateA = a.date_order?.ts || 0;
            const dateB = b.date_order?.ts || 0;

            return dateB - dateA;
        });

        this.pos.screenState.ticketSCreen.totalCount = orders.length;

        const start =
            (this.state.page - 1) * this.state.nbrByPage;

        return orders.slice(start, start + this.state.nbrByPage);
    },

    // -------------------------------------------------
    // SEARCH - KEEP DELIVERY MODE
    // -------------------------------------------------

    async onSearch(search) {

        if (!this.pos.deliveryOrdersMode) {
            return super.onSearch(...arguments);
        }

        this.state.search = search;
        this.state.page = 1;

        await this._fetchSyncedOrders();
    },

    // -------------------------------------------------
    // OPEN SELECTED DELIVERY ORDER
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
