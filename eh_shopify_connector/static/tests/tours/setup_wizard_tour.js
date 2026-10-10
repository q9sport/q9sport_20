/** @odoo-module **/
// Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
// The Connect a Store dialog end to end: pick a connection method, connect
// against the offline test store, go back and forward, answer the order
// questions on the review step and finish on the new store.

import { registry } from "@web/core/registry";

function assertValue(selector, expected) {
    const input = document.querySelector(selector);
    if (!input || input.value !== expected) {
        throw new Error(`${selector} holds ${input && input.value}, expected ${expected}`);
    }
}

registry.category("web_tour.tours").add("eh_shopify_setup_wizard_tour", {
    steps: () => [
        { trigger: ".modal .o_form_view .o_field_widget[name=shop_domain] input" },
        { trigger: ".modal .o_field_widget[name=required_scope_list]:contains(read_orders)" },
        // The token method swaps the fields it needs in and out of the dialog.
        { trigger: ".modal .o_field_widget[name=auth_mode] input[data-value=legacy_token]", run: "click" },
        { trigger: ".modal .o_field_widget[name=access_token] input[type=password]" },
        { trigger: ".modal .o_form_view:not(:has(.o_field_widget[name=client_id]))" },
        { trigger: ".modal .o_field_widget[name=auth_mode] input[data-value=client_credentials]", run: "click" },
        { trigger: ".modal .o_form_view:not(:has(.o_field_widget[name=access_token]))" },
        {
            trigger: ".modal .o_field_widget[name=shop_domain] input",
            run: "edit https://admin.shopify.com/store/eh-wizard-ui",
        },
        { trigger: ".modal .o_field_widget[name=client_id] input", run: "edit eh-client-id" },
        { trigger: ".modal .o_field_widget[name=client_secret] input[type=password]", run: "edit eh-client-secret" },
        { trigger: ".modal .modal-footer button[name=action_connect]", run: "click" },
        // Review step: the store answered and its locations were read.
        { trigger: ".modal .o_field_widget[name=shop_name]:contains(EH Test Store)" },
        { trigger: ".modal .o_field_widget[name=result_note]:contains(locations found)" },
        { trigger: ".modal .o_field_widget[name=currency_code]:contains(USD)" },
        // Back to the first step keeps what was typed, and connecting again
        // reuses the stored secret.
        { trigger: ".modal .modal-footer button[name=action_back]", run: "click" },
        {
            trigger: ".modal .modal-footer button[name=action_connect]",
            run() {
                assertValue(".modal .o_field_widget[name=shop_domain] input",
                    "https://admin.shopify.com/store/eh-wizard-ui");
                assertValue(".modal .o_field_widget[name=client_id] input", "eh-client-id");
                this.anchor.click();
            },
        },
        { trigger: ".modal .o_field_widget[name=shop_name]:contains(EH Test Store)" },
        // Order questions: invoice manually, leave orders unconfirmed.
        { trigger: ".modal .o_field_widget[name=invoice_policy] .o_select_menu_toggler", run: "click" },
        { trigger: ".o_select_menu_item:contains(Manually)", run: "click" },
        { trigger: ".modal .o_field_widget[name=invoice_policy] input.o_select_menu_toggler:value(Manually)" },
        { trigger: ".modal .o_field_widget[name=order_confirm] input:checked", run: "click" },
        { trigger: ".modal .o_field_widget[name=order_confirm] input:not(:checked)" },
        { trigger: ".modal .modal-footer button[name=action_finish]", run: "click" },
        // The dialog closes on the connected store's form.
        {
            trigger: "body:not(:has(.modal)) .o_form_view .o_field_widget[name=shop_domain] input",
            run() {
                assertValue(".o_form_view .o_field_widget[name=shop_domain] input",
                    "eh-wizard-ui.myshopify.com");
                assertValue(".o_form_view .o_field_widget[name=name] input", "eh-wizard-ui");
            },
        },
        { trigger: ".o_form_view .o_field_widget[name=has_client_secret] input:checked" },
    ],
});
