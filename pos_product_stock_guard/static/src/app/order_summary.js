import { OrderSummary } from "@point_of_sale/app/screens/product_screen/order_summary/order_summary";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { parseFloat } from "@web/views/fields/parsers";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

patch(OrderSummary.prototype, {
    _setValue(val) {
        const line = this.currentOrder?.getSelectedOrderline();
        if (this.pos.numpadMode === "quantity" && val !== "remove" && line && !line.combo_parent_id &&
            !line.refunded_orderline_id && !this.currentOrder.preset_id?.is_return) {
            let newQty;
            try {
                newQty = parseFloat(String(val === "" ? 0 : val));
            } catch {
                newQty = NaN;
            }
            const issue = isNaN(newQty) ? null : this.pos.stockGuardCheck(line.product_id, newQty - line.qty);
            if (issue) {
                const body = this.pos.stockGuardMessage(line.product_id, issue.available, newQty - line.qty);
                if (issue.block) {
                    this.dialog.add(AlertDialog, { title: _t("Out of stock"), body });
                    this.numberBuffer.reset();
                    return;
                }
                this.pos.notification.add(body, { type: "warning", title: _t("Not enough stock") });
            }
        }
        return super._setValue(...arguments);
    },
});
