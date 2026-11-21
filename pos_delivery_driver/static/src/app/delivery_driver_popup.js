
/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

export class DeliveryDriverPopup extends Component {

    static template = "pos_delivery_driver.DeliveryDriverPopup";

    static components = {
        Dialog,
    };

    setup() {
        this.state = useState({
            isCashOnDelivery: this.props.isCashOnDelivery !== false,
        });
    }

    // Cash on Delivery switch
    onCashOnDeliveryChange(ev) {
        this.state.isCashOnDelivery = Boolean(ev.target.checked);
    }

    // Confirm delivery
    confirm() {
        if (typeof this.props.getPayload === "function") {
            this.props.getPayload({
                confirmed: true,
                isCashOnDelivery: Boolean(
                    this.state.isCashOnDelivery
                ),
            });
        }

        if (typeof this.props.close === "function") {
            this.props.close();
        }
    }

    // Cancel popup
    cancel() {
        if (typeof this.props.getPayload === "function") {
            this.props.getPayload(false);
        }

        if (typeof this.props.close === "function") {
            this.props.close();
        }
    }
}
