
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

function getRelationId(value) {
    if (!value) {
        return false;
    }
    return typeof value === "object" ? value.id : value;
}

function hasDeliveryProduct(order) {
    const lines =
        typeof order.getOrderlines === "function"
            ? order.getOrderlines()
            : order.lines || [];

    return lines.some((line) => {
        const product = line.product_id;

        return (
            getRelationId(product) === DELIVERY_PRODUCT_ID ||
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

function isInvoiceUnpaid(order) {
    const invoiceState =
        order.account_move_payment_state ||
        order.account_move?.payment_state;

    return (
        isPaidOrder(order) &&
        ["not_paid", "partial", "in_payment"].includes(invoiceState)
    );
}

function getOrderTimestamp(order) {
    const date = order.date_order;

    if (typeof date?.ts === "number") {
        return date.ts;
    }

    if (date instanceof Date) {
        return date.getTime();
    }

    return Date.parse(date || "") || 0;
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
    // SETUP
    // -------------------------------------------------

    setup() {
        super.setup(...arguments);

        this.deliveryServerCounts = {
            paid: 0,
            cancelled: 0,
        };

        if (this.pos.deliveryOrdersMode) {
            this.state.filter = DELIVERY_FILTERS.ALL;
            this.state.page = 1;
            this.pos.deliveryOrdersFilter = DELIVERY_FILTERS.ALL;
        } else if (
            [
                DELIVERY_FILTERS.ALL,
                DELIVERY_FILTERS.INVOICE_UNPAID,
            ].includes(this.state.filter)
        ) {
            // Never leave a custom filter selected
            // when returning to the normal Orders screen.
            this.state.filter = DELIVERY_FILTERS.ACTIVE;
        }
    },

    // -------------------------------------------------
    // FILTER OPTIONS
    // -------------------------------------------------

    _getOrderStates() {
        const states = super._getOrderStates();

        if (!this.pos.deliveryOrdersMode) {
            return states;
        }

        states.set(DELIVERY_FILTERS.ALL, {
            text: _t("All Delivery Orders"),
        });

        states.set(DELIVERY_FILTERS.INVOICE_UNPAID, {
            text: _t("Not Paid Invoice"),
        });

        return states;
    },

    _getFilterOptions() {
        const originalOptions = super._getFilterOptions();

        if (!this.pos.deliveryOrdersMode) {
            return originalOptions;
        }

        const options = new Map();

        options.set(
            DELIVERY_FILTERS.ALL,
            originalOptions.get(DELIVERY_FILTERS.ALL) ||
                { text: _t("All Delivery Orders") }
        );

        options.set(
            DELIVERY_FILTERS.ACTIVE,
            originalOptions.get(DELIVERY_FILTERS.ACTIVE) ||
                { text: _t("Active") }
        );

        options.set(
            DELIVERY_FILTERS.INVOICE_UNPAID,
            {
                text: _t("Not Paid Invoice"),
            }
        );

        options.set(
            DELIVERY_FILTERS.CANCELLED,
            originalOptions.get(DELIVERY_FILTERS.CANCELLED) ||
                { text: _t("Cancelled") }
        );

        return options;
    },

    // -------------------------------------------------
    // FILTER SELECTION
    // -------------------------------------------------

    async onFilterSelected(selectedFilter) {
        if (!this.pos.deliveryOrdersMode) {
            return super.onFilterSelected(...arguments);
        }

        const validFilters = Object.values(DELIVERY_FILTERS);

        this.state.filter = validFilters.includes(selectedFilter)
            ? selectedFilter
            : DELIVERY_FILTERS.ALL;

        this.state.page = 1;

        this.pos.deliveryOrdersFilter = this.state.filter;

        const screenState = this.pos.screenState.ticketScreen;

        screenState.totalCount = 0;
        screenState.offsetByDomain = {};

        this.deliveryServerCounts = {
            paid: 0,
            cancelled: 0,
        };

        await this._updateSyncedOrders();

        this.updateOrderTimers();
    },

    // -------------------------------------------------
    // SERVER DOMAIN
    // -------------------------------------------------

    _computeSyncedOrdersDomain() {
        const domain = super._computeSyncedOrdersDomain();

        if (!this.pos.deliveryOrdersMode) {
            return domain;
        }

        const sessionId = getRelationId(this.pos.session);

        const deliveryDomain = [
            ["lines.product_id", "=", DELIVERY_PRODUCT_ID],
        ];

        if (sessionId) {
            deliveryDomain.push(["session_id", "=", sessionId]);
        }

        if (
            this.state.filter === DELIVERY_FILTERS.INVOICE_UNPAID
        ) {
            deliveryDomain.push([
                "account_move.payment_state",
                "in",
                ["not_paid", "partial", "in_payment"],
            ]);
        }

        return [...domain, ...deliveryDomain];
    },

    // -------------------------------------------------
    // LOAD SERVER ORDERS
    // -------------------------------------------------

    async _updateSyncedOrders() {
        if (!this.pos.deliveryOrdersMode) {
            return super._updateSyncedOrders(...arguments);
        }

        const filter = this.state.filter || DELIVERY_FILTERS.ALL;

        let stateFilters = [];

        if (filter === DELIVERY_FILTERS.ALL) {
            stateFilters = ["paid", "cancelled"];
        } else if (filter === DELIVERY_FILTERS.INVOICE_UNPAID) {
            stateFilters = ["paid"];
        } else if (filter === DELIVERY_FILTERS.CANCELLED) {
            stateFilters = ["cancelled"];
        }

        if (!stateFilters.length) {
            this.deliveryServerCounts = {
                paid: 0,
                cancelled: 0,
            };
            return;
        }

        const screenState = this.pos.screenState.ticketScreen;
        const domain = this._computeSyncedOrdersDomain();

        const configId = this.pos.config.id;

        // Load the corresponding page independently
        // for paid and cancelled server orders.
        const offset =
            (this.state.page - 1) * this.state.nbrByPage;

        const requests = stateFilters.map(async (stateFilter) => {
            const domainKey = JSON.stringify({
                domain,
                stateFilter,
            });

            const result = await this.pos.data.call(
                "pos.order",
                "search_order_ids",
                [],
                {
                    config_id: configId,
                    domain,
                    limit: this.state.nbrByPage,
                    offset,
                    state_filter: stateFilter,
                }
            );

            const ordersInfo = result?.ordersInfo || [];

            this.deliveryServerCounts[stateFilter] =
                result?.totalCount || 0;

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

            screenState.offsetByDomain[domainKey] =
                offset + ordersInfo.length;
        });

        await Promise.all(requests);
    },

    // -------------------------------------------------
    // DELIVERY ORDERS LIST
    // -------------------------------------------------

    getFilteredOrderList() {
        if (!this.pos.deliveryOrdersMode) {
            return super.getFilteredOrderList(...arguments);
        }

        const filter = this.state.filter || DELIVERY_FILTERS.ALL;

        const sessionId = getRelationId(this.pos.session);

        let orders = this.pos.models["pos.order"].filter((order) => {
            if (!hasDeliveryProduct(order)) {
                return false;
            }

            // Only orders belonging to the current POS session.
            if (sessionId) {
                const orderSessionId = getRelationId(order.session_id);

                if (orderSessionId !== sessionId) {
                    return false;
                }
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
                    return true;
            }
        });

        // SEARCH
        const search = this.state.search || {};

        if (search.searchTerm && search.fieldName) {
            const searchField =
                this._getSearchFields()[search.fieldName];

            if (searchField?.repr) {
                orders = fuzzyLookup(
                    search.searchTerm,
                    orders,
                    searchField.repr
                );
            }
        }

        if (
            search.partnerId &&
            search.fieldName === "PARTNER"
        ) {
            orders = orders.filter(
                (order) =>
                    getRelationId(order.partner_id) === search.partnerId
            );
        }

        // PRESET
        if (this.state.selectedPreset) {
            orders = orders.filter(
                (order) =>
                    getRelationId(order.preset_id) ===
                    this.state.selectedPreset.id
            );
        }

        // SORT
        orders.sort((a, b) => {
            const dateA = getOrderTimestamp(a);
            const dateB = getOrderTimestamp(b);

            if (dateA !== dateB) {
                return dateB - dateA;
            }

            const nameA =
                parseInt((a.pos_reference || "").replace(/\D/g, "")) || 0;

            const nameB =
                parseInt((b.pos_reference || "").replace(/\D/g, "")) || 0;

            return nameB - nameA;
        });

        // TOTAL COUNT
        const activeCount = orders.filter(
            (order) =>
                !isPaidOrder(order) &&
                !isCancelledOrder(order)
        ).length;

        if (filter === DELIVERY_FILTERS.ALL) {
            this.pos.screenState.ticketScreen.totalCount =
                this.deliveryServerCounts.paid +
                this.deliveryServerCounts.cancelled +
                activeCount;
        } else if (filter === DELIVERY_FILTERS.ACTIVE) {
            this.pos.screenState.ticketScreen.totalCount =
                orders.length;
        } else if (filter === DELIVERY_FILTERS.INVOICE_UNPAID) {
            this.pos.screenState.ticketScreen.totalCount =
                this.deliveryServerCounts.paid;
        } else if (filter === DELIVERY_FILTERS.CANCELLED) {
            this.pos.screenState.ticketScreen.totalCount =
                this.deliveryServerCounts.cancelled;
        }

        const start =
            (this.state.page - 1) * this.state.nbrByPage;

        const end =
            this.state.page * this.state.nbrByPage;

        return orders.slice(start, end);
    },

    // -------------------------------------------------
    // OPEN DELIVERY ORDER IN BACKEND
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

        if (
            !Number.isInteger(orderId) ||
            orderId <= 0
        ) {
            this.env.services.notification.add(
                _t(
                    "This order has not been saved or synchronized yet."
                ),
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
