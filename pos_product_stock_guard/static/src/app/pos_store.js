import { PosStore } from "@point_of_sale/app/services/pos_store";
import { _t } from "@web/core/l10n/translation";
import {
    Component,
    markRaw,
    proxy,
    useProps,
    xml,
    t,
} from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { patch } from "@web/core/utils/patch";

const EPSILON = 1e-6;

/**
 * Cashier stock password dialog.
 */
class StockGuardPasswordDialog extends Component {
    static template = xml`
        <Dialog title="this.props.title">
            <div class="p-3">
                <p t-out="this.props.body"/>

                <form t-on-submit.prevent="this.submit">
                    <div class="mb-3">
                        <label class="form-label">
                            Cashier Stock Password
                        </label>

                        <input
                            type="password"
                            class="form-control"
                            t-att-value="this.state.password"
                            t-on-input="this.onInput"
                            placeholder="Enter your stock password"
                            autocomplete="off"
                            t-att-disabled="this.state.loading"
                        />
                    </div>

                    <div
                        t-if="this.state.checking"
                        class="text-muted mb-2"
                    >
                        Checking password...
                    </div>

                    <div
                        t-if="this.state.error"
                        class="alert alert-danger"
                        role="alert"
                    >
                        <t t-out="this.state.error"/>
                    </div>

                    <div class="d-flex justify-content-end gap-2">
                        <button
                            type="button"
                            class="btn btn-secondary"
                            t-att-disabled="this.state.loading"
                            t-on-click="this.cancel"
                        >
                            Cancel
                        </button>

                        <button
                            type="submit"
                            class="btn btn-primary"
                            t-att-disabled="
                                this.state.loading ||
                                this.state.checking ||
                                !this.state.validPassword
                            "
                        >
                            <t t-if="this.state.loading">
                                Checking...
                            </t>
                            <t t-else="">Confirm</t>
                        </button>
                    </div>
                </form>
            </div>
        </Dialog>
    `;

    static components = { Dialog };

    props = useProps({
        close: t.function(),
        title: t.string(),
        body: t.string(),
        onConfirm: t.function(),
        resolve: t.function(),
    });

    setup() {
        this.state = proxy({
            password: "",
            loading: false,
            checking: false,
            validPassword: false,
            error: "",
        });

        this.validationRequest = 0;
    }

    async onInput(ev) {
        const password = ev.target.value;
        const requestId = ++this.validationRequest;

        this.state.password = password;
        this.state.validPassword = false;
        this.state.error = "";
        this.state.checking = Boolean(password);

        if (!password) {
            return;
        }

        try {
            const valid = await this.props.onConfirm(password);

            if (requestId !== this.validationRequest) {
                return;
            }

            this.state.validPassword = valid === true;

            if (!valid) {
                this.state.error = _t("Incorrect stock password.");
            }
        } catch (error) {
            if (requestId !== this.validationRequest) {
                return;
            }

            console.error("Stock Guard input verification error:", error);

            this.state.validPassword = false;
            this.state.error = _t("Could not verify the password.");
        } finally {
            if (requestId === this.validationRequest) {
                this.state.checking = false;
            }
        }
    }

    async submit(ev) {
        ev.preventDefault();

        if (
            this.state.loading ||
            this.state.checking ||
            !this.state.validPassword ||
            !this.state.password
        ) {
            return;
        }

        this.state.loading = true;
        this.state.error = "";

        try {
            const valid = await this.props.onConfirm(
                this.state.password
            );

            if (valid === true) {
                this.props.resolve(true);
                this.props.close();
                return;
            }

            this.state.validPassword = false;
            this.state.password = "";
            this.state.error = _t("Incorrect stock password.");
        } catch (error) {
            console.error("Stock Guard submit verification error:", error);

            this.state.validPassword = false;
            this.state.password = "";
            this.state.error = _t("Could not verify the password.");
        }

        this.state.loading = false;
    }

    cancel() {
        this.props.resolve(false);
        this.props.close();
    }
}


patch(PosStore.prototype, {
    async setup() {
        this.stockGuard = {
            qty: {},
            canOverride: false,
            locationName: "",
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

        await this.stockGuardFetch(
            this.models["product.product"].getAll().map(
                (product) => product.id
            )
        );

        return result;
    },

    get stockGuardEnabled() {
        return Boolean(
            this.config.stock_guard_show ||
            this.config.stock_guard_mode !== "none"
        );
    },

    async stockGuardFetch(productIds) {
        const ids = productIds.filter(
            (id) =>
                typeof id === "number" &&
                !this.stockGuard.requested.has(id)
        );

        if (!ids.length || !this.stockGuardEnabled) {
            return;
        }

        ids.forEach((id) => this.stockGuard.requested.add(id));

        try {
            const data = await this.data.call(
                "pos.config",
                "get_stock_guard_data",
                [[this.config.id], ids]
            );

            Object.assign(this.stockGuard.qty, data.quantities);
            this.stockGuard.canOverride = data.can_override;
            this.stockGuard.locationName = data.location_name || "";
        } catch (error) {
            console.error("Stock Guard fetch error:", error);

            ids.forEach((id) =>
                this.stockGuard.requested.delete(id)
            );
        }
    },

    stockGuardUsedQty(productId) {
        let used = 0;

        for (const order of this.models["pos.order"].getAll()) {
            if (
                order.state === "cancel" ||
                this.stockGuard.baseOrderUuids.has(order.uuid)
            ) {
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

    stockGuardAvailable(product) {
        const base = product && this.stockGuard.qty[product.id];

        if (base === undefined || base === null) {
            return null;
        }

        return base - this.stockGuardUsedQty(product.id);
    },

    stockGuardCardQty(productTemplate) {
        let total = null;

        for (
            const variant of
            productTemplate?.product_variant_ids || []
        ) {
            const available = this.stockGuardAvailable(variant);

            if (available !== null) {
                total = (total || 0) + available;
            }
        }

        if (total === null) {
            const ids = (
                productTemplate?.product_variant_ids || []
            ).map((product) => product.id);

            Promise.resolve().then(() =>
                this.stockGuardFetch(ids)
            );
        }

        return total;
    },

    orderProductBySequenceAndFav(products) {
        const sorted = products.map((product) => ({
            product,
            qty: this.stockGuardCardQty(product),
        }));

        const getPriority = (qty) => {
            if (qty === null || qty === undefined) {
                return 1;
            }

            return qty > EPSILON ? 0 : 2;
        };

        sorted.sort((a, b) => {
            const priorityA = getPriority(a.qty);
            const priorityB = getPriority(b.qty);

            if (priorityA !== priorityB) {
                return priorityA - priorityB;
            }

            return (a.product.name || "").localeCompare(
                b.product.name || "",
                undefined,
                {
                    numeric: true,
                    sensitivity: "base",
                }
            );
        });

        return sorted.map((item) => item.product);
    },

    stockGuardCheck(product, addQty, alreadyCounted = false) {
        const mode = this.config.stock_guard_mode;

        if (mode === "none" || !(addQty > 0)) {
            return null;
        }

        const available = this.stockGuardAvailable(product);

        if (available === null) {
            return null;
        }

        const before = alreadyCounted
            ? available + addQty
            : available;

        if (before - addQty > -EPSILON) {
            return null;
        }

        return {
            available: Math.max(before, 0),
            block:
                mode === "block" &&
                !this.stockGuard.canOverride,
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
            ? _t(
                "%(product)s: %(available)s left in %(location)s, %(requested)s requested.",
                values
            )
            : _t(
                "%(product)s: %(available)s left, %(requested)s requested.",
                values
            );
    },

    /**
     * Get current cashier as an hr.employee record.
     */
    stockGuardGetCurrentCashier() {
        const cashier =
            this.get_cashier?.() ||
            this.cashier ||
            this.accessRight?.cashier ||
            this.user ||
            null;

        if (!cashier) {
            console.error("Stock Guard: No current cashier found");
            return null;
        }

        // Cashier has employee_id relation.
        if (cashier.employee_id?.id) {
            return (
                this.models["hr.employee"]?.get(
                    cashier.employee_id.id
                ) || cashier.employee_id
            );
        }

        // Cashier has employee relation.
        if (cashier.employee?.id) {
            return (
                this.models["hr.employee"]?.get(
                    cashier.employee.id
                ) || cashier.employee
            );
        }

        // Cashier is already an hr.employee record.
        if (cashier.user_id !== undefined) {
            return cashier;
        }

        // Cashier is a res.users record.
        const employees =
            this.models["hr.employee"]?.getAll() || [];

        return (
            employees.find(
                (employee) =>
                    employee.user_id?.id === cashier.id
            ) || null
        );
    },

    /**
     * Verify the current cashier's stock password.
     */
    async stockGuardVerifyPassword(password) {
        const cashier = this.stockGuardGetCurrentCashier();
        const employeeId = cashier?.id;

        console.log("Stock Guard password verification:", {
            cashier,
            employeeId,
            passwordEntered: Boolean(password),
        });

        if (!employeeId || !password) {
            console.error(
                "Stock Guard: Missing employee ID or password"
            );
            return false;
        }

        try {
            const result = await this.data.call(
                "pos.config",
                "verify_stock_guard_override_password",
                [[this.config.id], employeeId, password]
            );

            console.log(
                "Stock Guard verification result:",
                result
            );

            return result === true;
        } catch (error) {
            console.error(
                "Stock Guard: Password verification failed",
                error
            );
            return false;
        }
    },

    async stockGuardAllows(
        product,
        addQty,
        alreadyCounted = false
    ) {
        await this.stockGuardFetch([product.id]);

        const issue = this.stockGuardCheck(
            product,
            addQty,
            alreadyCounted
        );

        if (!issue) {
            return true;
        }

        const body = this.stockGuardMessage(
            product,
            issue.available,
            addQty
        );

        return await new Promise((resolve) => {
            this.dialog.add(StockGuardPasswordDialog, {
                title: _t("Cashier Password Required"),
                body:
                    body +
                    " " +
                    _t(
                        "Enter the current cashier's stock password to continue."
                    ),

                onConfirm: async (password) => {
                    return await this.stockGuardVerifyPassword(
                        password
                    );
                },

                resolve,
            });
        });
    },

    async addLineToOrder(
        vals,
        order,
        opts = {},
        configure = true
    ) {
        if (
            !this.stockGuardEnabled ||
            this.config.stock_guard_mode === "none"
        ) {
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

        if (
            !variant &&
            template?.product_variant_ids?.length === 1
        ) {
            variant = template.product_variant_ids[0];
        }

        const isReturn = order.preset_id?.is_return;
        const qty = vals.qty ?? (isReturn ? -1 : 1);

        const preCheck =
            Boolean(variant) && !template?.to_weight;

        if (
            preCheck &&
            !(await this.stockGuardAllows(variant, qty))
        ) {
            return;
        }

        const before = new Map(
            order.lines.map((line) => [
                line.uuid,
                line.qty,
            ])
        );

        const line = await super.addLineToOrder(...arguments);

        if (!line || preCheck) {
            return line;
        }

        const added =
            line.qty - (before.get(line.uuid) ?? 0);

        if (
            added > 0 &&
            !(await this.stockGuardAllows(
                line.product_id,
                added,
                true
            ))
        ) {
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
