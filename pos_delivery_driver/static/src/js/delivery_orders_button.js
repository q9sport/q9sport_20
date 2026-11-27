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
    ACTIVE: "DELIVERY_ACTIVE",
    INVOICE_UNPAID: "DELIVERY_INVOICE_UNPAID",
    CANCELLED: "DELIVERY_CANCELLED",
};

// =====================================================
// DELIVERY HELPERS
// =====================================================

function hasDeliveryProduct(order) {
    const lines =
        typeof order.getOrderlines === "function"
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

function isPaidOrder(order) {
    return ["paid", "done"].includes(order.state);
}

function isCancelledOrder(order) {
    return order.state === "cancel";
}

function isActiveOrder(order) {
    return (
        !isPaidOrder(order) &&
        !isCancelledOrder(order)
    );
}

function isInvoiceUnpaid(order) {
    if (!isPaidOrder(order)) {
        return false;
    }

    const invoiceState =
        order.account_move?.payment_state ||
        order.account_move_payment_state;

    return ["not_paid", "partial"].includes(invoiceState);
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
    // OPEN DELIVERY SCREEN WITH ALL FILTER
    // -------------------------------------------------

    onMounted() {
        if (this.pos.deliveryOrdersMode) {
            this.pos.deliveryOrdersFilter = DELIVERY_FILTERS.ALL;
            return this.onFilterSelected(DELIVERY_FILTERS.ALL);
        }

        return super.onMounted(...arguments);
    },

    // -------------------------------------------------
    // DELIVERY SEARCH FILTER OPTIONS
    // -------------------------------------------------

    _getFilterOptions() {
        if (!this.pos.deliveryOrdersMode) {
            return super._getFilterOptions(...arguments);
        }

        return new Map([
            [DELIVERY_FILTERS.ALL, { text: _t("All") }],
            [DELIVERY_FILTERS.ACTIVE, { text: _t("Active") }],
            [
                DELIVERY_FILTERS.INVOICE_UNPAID,
                { text: _t("Not Paid Invoice") },
            ],
            [DELIVERY_FILTERS.CANCELLED, { text: _t("Cancelled") }],
        ]);
    },

    // -------------------------------------------------
    // SEARCH BAR CONFIGURATION
    // -------------------------------------------------

    getSearchBarConfig() {
        const config = super.getSearchBarConfig(...arguments);

        if (!this.pos.deliveryOrdersMode) {
            return config;
        }

        return {
            ...config,
            defaultFilter:
                this.state.filter || DELIVERY_FILTERS.ALL,
        };
    },

    // -------------------------------------------------
    // FILTER SELECTION
    // -------------------------------------------------

    async onFilterSelected(selectedFilter) {
        if (!this.pos.deliveryOrdersMode) {
            return super.onFilterSelected(...arguments);
        }

        this.pos.deliveryOrdersFilter = selectedFilter;
        this.state.filter = selectedFilter;
        this.state.page = 1;

        this.pos.screenState.ticketScreen.totalCount = 0;
        this.pos.screenState.ticketScreen.offsetByDomain = {};

        await this._updateSyncedOrders();
        this.updateOrderTimers();
    },

    // -------------------------------------------------
    // DELIVERY ORDERS DOMAIN
    // -------------------------------------------------

    _computeSyncedOrdersDomain() {
        const domain = super._computeSyncedOrdersDomain(...arguments);

        if (!this.pos.deliveryOrdersMode) {
            return domain;
        }

        return [
            ...domain,
            "|",
            ["lines.product_id", "=", DELIVERY_PRODUCT_ID],
            ["lines.product_id.barcode", "=", DELIVERY_PRODUCT_BARCODE],
        ];
    },

    // -------------------------------------------------
    // LOAD ALL DELIVERY ORDERS
    // -------------------------------------------------

    async _updateSyncedOrders() {
        if (!this.pos.deliveryOrdersMode) {
            return super._updateSyncedOrders(...arguments);
        }

        const filter = this.state.filter || DELIVERY_FILTERS.ALL;

        // Active orders are already available in the POS local model.
        if (filter === DELIVERY_FILTERS.ACTIVE) {
            return;
        }

        // Load paid and cancelled orders from the server.
        // Odoo's standard search_order_ids supports these two
        // state_filter values.
        const loadAllForState = async (stateFilter) => {
            const screenState = this.pos.screenState.ticketScreen;

            screenState.offsetByDomain = {};

            const previousFilter = this.state.filter;
            this.state.filter =
                stateFilter === "cancelled" ? "CANCELLED" : "SYNCED";

            try {
                let totalCount = 0;
                let loadedCount = 0;

                do {
                    await super._updateSyncedOrders();

                    totalCount = screenState.totalCount || 0;

                    const domain = this._computeSyncedOrdersDomain();
                    loadedCount =
                        screenState.offsetByDomain[JSON.stringify(domain)] || 0;

                    if (!totalCount || loadedCount >= totalCount) {
                        break;
                    }
                } while (loadedCount < totalCount);
                
                return totalCount;
            } finally {
                this.state.filter = previousFilter;
            }
        };

        if (filter === DELIVERY_FILTERS.CANCELLED) {
            await loadAllForState("cancelled");
            return;
        }

        if (filter === DELIVERY_FILTERS.INVOICE_UNPAID) {
            await loadAllForState("paid");
            return;
        }

        if (filter === DELIVERY_FILTERS.ALL) {
            const paidCount = await loadAllForState("paid");
            const cancelledCount = await loadAllForState("cancelled");

            const activeCount = this.pos.models["pos.order"].filter(
                (order) =>
                    hasDeliveryProduct(order) &&
                    isActiveOrder(order)
            ).length;

            this.pos.screenState.ticketScreen.totalCount =
                paidCount + cancelledCount + activeCount;
        }
    },

    // -------------------------------------------------
    // FILTER DISPLAYED ORDERS
    // -------------------------------------------------

    getFilteredOrderList() {
        if (!this.pos.deliveryOrdersMode) {
            return super.getFilteredOrderList(...arguments);
        }

        const filter = this.state.filter || DELIVERY_FILTERS.ALL;

        let orders = this.pos.models["pos.order"].filter((order) => {
            if (!hasDeliveryProduct(order)) {
                return false;
            }

            switch (filter) {
                case DELIVERY_FILTERS.ACTIVE:
                    return isActiveOrder(order);

                case DELIVERY_FILTERS.INVOICE_UNPAID:
                    return isInvoiceUnpaid(order);

                case DELIVERY_FILTERS.CANCELLED:
                    return isCancelledOrder(order);

                case DELIVERY_FILTERS.ALL:
                default:
                    return true;
            }
        });

        // Keep the standard search text functionality.
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
            const dateA = a.date_order;
            const dateB = b.date_order;

            if (dateA?.equals && !dateA.equals(dateB)) {
                return dateB - dateA;
            }

            return (b.id || 0) - (a.id || 0);
        });

        this.pos.screenState.ticketScreen.totalCount = orders.length;

        return orders.slice(
            (this.state.page - 1) * this.state.nbrByPage,
            this.state.page * this.state.nbrByPage
        );
    },

    // -------------------------------------------------
    // OPEN ORDER IN BACKEND
    // -------------------------------------------------

    onOpenDeliveryOrderBackend(order) {
        if (!order) {
            this.env.services.notification.add(
                _t("No order selected."),
                { type: "warning" }
            );
            return;
        }

        const orderId = Number(order.id);

        if (!Number.isInteger(orderId) || orderId <= 0) {
            this.env.services.notification.add(
                _t("This order has not been saved or synchronized yet."),
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
