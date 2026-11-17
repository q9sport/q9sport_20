/** @odoo-module **/
import { Component, proxy, useProps, t } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

export class DeliveryDriverPopup extends Component {

    static template = "pos_delivery_driver.DeliveryDriverPopup";

    static components = {
        Dialog,
    };

    // Component properties
    props = useProps({
        orderName: t.string().optional("New Order"),
        isCashOnDelivery: t.boolean().optional(true),
        alreadyAdded: t.boolean().optional(false),
        close: t.function(),
        getPayload: t.function(),
    });

    setup() {

        this.state = proxy({
            // Cash on Delivery is enabled by default
            isCashOnDelivery: this.props.isCashOnDelivery !== false,
        });

    }

    // Cash on Delivery switch
    onCashOnDeliveryChange(ev) {

        this.state.isCashOnDelivery = Boolean(
            ev.target.checked
        );

    }

    // Confirm delivery
    confirm() {

        this.props.getPayload({
            confirmed: true,
            isCashOnDelivery: Boolean(
                this.state.isCashOnDelivery
            ),
        });

        this.props.close();

    }

    // Cancel popup
    cancel() {

        this.props.getPayload(false);

        this.props.close();

    }

}
