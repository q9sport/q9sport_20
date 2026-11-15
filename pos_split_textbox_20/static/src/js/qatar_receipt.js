/** @odoo-module **/

import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

function partnerOf(order) {
    return order?.get_partner?.() || order?.getPartner?.() || order?.partner_id || null;
}

function text(value) {
    return String(value || "").trim();
}

patch(PosOrder.prototype, {
    export_for_printing(...args) {
        const receipt = super.export_for_printing(...args);
        const partner = partnerOf(this);
        if (!partner) {
            receipt.qatar_customer = false;
            return receipt;
        }
        const zone = text(partner.zone_no);
        const streetNo = text(partner.street_no);
        const building = text(partner.building_no);
        const standardAddress = text(partner.street);
        const generatedAddress = zone && streetNo && building
            ? `Zone ${zone}, Street ${streetNo}, Building ${building}`
            : "";
        // The server stores a generated street when the cashier leaves the
        // free-text Address empty; do not print that same information twice.
        const freeTextAddress = standardAddress && standardAddress.toLowerCase() !== generatedAddress.toLowerCase()
            ? standardAddress : "";
        const addressParts = [
            freeTextAddress,
            zone ? `Zone No: ${zone}` : "",
            streetNo ? `Street No: ${streetNo}` : "",
            building ? `Building No: ${building}` : "",
        ].filter(Boolean);
        receipt.qatar_customer = {
            name: text(partner.name),
            phone: text(partner.mobile || partner.phone),
            address: addressParts.join(" · "),
            zone_no: zone,
            street_no: streetNo,
            building_no: building,
        };
        return receipt;
    },
});
