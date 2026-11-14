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

        if (amount <= 0) {
            if (line) {
                this.deletePaymentLine(line.uuid);
            }
            input.value = "";
            return;
        }

        if (!line) {
            const created = await this.addNewPaymentLine(paymentMethod);
            if (!created) {
                input.value = "";
                return;
            }
            line = this.paymentLines.find(
                (paymentLine) => paymentLine.payment_method_id.id === paymentMethod.id
            );
        }

        if (line) {
            line.setAmount(amount, line.currency);
            input.value = String(amount);
        }
    },
});
