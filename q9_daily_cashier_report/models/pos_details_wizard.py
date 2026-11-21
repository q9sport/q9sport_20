from collections import defaultdict
from datetime import datetime, time, timedelta

from odoo import fields, models


class PosDetailsWizard(models.TransientModel):
    _inherit = "pos.details.wizard"

    def _q9_get_sales_details_extra(self):
        """Build extra sections for the native Sales Details PDF.

        The native wizard's date/config field names changed between Odoo
        versions; detect the fields available in this database rather than
        depending on a custom wizard or custom access CSV.
        """
        self.ensure_one()
        model_fields = self._fields

        def first_value(names):
            for name in names:
                if name in model_fields:
                    value = self[name]
                    if value:
                        return value
            return False

        date_from = first_value(("start_date", "date_start", "date_from"))
        date_to = first_value(("end_date", "date_end", "date_to"))
        config_records = first_value(("pos_config_ids", "config_ids", "pos_config_id"))
        session_records = first_value(("session_ids", "pos_session_ids"))

        # Standard POS Sales Details uses a date range and optionally sessions/configs.
        domain = [("state", "in", ("paid", "done", "invoiced"))]
        if date_from:
            start_dt = datetime.combine(fields.Date.to_date(date_from), time.min)
            domain.append(("date_order", ">=", fields.Datetime.to_string(start_dt)))
        if date_to:
            end_dt = datetime.combine(fields.Date.to_date(date_to) + timedelta(days=1), time.min)
            domain.append(("date_order", "<", fields.Datetime.to_string(end_dt)))
        if config_records:
            if config_records._name == "pos.config":
                domain.append(("config_id", "in", config_records.ids))
        if session_records:
            if session_records._name == "pos.session":
                domain.append(("session_id", "in", session_records.ids))

        orders = self.env["pos.order"].search(domain)
        currency = orders[:1].currency_id or self.env.company.currency_id
        payments = orders.payment_ids

        def payment_bucket(payment):
            name = (payment.payment_method_id.name or "").lower()
            if "cash" in name:
                return "Cash"
            if any(word in name for word in ("visa", "card", "master", "mada")):
                return "Card / Visa"
            if any(word in name for word in ("credit", "account", "online", "snoonu", "q_mall", "q mall")):
                return "Credit"
            return "Other"

        payment_totals = defaultdict(lambda: {"count": 0, "amount": 0.0})
        for payment in payments:
            bucket = payment_bucket(payment)
            payment_totals[bucket]["count"] += 1
            payment_totals[bucket]["amount"] += payment.amount

        products = {}
        for line in orders.lines:
            product = line.product_id
            row = products.setdefault(product.id, {
                "code": product.default_code or "",
                "name": product.display_name,
                "qty": 0.0,
                "amount": 0.0,
                "returned_qty": 0.0,
                "refund": 0.0,
            })
            amount = getattr(line, "price_subtotal_incl", False)
            if amount is False:
                amount = line.price_subtotal
            row["qty"] += line.qty
            row["amount"] += amount
            if line.qty < 0 or amount < 0:
                row["returned_qty"] += abs(line.qty)
                row["refund"] += abs(amount)

        product_rows = sorted(products.values(), key=lambda row: row["amount"], reverse=True)
        return_rows = [row for row in product_rows if row["returned_qty"] or row["refund"]]
        cashier_rows = []
        for cashier in orders.mapped("user_id"):
            cashier_orders = orders.filtered(lambda order: order.user_id == cashier)
            total = sum(cashier_orders.mapped("amount_total"))
            cashier_rows.append({
                "name": cashier.display_name,
                "count": len(cashier_orders),
                "total": total,
                "average": total / len(cashier_orders) if cashier_orders else 0.0,
            })
        cashier_rows.sort(key=lambda row: row["total"], reverse=True)

        credit_partners = {}
        for order in orders:
            credit_payments = order.payment_ids.filtered(lambda payment: payment_bucket(payment) == "Credit")
            if not credit_payments:
                continue
            partner = order.partner_id
            row = credit_partners.setdefault(partner.id or 0, {
                "name": partner.display_name if partner else "Walk-in Customer",
                "code": partner.ref or "" if partner else "",
                "count": 0,
                "amount": 0.0,
            })
            row["count"] += 1
            row["amount"] += sum(credit_payments.mapped("amount"))

        hourly = defaultdict(lambda: {"count": 0, "qty": 0.0, "total": 0.0})
        for order in orders:
            if not order.date_order:
                continue
            local_dt = fields.Datetime.context_timestamp(self, order.date_order)
            key = local_dt.strftime("%H:00")
            hourly[key]["count"] += 1
            hourly[key]["qty"] += sum(order.lines.mapped("qty"))
            hourly[key]["total"] += order.amount_total

        return {
            "currency": currency,
            "payments": [(name, values["count"], values["amount"]) for name, values in sorted(payment_totals.items())],
            "products": product_rows,
            "returns": return_rows,
            "cashiers": cashier_rows,
            "credit_accounts": sorted(credit_partners.values(), key=lambda row: row["amount"], reverse=True),
            "hourly": [(hour, values) for hour, values in sorted(hourly.items())],
            "orders_count": len(orders),
            "sales_total": sum(orders.mapped("amount_total")),
            "cash_total": payment_totals["Cash"]["amount"],
            "card_total": payment_totals["Card / Visa"]["amount"],
            "credit_total": payment_totals["Credit"]["amount"],
        }
