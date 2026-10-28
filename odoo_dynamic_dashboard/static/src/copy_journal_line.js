/** @odoo-module **/
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";

class CopyJournalLine extends Component {
    static template = "odoo_dynamic_dashboard.CopyJournalLine";

    static props = {
        ...Component.props,
    };

    setup() {
        console.log("CopyJournalLine component props:", this.props);
    }

    async onCopy() {
        console.log("CopyJournalLine click props:", this.props);

        const record = this.props?.record;

        if (!record) {
            console.error(
                "CopyJournalLine: record is missing. Check the props logged above."
            );
            return;
        }

        console.log("Journal line record:", record);
    }
}

registry.category("fields").add("copy_journal_line", {
    component: CopyJournalLine,
    supportedTypes: ["boolean"],
});
