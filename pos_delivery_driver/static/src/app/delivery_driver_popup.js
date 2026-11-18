/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";


export class DeliveryDriverPopup extends Component {

    static template = "pos_delivery_driver.DeliveryDriverPopup";

    static components = {
        Dialog,
    };

    static props = {
        orderName: {
            type: String,
            optional: true,
        },

        isCashOnDelivery: {
            type: Boolean,
            optional: true,
        },

        alreadyAdded: {
            type: Boolean,
            optional: true,
        },

        employees: {
            type: Array,
            optional: true,
        },

        selectedDriverId: {
            type: [Number, Boolean],
            optional: true,
        },

        close: Function,
        getPayload: Function,
    };


    setup() {
        this.state = useState({

            isCashOnDelivery:
                this.props.isCashOnDelivery !== false,

            selectedDriverId:
                this.props.selectedDriverId || false,

        });
    }


    onDriverChange(ev) {
        this.state.selectedDriverId =
            ev.target.value
                ? Number(ev.target.value)
                : false;
    }


    onCashOnDeliveryChange(ev) {
        this.state.isCashOnDelivery =
            Boolean(ev.target.checked);
    }


    confirm() {

        this.props.getPayload({

            confirmed: true,

            driverId:
                this.state.selectedDriverId || false,

            isCashOnDelivery:
                Boolean(this.state.isCashOnDelivery),

        });

        this.props.close();
    }


    cancel() {

        this.props.getPayload(false);

        this.props.close();
    }

}
