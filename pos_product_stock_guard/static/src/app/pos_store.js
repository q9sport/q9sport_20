import { PosStore } from "@point_of_sale/app/services/pos_store";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { ask } from "@point_of_sale/app/utils/make_awaitable_dialog";
import { _t } from "@web/core/l10n/translation";
import { markRaw } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";

// Quantities closer than this to zero count as zero (float noise on 0.1 + 0.2).
const EPSILON = 1e-6;

patch(PosStore.prototype, {
    async setup() {
        this.stockGuard = {
            // product.product id -> quantity read from the server when the register loaded
            qty: {},
            canOverride: false,
            locationName: "",
            // orders already paid when the quantities were read: their stock moves are in `qty`
            baseOrderUuids: markRaw(new Set()),
            requested: markRaw(new Set()),
        };
        return await super.setup(...arguments);
    },

    async afterProcessServerData() {
        const result = await super.afterProcessServerData(...arguments);
        for (const order of this.models["pos.order"].getAll()) {
            if (order.finalized) {
                this.stockGuard.baseOrderUuids.add(order.uuid);
            }
        }
        await this.stockGuardFetch(this.models["product.product"].getAll().map((p) => p.id));
        return result;
    },

    get stockGuardEnabled() {
        return Boolean(this.config.stock_guard_show || this.config.stock_guard_mode !== "none");
    },

    /** Read the quantities of the given products from the server (each product once). */
    async stockGuardFetch(productIds) {
        const ids = productIds.filter((id) => typeof id === "number" && !this.stockGuard.requested.has(id));
        if (!ids.length || !this.stockGuardEnabled) {
            return;
        }
        ids.forEach((id) => this.stockGuard.requested.add(id));
        try {
            const data = await this.data.call("pos.config", "get_stock_guard_data", [
                [this.config.id],
                ids,
            ]);
            Object.assign(this.stockGuard.qty, data.quantities);
            this.stockGuard.canOverride = data.can_override;
            this.stockGuard.locationName = data.location_name || "";
        } catch {
            // Offline: the products stay untracked, selling is never blocked by a missing figure.
            ids.forEach((id) => this.stockGuard.requested.delete(id));
        }
    },

    /** Quantity this register has rung up for a product since the quantities were read. */
    stockGuardUsedQty(productId) {
        let used = 0;
        for (const order of this.models["pos.order"].getAll()) {
            if (order.state === "cancel" || this.stockGuard.baseOrderUuids.has(order.uuid)) {
                continue;
            }
            for (const line of order.lines) {
                if (line.product_id?.id === productId) {
                    used += line.qty;
                }
            }
        }
        return used;
    },

    /** Available quantity of a product.product, or null when it is not tracked (service, consumable). */
    stockGuardAvailable(product) {
        const base = product && this.stockGuard.qty[product.id];
        if (base === undefined || base === null) {
            return null;
        }
        return base - this.stockGuardUsedQty(product.id);
    },

    /** Available quantity shown on a product card (sum of the variants of a template). */
    stockGuardCardQty(productTemplate) {
        let total = null;
        for (const variant of productTemplate?.product_variant_ids || []) {
            const available = this.stockGuardAvailable(variant);
            if (available !== null) {
                total = (total || 0) + available;
            }
        }
        if (total === null) {
            // Products loaded after the start (search, new variants): read them outside the render.
            const ids = (productTemplate?.product_variant_ids || []).map((p) => p.id);
            Promise.resolve().then(() => this.stockGuardFetch(ids));
        }
        return total;
    },

    /**
     * Sync check used by the numpad. Returns null when the quantity is fine, otherwise
     * { available, block } where `block` means the change must be refused.
     */
    stockGuardCheck(product, addQty, alreadyCounted = false) {
        const mode = this.config.stock_guard_mode;
        if (mode === "none" || !(addQty > 0)) {
            return null;
        }
        const available = this.stockGuardAvailable(product);
        if (available === null) {
            return null;
        }
        const before = alreadyCounted ? available + addQty : available;
        if (before - addQty > -EPSILON) {
            return null;
        }
        return {
            available: Math.max(before, 0),
            block: mode === "block" && !this.stockGuard.canOverride,
        };
    },

    stockGuardMessage(product, available, requested) {
        const values = {
            product: product.display_name,
            available: this.formatProductQty(available, false),
            requested: this.formatProductQty(requested, false),
            location: this.stockGuard.locationName,
        };
        return values.location
            ? _t("%(product)s: %(available)s left in %(location)s, %(requested)s requested.", values)
            : _t("%(product)s: %(available)s left, %(requested)s requested.", values);
    },

    /** Ask or refuse when adding `addQty` of `product` goes above the available quantity. */
    async stockGuardAllows(product, addQty, alreadyCounted = false) {
        await this.stockGuardFetch([product.id]);
        const issue = this.stockGuardCheck(product, addQty, alreadyCounted);
        if (!issue) {
            return true;
        }
        const body = this.stockGuardMessage(product, issue.available, addQty);
        if (issue.block) {
            this.dialog.add(AlertDialog, {
                title: _t("Out of stock"),
                body,
            });
            return false;
        }
        return await ask(this.dialog, {
            title: _t("Not enough stock"),
            body: body + " " + _t("Add it anyway?"),
            confirmLabel: _t("Add anyway"),
            cancelLabel: _t("Cancel"),
        });
    },

    async addLineToOrder(vals, order, opts = {}, configure = true) {
        if (!this.stockGuardEnabled || this.config.stock_guard_mode === "none") {
            return await super.addLineToOrder(...arguments);
        }
        let template = vals.product_tmpl_id;
        if (typeof template === "number") {
            template = this.models["product.template"].get(template);
        }
        let variant = vals.product_id;
        if (typeof variant === "number") {
            variant = this.models["product.product"].get(variant);
        }
        if (!variant && template?.product_variant_ids?.length === 1) {
            variant = template.product_variant_ids[0];
        }
        const isReturn = order.preset_id?.is_return;
        const qty = vals.qty ?? (isReturn ? -1 : 1);
        // Weighed products get their quantity from the scale: check them once the line exists.
        const preCheck = Boolean(variant) && !template?.to_weight;
        if (preCheck && !(await this.stockGuardAllows(variant, qty))) {
            return;
        }
        const before = new Map(order.lines.map((line) => [line.uuid, line.qty]));
        const line = await super.addLineToOrder(...arguments);
        if (!line || preCheck) {
            return line;
        }
        const added = line.qty - (before.get(line.uuid) ?? 0);
        if (added > 0 && !(await this.stockGuardAllows(line.product_id, added, true))) {
            if (before.has(line.uuid)) {
                line.setQuantity(before.get(line.uuid), true);
            } else {
                order.removeOrderline(line);
            }
            return;
        }
        return line;
    },
});
