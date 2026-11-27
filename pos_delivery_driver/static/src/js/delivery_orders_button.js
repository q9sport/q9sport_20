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
        const productId =
            typeof product === "object" ? product?.id : product;

        return (
            Number(productId) === DELIVERY_PRODUCT_ID ||
            product?.barcode === DELIVERY_PRODUCT_BARCODE
        );
    });
}

function getOrderTimestamp(order) {
    const date = order.date_order;

    if (!date) {
        return 0;
    }

    if (typeof date.toMillis === "function") {
        return date.toMillis();
    }

    if (typeof date.toJSDate === "function") {
        return date.toJSDate().getTime();
    }

    if (date instanceof Date) {
        return date.getTime();
    }

    return new Date(date).getTime() || 0;
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
    // INITIALIZE DELIVERY MODE
    // -------------------------------------------------

    setup() {
        super.setup(...arguments);

        if (this.pos.deliveryOrdersMode) {
            this.pos.deliveryOrdersFilter =
                this.pos.deliveryOrdersFilter || DELIVERY_FILTERS.ALL;
        }
    },

    // -------------------------------------------------
    // DELIVERY ORDERS LIST
    // -------------------------------------------------

    getFilteredOrderList() {
        if (!this.pos.deliveryOrdersMode) {
            return super.getFilteredOrderList(...arguments);
        }

        let orders = this.pos.models["pos.order"].filter((order) =>
            hasDeliveryProduct(order)
        );

        // Preserve the standard POS search behavior.
        if (this.state.search?.searchTerm) {
            const searchFields = this._getSearchFields();
            const searchField =
                searchFields?.[this.state.search.fieldName];

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

        // Newest orders first.
        orders.sort((a, b) => {
            const dateDifference =
                getOrderTimestamp(b) - getOrderTimestamp(a);

            if (dateDifference !== 0) {
                return dateDifference;
            }

            const nameA =
                parseInt(a.pos_reference?.replace(/\D/g, "")) || 0;
            const nameB =
                parseInt(b.pos_reference?.replace(/\D/g, "")) || 0;

            return nameB - nameA;
        });

        const screenState = this.pos.screenState.ticketScreen;
        screenState.totalCount = orders.length;

        const page = Math.max(1, this.state.page || 1);
        const pageSize = Math.max(1, this.state.nbrByPage || 1);

        const start = (page - 1) * pageSize;
        const end = page * pageSize;

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

        return [
            ...domain,
            ["lines.product_id", "=", DELIVERY_PRODUCT_ID],
        ];
    },

    // -------------------------------------------------
    // LOAD SERVER DELIVERY ORDERS
    // -------------------------------------------------

    async _updateSyncedOrders() {
        if (!this.pos.deliveryOrdersMode) {
            return super._updateSyncedOrders(...arguments);
        }

        const screenState = this.pos.screenState.ticketScreen;
        const domain = this._computeSyncedOrdersDomain();
        const config_id = this.pos.config.id;

        // Load paid and cancelled orders from the server.
        // Active orders are already available in the POS local models.
        const stateFilters = ["paid", "cancelled"];

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

            const { ordersInfo = [] } = result || {};

            screenState.offsetByDomain[domainKey] =
                offset + ordersInfo.length;

            const idsToLoad = ordersInfo
                .filter((orderInfo) => {
                    const orderId = orderInfo[0];

                    return !this.pos.models["pos.order"].get(orderId);
                })
                .map((orderInfo) => orderInfo[0]);

            if (idsToLoad.length) {
                await this.pos.data.loadServerOrders([
                    ["id", "in", [...new Set(idsToLoad)]],
                ]);
            }

            return result?.totalCount || 0;
        });

        await Promise.all(requests);

        // Refresh the visible delivery order list.
        this.getFilteredOrderList();
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

        if (!Number.isInteger(orderId) || orderId <= 0) {
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
