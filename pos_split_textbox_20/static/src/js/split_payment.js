/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";

patch(PaymentScreen.prototype, {
    /**
     * One textbox represents one payment method. If old data contains more than
     * one line for the same method, show their combined value.
     */
    getSplitAmount(paymentMethod) {
        const amount = this.paymentLines
            .filter((line) => line.payment_method_id.id === paymentMethod.id)
            .reduce((sum, line) => sum + (Number(line.getAmount()) || 0), 0);
        return amount ? String(amount) : "";
    },


    /**
     * Cash Received is informational only. The native POS payment line
     * continues to contain the actual allocated Amount.
     */
    getCashReceived(paymentMethod) {
        if (paymentMethod && paymentMethod.type !== "cash") {
            return 0;
        }
        return Number(
            this.currentOrder.cash_received ??
            this.currentOrder.raw?.cash_received ??
            0
        ) || 0;
    },

    setCashReceived(paymentMethod, event) {
        const input = event.currentTarget;
        const rawValue = String(input.value ?? "").trim();

        if (paymentMethod.type !== "cash") {
            input.value = "";
            return;
        }

        if (!rawValue) {
            this.currentOrder.cash_received = 0;
            if (this.currentOrder.raw) {
                this.currentOrder.raw.cash_received = 0;
            }
            input.value = "";
            return;
        }

        let amount = Number(rawValue);
        if (!Number.isFinite(amount) || amount < 0) {
            input.value = String(this.getCashReceived(paymentMethod) || "");
            this.notification.add(
                "Enter a valid non-negative cash received amount.",
                { type: "warning" }
            );
            return;
        }

        amount = this.currentOrder.currency.round(amount);
        this.currentOrder.cash_received = amount;
        if (this.currentOrder.raw) {
            this.currentOrder.raw.cash_received = amount;
        }
        input.value = String(amount);
    },

    getCashChange(paymentMethod) {
        if (!paymentMethod || paymentMethod.type !== "cash") {
            return 0;
        }
        const cashAmount = this.paymentLines
            .filter((line) => line.payment_method_id.type === "cash")
            .reduce((sum, line) => sum + (Number(line.getAmount()) || 0), 0);
        return this.currentOrder.currency.round(
            this.getCashReceived(paymentMethod) - cashAmount
        );
    },

    getCashChangeLabel(paymentMethod) {
        return this.getCashChange(paymentMethod) >= 0 ? "Change" : "Remaining";
    },

    getCashChangeFormatted(paymentMethod) {
        return this.pos.formatCurrency(Math.abs(this.getCashChange(paymentMethod)));
    },

    /**
     * Clear the informational tender value when the cashier removes the last
     * cash payment line using Odoo's native delete control.
     */
    deletePaymentLine(uuid) {
        const line = this.paymentLines.find((paymentLine) => paymentLine.uuid === uuid);
        const result = super.deletePaymentLine(...arguments);
        if (
            line?.payment_method_id?.type === "cash" &&
            !this.paymentLines.some(
                (paymentLine) => paymentLine.payment_method_id?.type === "cash"
            )
        ) {
            this.currentOrder.cash_received = 0;
            if (this.currentOrder.raw) {
                this.currentOrder.raw.cash_received = 0;
            }
        }
        return result;
    },

    /** Only completed native payment lines count as Paid in Odoo 20. */
    getSplitPaidTotal() {
        return this.currentOrder.amountPaid || 0;
    },

    /**
     * Update a native POS payment line from the textbox.
     * The available amount is calculated against ALL other payment lines,
     * including pending terminal lines, so the combined allocation cannot
     * exceed the order total.
     */
    async setSplitAmount(paymentMethod, event) {
        const input = event.currentTarget;
        const rawValue = String(input.value ?? "").trim();
        const lines = this.paymentLines.filter(
            (line) => line.payment_method_id.id === paymentMethod.id
        );
        let line = lines[0];

        if (!rawValue) {
            for (const oldLine of lines) {
                this.deletePaymentLine(oldLine.uuid);
            }
            if (paymentMethod.type === "cash") {
                this.currentOrder.cash_received = 0;
                if (this.currentOrder.raw) {
                    this.currentOrder.raw.cash_received = 0;
                }
            }
            return;
        }

        let amount = Number(rawValue);
        if (!Number.isFinite(amount) || amount < 0) {
            input.value = this.getSplitAmount(paymentMethod);
            this.notification.add("Enter a valid non-negative amount.", { type: "warning" });
            return;
        }

        const order = this.currentOrder;
        const currency = order.currency;
        amount = currency.round(amount);

        // Sum every other payment method, including pending electronic payments.
        const allocatedByOthers = this.paymentLines
            .filter((paymentLine) => paymentLine.payment_method_id.id !== paymentMethod.id)
            .reduce((sum, paymentLine) => sum + (Number(paymentLine.getAmount()) || 0), 0);
        const maximum = Math.max(0, currency.round(order.totalDue - allocatedByOthers));

        if (amount > maximum) {
            amount = maximum;
            input.value = String(amount || "");
            this.notification.add(
                "The amount cannot exceed the remaining unallocated balance.",
                { type: "warning" }
            );
        }

        // Remove duplicate legacy lines for this same manual method.
        for (const duplicate of lines.slice(1)) {
            this.deletePaymentLine(duplicate.uuid);
        }

        // A zero/rounded-zero amount must NEVER create a payment line.
        if (amount <= 0 || maximum <= 0) {
            if (line) {
                this.deletePaymentLine(line.uuid);
            }
            input.value = "";
            return;
        }

        if (!line) {
            // addNewPaymentLine creates the native POS line but does not
            // consistently return that line across POS builds. Never use its
            // return value as a success flag.
            await this.addNewPaymentLine(paymentMethod);
            line = this.paymentLines.find(
                (paymentLine) => paymentLine.payment_method_id.id === paymentMethod.id
            );
        }

        if (!line) {
            input.value = "";
            this.notification.add(
                "Odoo could not create a payment line for this method. Please try again.",
                { type: "warning" }
            );
            return;
        }

        // setAmount updates the native payment line and triggers POS reactivity.
        line.setAmount(amount);
        input.value = String(amount);
    },
});
