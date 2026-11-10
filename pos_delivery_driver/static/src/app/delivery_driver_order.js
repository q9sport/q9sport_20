/** @odoo-module **/
import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

patch(PosOrder.prototype, {

    setup(vals = {}) {

        super.setup(...arguments);

        this.delivery_employee_id =
            vals.delivery_employee_id || false;

        this.delivery_status =
            vals.delivery_status || "pending";

        this.is_cash_on_delivery =
            Boolean(vals.is_cash_on_delivery);
    },


    export_as_JSON() {

        const json = super.export_as_JSON(...arguments);

        json.delivery_employee_id =
            this.delivery_employee_id || false;

        json.delivery_status =
            this.delivery_status || "pending";

        json.is_cash_on_delivery =
            Boolean(this.is_cash_on_delivery);

        return json;
    },


    init_from_JSON(json) {

        super.init_from_JSON(...arguments);

        this.delivery_employee_id =
            json.delivery_employee_id || false;

        this.delivery_status =
            json.delivery_status || "pending";

        this.is_cash_on_delivery =
            Boolean(json.is_cash_on_delivery);
    },

});
