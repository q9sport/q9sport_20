/** @odoo-module **/

import { GeneratePrinterData } from "@point_of_sale/app/utils/printer/generate_printer_data";
import { patch } from "@web/core/utils/patch";

function text(value) {
    return String(value || "").trim();
}

function partnerValue(partner, field) {
    return partner?.[field] ?? partner?.raw?.[field] ?? "";
}

function getQatarCustomer(partner) {
    if (!partner) {
        return false;
    }

    const zone = text(partnerValue(partner, "zone_no"));
    const streetNo = text(partnerValue(partner, "street_no"));
    const building = text(partnerValue(partner, "building_no"));
    const standardAddress = text(partnerValue(partner, "street"));

    const generatedAddress =
        zone && streetNo && building
            ? `Zone ${zone}, Street ${streetNo}, Building ${building}`
            : "";

    const freeTextAddress =
        standardAddress &&
        standardAddress.toLowerCase() !== generatedAddress.toLowerCase()
            ? standardAddress
            : "";

    const addressParts = [
        freeTextAddress,
        zone ? `Zone No: ${zone}` : "",
        streetNo ? `Street No: ${streetNo}` : "",
        building ? `Building No: ${building}` : "",
    ].filter(Boolean);

    return {
        name: text(partnerValue(partner, "name")),
        phone: text(
            partnerValue(partner, "mobile") || partnerValue(partner, "phone")
        ),
        address: addressParts.join(" · "),
        zone_no: zone,
        street_no: streetNo,
        building_no: building,
    };
}

patch(GeneratePrinterData.prototype, {
    /**
     * Odoo 20 uses GeneratePrinterData for the POS receipt preview/printing.
     * Keep this data shape aligned with models/pos_order_receipt.py because
     * the same QWeb receipt template is rendered by JavaScript and Python.
     */
    generateReceiptData(...args) {
        const receipt = super.generateReceiptData(...args);
        const partner = this.order?.partner_id || false;

        receipt.qatar_customer = getQatarCustomer(partner);

        const cashPayments = (this.order?.payment_ids || []).filter(
            (payment) => payment.payment_method_id?.type === "cash"
        );
        const cashPaymentAmountRaw = cashPayments.reduce(
            (total, payment) =>
                total + (Number(payment.amount_currency || payment.amount) || 0),
            0
        );

        const orderCurrency = this.order?.orderCurrency || this.currency;
        const cashPaymentAmount = orderCurrency.round(cashPaymentAmountRaw);
        const cashReceived = cashPayments.length
            ? Number(
                  this.order?.cash_received ??
                      this.order?.raw?.cash_received ??
                      0
              ) || 0
            : 0;
        const cashChange = orderCurrency.round(
            cashReceived - cashPaymentAmount
        );

        receipt.order = {
            ...receipt.order,
            cash_received: cashReceived,
            cash_payment_amount: cashPaymentAmount,
            cash_received_formatted: cashReceived
                ? this.formatCurrency(cashReceived, orderCurrency)
                : false,
            cash_change: cashChange,
            cash_change_label: cashChange >= 0 ? "Change" : "Remaining",
            cash_change_formatted: this.formatCurrency(
                Math.abs(cashChange),
                orderCurrency
            ),
        };

        return receipt;
    },
});
