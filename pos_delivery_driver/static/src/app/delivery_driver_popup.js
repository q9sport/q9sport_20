/** @odoo-module **/
import { Component, proxy, useProps } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";


export class DeliveryDriverPopup extends Component {

    static template = "pos_delivery_driver.DeliveryDriverPopup";

    static components = {
        Dialog,
    };

    // OWL 3: explicitly read component props.
    props = useProps();

    setup() {

        this.state = proxy({
            search: "",
            selectedDriverId: this.props.selectedDriverId || false,
            isCashOnDelivery: Boolean(
                this.props.isCashOnDelivery
            ),
        });

        this.employees = Array.isArray(this.props.employees)
            ? this.props.employees
            : [];
    }

    get filteredEmployees() {

        const query = String(this.state.search || "")
            .trim()
            .toLowerCase();

        if (!query) {
            return this.employees;
        }

        return this.employees.filter((employee) =>
            String(employee.name || "")
                .toLowerCase()
                .includes(query)
        );
    }

    onSearchInput(ev) {
        this.state.search = ev.target.value || "";
    }

    onCashOnDeliveryChange(ev) {
        this.state.isCashOnDelivery = Boolean(
            ev.target.checked
        );
    }

    selectDriver(driverId) {
        this.state.selectedDriverId = driverId || false;
    }

    finish(payload) {

        // Resolve the promise created by makeAwaitable.
        if (typeof this.props.getPayload === "function") {
            this.props.getPayload(payload);
        }

        // Close the popup through the callback supplied by Dialog.
        if (typeof this.props.close === "function") {
            this.props.close();
        }
    }

    confirm() {

        const driverId = this.state.selectedDriverId;

        if (!driverId) {
            return;
        }

        const employee = this.employees.find(
            (item) => Number(item.id) === Number(driverId)
        );

        this.finish({
            driverId: Number(driverId),
            driverName: employee ? employee.name : "",
            isCashOnDelivery: Boolean(
                this.state.isCashOnDelivery
            ),
        });
    }

    cancel() {
        // Resolve with false so the calling method can stop
        // without adding the Delivery product.
        this.finish(false);
    }
}
