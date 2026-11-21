# -*- coding: utf-8 -*-
from odoo import _, models


def _receipt_text(value):
    return str(value or "").strip()


class PosOrderReceipt(models.AbstractModel):
    _inherit = "pos.order.receipt"

    def _get_common_record_data(self):
        """Add Qatar customer details and cash-tender display data.

        Cash Received is informational only. It never creates a payment line
        and never changes the order's paid amount or accounting entries.
        """
        result = super()._get_common_record_data()
        order_data = result.get("order") or {}

        # Qatar customer details are supplied to the same receipt data object
        # used by Odoo 20's Python/JavaScript receipt renderer.
        partner_data = result.get("partner") or False
        if partner_data:
            zone = _receipt_text(partner_data.get("zone_no"))
            street_no = _receipt_text(partner_data.get("street_no"))
            building = _receipt_text(partner_data.get("building_no"))
            standard_address = _receipt_text(partner_data.get("street"))

            generated_address = (
                "Zone %s, Street %s, Building %s" % (zone, street_no, building)
                if zone and street_no and building
                else ""
            )
            free_text_address = (
                standard_address
                if standard_address
                and standard_address.lower() != generated_address.lower()
                else ""
            )
            address_parts = [
                free_text_address,
                "Zone No: %s" % zone if zone else "",
                "Street No: %s" % street_no if street_no else "",
                "Building No: %s" % building if building else "",
            ]
            result["qatar_customer"] = {
                "name": _receipt_text(partner_data.get("name")),
                "phone": _receipt_text(
                    partner_data.get("mobile") or partner_data.get("phone")
                ),
                "address": " · ".join(part for part in address_parts if part),
                "zone_no": zone,
                "street_no": street_no,
                "building_no": building,
            }
        else:
            result["qatar_customer"] = False

        # Only cash payment lines are used to calculate the displayed
        # difference. Other payment methods and accounting values are untouched.
        cash_payments = self.payment_ids.filtered(
            lambda payment: payment.payment_method_id.type == "cash"
        )
        cash_payment_amount = self.currency_id.round(sum(
            (getattr(payment, "amount_currency", 0.0) or payment.amount)
            for payment in cash_payments
        ))
        cash_received = (
            self.currency_id.round(self.cash_received) if cash_payments else 0.0
        )
        cash_difference = self.currency_id.round(
            cash_received - cash_payment_amount
        )

        order_data.update({
            "cash_received": cash_received,
            "cash_payment_amount": cash_payment_amount,
            "cash_received_formatted": (
                self._order_receipt_format_currency(cash_received)
                if cash_received
                else False
            ),
            "cash_change": cash_difference,
            "cash_change_label": (
                _("Change") if cash_difference >= 0 else _("Remaining")
            ),
            "cash_change_formatted": self._order_receipt_format_currency(
                abs(cash_difference)
            ),
        })
        result["order"] = order_data
        return result
