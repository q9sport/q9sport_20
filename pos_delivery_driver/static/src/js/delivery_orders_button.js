/** @odoo-module **/
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";
import { patch } from "@web/core/utils/patch";
import { fuzzyLookup } from "@web/core/utils/search";
import { _t } from "@web/core/l10n/translation";

const DELIVERY_PRODUCT_ID = 38;
const DELIVERY_PRODUCT_BARCODE = "10002";

const DELIVERY_FILTERS = {
    ALL: "DELIVERY_ALL",
    ACTIVE: "ACTIVE_ORDERS",
    INVOICE_UNPAID: "INVOICE_UNPAID",
    CANCELLED: "CANCELLED",
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
    return order.state === "paid" || order.state === "done";
}

function isCancelledOrder(order) {
    return order.state === "cancel";
}

function isInvoiceUnpaid(order) {
    const invoiceState =
        order.account_move_payment_state ||
        order.account_move?.payment_state;

    return (
        isPaidOrder(order) &&
        ["not_paid", "partial"].includes(invoiceState)
    );
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
    // INITIALIZE DELIVERY FILTER
    // -------------------------------------------------

    setup() {
        super.setup(...arguments);

        if (this.pos.deliveryOrdersMode) {
            this.state.filter = DELIVERY_FILTERS.ALL;
            this.pos.deliveryOrdersFilter = DELIVERY_FILTERS.ALL;
        }
    },

    // -------------------------------------------------
    // FILTER DROPDOWN OPTIONS
    // -------------------------------------------------

    _getOrderStates() {
        if (!this.pos.deliveryOrdersMode) {
            return super._getOrderStates(...arguments);
        }

        // Odoo expects a Map with { text: ... } values.
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

        this.state.filter = selectedFilter || DELIVERY_FILTERS.ALL;
        this.state.page = 1;

        this.pos.deliveryOrdersFilter = this.state.filter;

        const screenState = this.pos.screenState.ticketScreen;
        screenState.totalCount = 0;
        screenState.offsetByDomain = {};

        await this._updateSyncedOrders();
        this.updateOrderTimers();
    },

    // -------------------------------------------------
    // DELIVERY ORDERS LIST
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
                    return (
                        !isPaidOrder(order) &&
                        !isCancelledOrder(order)
                    );

                case DELIVERY_FILTERS.INVOICE_UNPAID:
                    return isInvoiceUnpaid(order);

                case DELIVERY_FILTERS.CANCELLED:
                    return isCancelledOrder(order);

                case DELIVERY_FILTERS.ALL:
                default:
                    // ALL includes every order state.
                    return true;
            }
        });

        // Keep Odoo search input behavior.
        if (this.state.search?.searchTerm) {
            const searchField =
                this._getSearchFields()[this.state.search.fieldName];

            if (searchField?.repr) {
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

        if (this.state.selectedPreset) {
            orders = orders.filter(
                (order) =>
                    order.preset_id?.id === this.state.selectedPreset.id
            );
        }

        // Keep Odoo's normal order sorting.
        orders.sort((a, b) => {
            const dateA = a.date_order;
            const dateB = b.date_order;

            if (!dateA.equals(dateB)) {
                return dateB - dateA;
            }

            const nameA =
                parseInt(a.pos_reference?.replace(/\D/g, "")) || 0;
            const nameB =
                parseInt(b.pos_reference?.replace(/\D/g, "")) || 0;

            return nameB - nameA;
        });

        this.pos.screenState.ticketScreen.totalCount = orders.length;

        const start = (this.state.page - 1) * this.state.nbrByPage;
        const end = this.state.page * this.state.nbrByPage;

        return orders.slice(start, end);
    },

    // -------------------------------------------------
    // DELIVERY SEARCH DOMAIN
    // -------------------------------------------------

    _computeSyncedOrdersDomain() {
        const domain = super._computeSyncedOrdersDomain(...arguments);

        if (!this.pos.deliveryOrdersMode) {
            return domain;
        }

        // Search domain + delivery product restriction.
        return [
            ...domain,
            ["lines.product_id", "=", DELIVERY_PRODUCT_ID],
        ];
    },

    // -------------------------------------------------
    // LOAD SERVER ORDERS
    // -------------------------------------------------

    async _updateSyncedOrders() {
        if (!this.pos.deliveryOrdersMode) {
            return super._updateSyncedOrders(...arguments);
        }

        const filter = this.state.filter || DELIVERY_FILTERS.ALL;

        // Active orders are already available in the POS local models.
        // Paid and cancelled orders must be fetched from the server.
        let stateFilters = [];

        if (filter === DELIVERY_FILTERS.ALL) {
            stateFilters = ["paid", "cancelled"];
        } else if (filter === DELIVERY_FILTERS.INVOICE_UNPAID) {
            stateFilters = ["paid"];
        } else if (filter === DELIVERY_FILTERS.CANCELLED) {
            stateFilters = ["cancelled"];
        }

        if (!stateFilters.length) {
            return;
        }

        const screenState = this.pos.screenState.ticketScreen;
        const domain = this._computeSyncedOrdersDomain();
        const config_id = this.pos.config.id;

        const requests = stateFilters.map(async (state_filter) => {
            const domainKey = JSON.stringify({
                domain,
                state_filter,
            });

            const offset =
                screenState.offsetByDomain[domainKey] || 0;

            const result = await this.pos.data.call(
                "pos.order",
                "search_order_ids",
                [],
                {
                    config_id,
                    domain,
                    limit: this.state.nbrByPage,
                    offset,
                    state_filter,
                }
            );

            const { ordersInfo = [] } = result;

            screenState.offsetByDomain[domainKey] =
                offset + ordersInfo.length;

            const idsToLoad = ordersInfo
                .filter((orderInfo) => {
                    const existingOrder =
                        this.pos.models["pos.order"].get(orderInfo[0]);

                    return !existingOrder;
                })
                .map((orderInfo) => orderInfo[0]);

            if (idsToLoad.length) {
                await this.pos.data.loadServerOrders([
                    ["id", "in", [...new Set(idsToLoad)]],
                ]);
            }

            return result.totalCount || 0;
        });

        await Promise.all(requests);

        // The visible count is recalculated by getFilteredOrderList().
        this.getFilteredOrderList();
    },

    // -------------------------------------------------
    // OPEN ORDER IN BACKEND
    // -------------------------------------------------

    onOpenDeliveryOrderBackend(order) {
        if (!order) {
            this.env.services.notification.add(
                _t("No order was selected."),
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
