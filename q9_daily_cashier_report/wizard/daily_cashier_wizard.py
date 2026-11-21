from odoo import fields, models
from odoo.exceptions import UserError
import io
import base64
from datetime import datetime, time, timedelta


class DailyCashierWizard(models.TransientModel):
    _name = "q9.daily.cashier.wizard"
    _description = "Daily Cashier Report"

    date_start = fields.Date(required=True, default=fields.Date.context_today)
    date_end = fields.Date(required=True, default=fields.Date.context_today)
    config_ids = fields.Many2many("pos.config", string="POS / Store")
    cashier_ids = fields.Many2many("res.users", string="Cashiers")
    report_file = fields.Binary(readonly=True)
    report_filename = fields.Char(readonly=True)

    def _get_orders(self):
        self.ensure_one()
        if self.date_end < self.date_start:
            raise UserError("End date must be on or after start date.")
        start_local = datetime.combine(self.date_start, time.min)
        end_local = datetime.combine(self.date_end + timedelta(days=1), time.min)
        tz_name = self.env.context.get("tz") or self.env.user.tz or "UTC"
        try:
            import pytz
            tz = pytz.timezone(tz_name)
            start_utc = tz.localize(start_local).astimezone(pytz.UTC).replace(tzinfo=None)
            end_utc = tz.localize(end_local).astimezone(pytz.UTC).replace(tzinfo=None)
        except Exception:
            start_utc, end_utc = start_local, end_local
        domain = [
            ("date_order", ">=", fields.Datetime.to_string(start_utc)),
            ("date_order", "<", fields.Datetime.to_string(end_utc)),
            ("state", "in", ["paid", "done", "invoiced"]),
        ]
        if self.config_ids:
            domain.append(("config_id", "in", self.config_ids.ids))
        if self.cashier_ids:
            domain.append(("user_id", "in", self.cashier_ids.ids))
        return self.env["pos.order"].search(domain, order="date_order")

    @staticmethod
    def _is_refund_order(order):
        return bool(getattr(order, "refunded_order_id", False)) or sum(order.lines.mapped("qty")) < 0

    def _report_data(self):
        orders = self._get_orders()
        lines = orders.lines
        payments = orders.payment_ids
        currency = orders[:1].currency_id or self.env.company.currency_id

        # Payment method totals (all payment rows, including negative refund payments).
        payment_rows = []
        methods = payments.mapped("payment_method_id")
        for method in methods:
            method_payments = payments.filtered(lambda p: p.payment_method_id == method)
            payment_rows.append({
                "method": method.name,
                "count": len(method_payments),
                "amount": sum(method_payments.mapped("amount")),
                "qty_orders": len(method_payments.mapped("pos_order_id")),
            })
        payment_rows.sort(key=lambda r: r["method"].lower())

        def method_class(name):
            n = (name or "").lower()
            if "credit" in n or "account" in n or "snoonu" in n or "online" in n or "q_mall" in n or "q mall" in n:
                return "credit"
            if "cash" in n:
                return "cash"
            if "visa" in n or "card" in n or "master" in n or "mada" in n:
                return "card"
            return "other"

        collection_rows = []
        for group_key, label in [("cash", "Cash"), ("card", "Card / Visa"), ("credit", "Credit"), ("other", "Other")]:
            group = payments.filtered(lambda p: method_class(p.payment_method_id.name) == group_key)
            collection_rows.append({"label": label, "qty": len(group), "amount": sum(group.mapped("amount"))})
        cash_total = sum(p.amount for p in payments if method_class(p.payment_method_id.name) == "cash")
        card_total = sum(p.amount for p in payments if method_class(p.payment_method_id.name) == "card")
        credit_total = sum(p.amount for p in payments if method_class(p.payment_method_id.name) == "credit")
        net_collections = sum(payments.mapped("amount"))

        product_map = {}
        for line in lines:
            product = line.product_id
            row = product_map.setdefault(product.id, {
                "code": product.default_code or "",
                "name": product.display_name,
                "qty": 0.0, "total": 0.0, "returned_qty": 0.0, "refund": 0.0,
                "complimentary_qty": 0.0, "complimentary_amount": 0.0,
            })
            qty = line.qty
            amount = getattr(line, "price_subtotal_incl", None)
            if amount is None:
                amount = line.price_subtotal
            row["qty"] += qty
            row["total"] += amount
            if qty < 0 or amount < 0:
                row["returned_qty"] += abs(qty)
                row["refund"] += abs(amount)
            if qty > 0 and not line.price_unit:
                row["complimentary_qty"] += qty
                row["complimentary_amount"] += abs(amount)
        product_rows = sorted(product_map.values(), key=lambda r: r["total"], reverse=True)
        return_rows = sorted(
            [r for r in product_rows if r["returned_qty"] or r["refund"]],
            key=lambda r: r["refund"], reverse=True
        )
        top_rows = sorted(
            [r for r in product_rows if r["qty"] > 0],
            key=lambda r: r["qty"], reverse=True
        )

        cashier_rows = []
        for user in orders.mapped("user_id"):
            user_orders = orders.filtered(lambda o: o.user_id == user and not self._is_refund_order(o))
            sales = sum(user_orders.mapped("amount_total"))
            cashier_rows.append({
                "name": user.name, "transactions": len(user_orders), "sales": sales,
                "average": sales / len(user_orders) if user_orders else 0.0,
                "last_sale": max(user_orders.mapped("date_order")) if user_orders else False,
            })
        cashier_rows.sort(key=lambda r: r["sales"], reverse=True)

        # Credit/account breakdown: partner/customer for orders paid through credit-like methods.
        credit_orders = orders.filtered(lambda o: any(method_class(p.payment_method_id.name) == "credit" for p in o.payment_ids))
        credit_accounts = []
        for partner in credit_orders.mapped("partner_id"):
            partner_orders = credit_orders.filtered(lambda o: o.partner_id == partner)
            partner_payments = partner_orders.payment_ids.filtered(lambda p: method_class(p.payment_method_id.name) == "credit")
            credit_accounts.append({
                "code": partner.ref or "",
                "name": partner.display_name,
                "qty": len(partner_orders),
                "amount": sum(partner_payments.mapped("amount")),
            })
        credit_accounts.sort(key=lambda r: r["amount"], reverse=True)

        hourly = {}
        config_hourly = {}
        for order in orders:
            dt = order.date_order
            if not dt:
                continue
            dt_local = fields.Datetime.context_timestamp(self, dt)
            hour = dt_local.strftime("%H:00")
            cfg = order.config_id.display_name or "POS"
            hrow = hourly.setdefault(hour, {"transactions": 0, "qty": 0.0, "sales": 0.0})
            hrow["transactions"] += 1
            hrow["qty"] += sum(order.lines.mapped("qty"))
            hrow["sales"] += order.amount_total
            ckey = (cfg, hour)
            crow = config_hourly.setdefault(ckey, {"transactions": 0, "qty": 0.0, "sales": 0.0})
            crow["transactions"] += 1
            crow["qty"] += sum(order.lines.mapped("qty"))
            crow["sales"] += order.amount_total

        # Delivery performance uses available standard/custom delivery-related fields if present.
        delivery_field_candidates = ("delivery_employee_id", "delivery_man_id", "driver_id", "delivery_user_id")
        delivery_field = next((f for f in delivery_field_candidates if f in orders._fields), None)
        delivery_rows = []
        if delivery_field:
            for driver in orders.mapped(delivery_field):
                driver_orders = orders.filtered(lambda o: getattr(o, delivery_field) == driver)
                delivery_rows.append({
                    "name": driver.display_name,
                    "deliveries": len(driver_orders),
                    "sales": sum(driver_orders.mapped("amount_total")),
                })
            delivery_rows.sort(key=lambda r: r["sales"], reverse=True)

        return {
            "payments": payment_rows, "collections": collection_rows,
            "cash_total": cash_total, "card_total": card_total,
            "credit_total": credit_total, "net_collections": net_collections,
            "products": product_rows, "returns": return_rows, "top_items": top_rows,
            "cashiers": cashier_rows, "credit_accounts": credit_accounts,
            "hourly": sorted(hourly.items()),
            "config_hourly": sorted(config_hourly.items()),
            "delivery_rows": delivery_rows, "delivery_field_found": bool(delivery_field),
            "total_sales": sum(orders.filtered(lambda o: not self._is_refund_order(o)).mapped("amount_total")),
            "order_count": len(orders.filtered(lambda o: not self._is_refund_order(o))),
            "complimentary_qty": sum(r["complimentary_qty"] for r in product_rows),
            "complimentary_amount": sum(r["complimentary_amount"] for r in product_rows),
            "currency": currency, "date_start": self.date_start, "date_end": self.date_end,
        }

    def action_print_pdf(self):
        self.ensure_one()
        return self.env.ref("q9_daily_cashier_report.action_daily_cashier_pdf").report_action(self, data=self._report_data())

    def action_export_excel(self):
        self.ensure_one()
        data = self._report_data()
        try:
            import xlsxwriter
        except ImportError:
            raise UserError("Excel export needs the xlsxwriter Python package.")
        out = io.BytesIO()
        wb = xlsxwriter.Workbook(out, {"in_memory": True})
        ws = wb.add_worksheet("Daily Cashier")
        title = wb.add_format({"bold": True, "font_size": 14})
        hdr = wb.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
        money = wb.add_format({"num_format": "#,##0.00"})
        def section(name, headers, rows, money_cols=()):
            nonlocal_row = section.row
            ws.write(nonlocal_row, 0, name, title); nonlocal_row += 1
            for c, h in enumerate(headers): ws.write(nonlocal_row, c, h, hdr)
            nonlocal_row += 1
            for row in rows:
                for c, val in enumerate(row):
                    ws.write(nonlocal_row, c, val, money if c in money_cols and isinstance(val, (int,float)) else None)
                nonlocal_row += 1
            nonlocal_row += 1
            section.row = nonlocal_row
        section.row = 0
        ws.write(0, 0, "Daily Cashier Report", title)
        ws.write(1, 0, "Date From"); ws.write(1, 1, str(self.date_start))
        ws.write(2, 0, "Date To"); ws.write(2, 1, str(self.date_end))
        section("Daily Sales Report", ["Method","Payment Lines","Amount"], [(p["method"],p["count"],p["amount"]) for p in data["payments"]], (2,))
        section("Daily Cash Collections", ["Type","Count","Net Amount"], [(r["label"],r["qty"],r["amount"]) for r in data["collections"]]+[("Cash Total", "", data["cash_total"]),("Card Total","",data["card_total"]),("Credit Total","",data["credit_total"]),("Net Collections","",data["net_collections"])], (2,))
        section("Credit Accounts Report", ["Account Code","Account Name","Orders","Credit Amount"], [(r["code"],r["name"],r["qty"],r["amount"]) for r in data["credit_accounts"]], (3,))
        section("Daily Items Report", ["Item Code","Item Name","Qty Sold","Total Price"], [(r["code"],r["name"],r["qty"],r["total"]) for r in data["products"]], (3,))
        section("Daily Return Report", ["Item Code","Item Name","Qty Returned","Total Refund"], [(r["code"],r["name"],r["returned_qty"],r["refund"]) for r in data["returns"]], (3,))
        section("Salesman Performance", ["Cashier","Transactions","Total Sales","Average Sale","Last Sale"], [(r["name"],r["transactions"],r["sales"],r["average"],str(r["last_sale"] or "")) for r in data["cashiers"]], (2,3))
        section("Delivery Man Performance", ["Delivery Person","Deliveries","Sales"], [(r["name"],r["deliveries"],r["sales"]) for r in data["delivery_rows"]], (2,))
        section("Hourly Performance", ["Hour","Transactions","Qty","Sales"], [(h,r["transactions"],r["qty"],r["sales"]) for h,r in data["hourly"]], (3,))
        section("Hourly by POS / Channel", ["POS / Channel","Hour","Transactions","Qty","Sales"], [(c,h,r["transactions"],r["qty"],r["sales"]) for (c,h),r in data["config_hourly"]], (4,))
        section("Top Selling Items", ["Item Code","Item Name","Qty Sold","Revenue","Average Price"], [(r["code"],r["name"],r["qty"],r["total"],(r["total"]/r["qty"] if r["qty"] else 0)) for r in data["top_items"]], (3,4))
        ws.set_column("A:A", 26); ws.set_column("B:B", 48); ws.set_column("C:E", 18)
        wb.close()
        out.seek(0)
        fname = "Daily_Cashier_%s.xlsx" % self.date_start
        self.write({"report_file": base64.b64encode(out.read()), "report_filename": fname})
        return {"type":"ir.actions.act_url","url":"/web/content/?model=q9.daily.cashier.wizard&id=%s&field=report_file&filename_field=report_filename&download=true" % self.id,"target":"self"}
