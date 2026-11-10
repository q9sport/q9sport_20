/** @odoo-module **/

import { Component, proxy } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

export class DeliveryDriverPopup extends Component {
    static template = "pos_delivery_driver.DeliveryDriverPopup";
    static components = { Dialog };

    setup() {
        // Owl components receive dialog parameters through this.props.
        // Do not read a separate `props` argument: Owl calls setup without one.
        this.state = proxy({
            search: "",
            selectedDriverId: this.props.selectedDriverId || false,
            isCashOnDelivery: Boolean(this.props.isCashOnDelivery),
        });
    }

    get filteredEmployees() {
        const employees = Array.isArray(this.props.employees)
            ? this.props.employees
            : [];
        const query = (this.state.search || "").trim().toLowerCase();
        if (!query) {
            return employees;
        }
        return employees.filter((employee) =>
            String(employee.name || "").toLowerCase().includes(query)
        );
    }

    selectDriver(driverId) {
        this.state.selectedDriverId = driverId;
    }

    confirm() {
        if (!this.state.selectedDriverId) {
            return;
        }
        const employees = Array.isArray(this.props.employees) ? this.props.employees : [];
        const employee = employees.find(
            (item) => item.id === this.state.selectedDriverId
        );
        const payload = {
            driverId: this.state.selectedDriverId,
            driverName: employee ? employee.name : false,
            isCashOnDelivery: Boolean(this.state.isCashOnDelivery),
        };
        this.props.getPayload(payload);
        this.props.close();
    }

    cancel() {
        this.props.close();
    }
}
