/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";

patch(PaymentScreen.prototype, {

    /**
     * Check whether the payment method is Cash.
     * Supports English and Arabic payment method names.
     */
    isSplitCashMethod(paymentMethod) {
        if (!paymentMethod) {
            return false;
        }

        if (paymentMethod.type === "cash") {
            return true;
        }

        const name = String(paymentMethod.name || "").toLowerCase();

        return (
            name.includes("cash") ||
            name.includes("كاش") ||
            name.includes("نقد")
        );
    },

    /**
     * One textbox represents one payment method.
     * If old data contains more than one line for the same method,
     * show their combined value.
     */
    getSplitAmount(paymentMethod) {
        const amount = this.paymentLines
            .filter(
                (line) =>
                    line.payment_method_id.id === paymentMethod.id
            )
            .reduce(
                (sum, line) =>
                    sum + (Number(line.getAmount()) || 0),
                0
            );

        return amount ? String(amount) : "";
    },

    /**
     * Only completed native payment lines count as Paid in Odoo 20.
     */
    getSplitPaidTotal() {
        return this.currentOrder.amountPaid || 0;
    },

    /**
     * Calculate the Change for Cash only.
     *
     * Example:
     * Order total = 100
     * Mada = 40
     * Cash = 70
     * Remaining before Cash = 60
     * Change = 10
     */
    getSplitChange(paymentMethod) {
        if (!this.isSplitCashMethod(paymentMethod)) {
            return "0.00";
        }

        const order = this.currentOrder;
        const currency = order.currency;

        // Total entered through this Cash payment method.
        const cashAmount = this.paymentLines
            .filter(
                (line) =>
                    line.payment_method_id.id === paymentMethod.id
            )
            .reduce(
                (sum, line) =>
                    sum + (Number(line.getAmount()) || 0),
                0
            );

        // Amount allocated to all other payment methods.
        const allocatedByOthers = this.paymentLines
            .filter(
                (line) =>
                    line.payment_method_id.id !== paymentMethod.id
            )
            .reduce(
                (sum, line) =>
                    sum + (Number(line.getAmount()) || 0),
                0
            );

        // Amount still required before receiving Cash.
        const remainingBeforeCash = Math.max(
            0,
            currency.round(order.totalDue - allocatedByOthers)
        );

        // Change = Cash received - remaining amount.
        const change = Math.max(
            0,
            currency.round(cashAmount - remainingBeforeCash)
        );

        return change.toFixed(2);
    },

    /**
     * Update a native POS payment line from the textbox.
     *
     * Cash:
     * - Allows an amount greater than the remaining balance.
     * - Calculates the Change using getSplitChange().
     *
     * Other payment methods:
     * - Cannot exceed the remaining unallocated balance.
     *
     * The available amount is calculated against ALL other payment lines,
     * including pending terminal lines.
     */
    async setSplitAmount(paymentMethod, event) {
        const input = event.currentTarget;
        const rawValue = String(input.value ?? "").trim();

        const lines = this.paymentLines.filter(
            (line) =>
                line.payment_method_id.id === paymentMethod.id
        );

        let line = lines[0];

        const isCash = this.isSplitCashMethod(paymentMethod);

        // Empty textbox: remove all payment lines for this method.
        if (!rawValue) {
            for (const oldLine of lines) {
                this.deletePaymentLine(oldLine.uuid);
            }
            return;
        }

        let amount = Number(rawValue);

        // Validate the entered amount.
        if (!Number.isFinite(amount) || amount < 0) {
            input.value = this.getSplitAmount(paymentMethod);

            this.notification.add(
                "Enter a valid non-negative amount.",
                { type: "warning" }
            );

            return;
        }

        const order = this.currentOrder;
        const currency = order.currency;

        amount = currency.round(amount);

        // Sum every other payment method, including pending electronic payments.
        const allocatedByOthers = this.paymentLines
            .filter(
                (paymentLine) =>
                    paymentLine.payment_method_id.id !== paymentMethod.id
            )
            .reduce(
                (sum, paymentLine) =>
                    sum + (Number(paymentLine.getAmount()) || 0),
                0
            );

        // Maximum amount available for this payment method.
        const maximum = Math.max(
            0,
            currency.round(order.totalDue - allocatedByOthers)
        );

        /*
         * IMPORTANT:
         * Only non-cash payment methods are restricted to the remaining balance.
         * Cash is allowed to exceed the remaining balance.
         */
        if (!isCash && amount > maximum) {
            amount = maximum;

            input.value = String(amount || "");

            this.notification.add(
                "The amount cannot exceed the remaining unallocated balance.",
                { type: "warning" }
            );
        }

        // Remove duplicate legacy lines for this same payment method.
        for (const duplicate of lines.slice(1)) {
            this.deletePaymentLine(duplicate.uuid);
        }

        /*
         * For non-cash methods:
         * A zero available balance must not create a payment line.
         *
         * For Cash:
         * Allow entry even if maximum is zero, because the entered amount
         * may represent cash received and returned as Change.
         */
        if (amount <= 0 || (!isCash && maximum <= 0)) {
            if (line) {
                this.deletePaymentLine(line.uuid);
            }

            input.value = "";
            return;
        }

        // Create a native POS payment line if one does not exist.
        if (!line) {
            await this.addNewPaymentLine(paymentMethod);

            line = this.paymentLines.find(
                (paymentLine) =>
                    paymentLine.payment_method_id.id === paymentMethod.id
            );
        }

        // Verify that Odoo successfully created the payment line.
        if (!line) {
            input.value = "";

            this.notification.add(
                "Odoo could not create a payment line for this method. Please try again.",
                { type: "warning" }
            );

            return;
        }

        // Update the native payment line and trigger POS reactivity.
        line.setAmount(amount);

        input.value = String(amount);
    },

});
