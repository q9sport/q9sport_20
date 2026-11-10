# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    is_delivery_driver = fields.Boolean(
        string="Delivery Driver",
        default=False,
        copy=False,
        index=True,
        help="Enable this employee to be selected as a POS delivery driver.",
    )
    delivery_order_ids = fields.One2many(
        "pos.order", "delivery_employee_id", string="Delivery Orders", readonly=True
    )
    delivery_order_count = fields.Integer(
        string="Total Orders", compute="_compute_delivery_statistics"
    )
    delivery_pending_count = fields.Integer(
        string="Open Orders", compute="_compute_delivery_statistics"
    )
    delivery_delivered_count = fields.Integer(
        string="Delivered Orders", compute="_compute_delivery_statistics"
    )
    delivery_failed_count = fields.Integer(
        string="Failed Orders", compute="_compute_delivery_statistics"
    )

    @api.depends(
        "delivery_order_ids",
        "delivery_order_ids.delivery_status",
        "delivery_order_ids.state",
    )
    def _compute_delivery_statistics(self):
        for employee in self:
            orders = employee.delivery_order_ids
            employee.delivery_order_count = len(orders)
            employee.delivery_pending_count = len(
                orders.filtered(lambda order: order.delivery_status in ("pending", "assigned", "in_delivery"))
            )
            employee.delivery_delivered_count = len(
                orders.filtered(lambda order: order.delivery_status == "delivered")
            )
            employee.delivery_failed_count = len(
                orders.filtered(lambda order: order.delivery_status == "failed")
            )

    def _delivery_orders_action(self, title, extra_domain=None):
        self.ensure_one()
        domain = [("delivery_employee_id", "=", self.id)]
        if extra_domain:
            domain += extra_domain
        return {
            "type": "ir.actions.act_window",
            "name": title,
            "res_model": "pos.order",
            "view_mode": "list,form",
            "domain": domain,
            "context": {"default_delivery_employee_id": self.id},
        }

    def action_view_delivery_orders(self):
        return self._delivery_orders_action(_("Delivery Orders - %s") % self.name)

    def action_view_open_delivery_orders(self):
        return self._delivery_orders_action(
            _("Open Delivery Orders - %s") % self.name,
            [("delivery_status", "in", ["pending", "assigned", "in_delivery"])],
        )

    def action_view_delivered_delivery_orders(self):
        return self._delivery_orders_action(
            _("Delivered Orders - %s") % self.name,
            [("delivery_status", "=", "delivered")],
        )

    def action_view_failed_delivery_orders(self):
        return self._delivery_orders_action(
            _("Failed Orders - %s") % self.name,
            [("delivery_status", "=", "failed")],
        )


class PosOrder(models.Model):
    _inherit = "pos.order"

    delivery_employee_id = fields.Many2one(
        "hr.employee",
        string="Delivery Driver",
        index=True,
        copy=False,
        ondelete="restrict",
        domain=[("active", "=", True), ("is_delivery_driver", "=", True)],
    )
    delivery_status = fields.Selection(
        [
            ("pending", "Waiting"),
            ("assigned", "Assigned"),
            ("in_delivery", "On the Way"),
            ("delivered", "Delivered"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="Delivery Status",
        default="pending",
        copy=False,
        index=True,
    )
    delivery_assigned_date = fields.Datetime(
        string="Assigned Date", readonly=True, copy=False
    )
    delivery_date = fields.Datetime(
        string="Delivered Date", readonly=True, copy=False
    )
    is_cash_on_delivery = fields.Boolean(
        string="Cash on Delivery", default=False, copy=False, index=True
    )
    delivery_collected = fields.Boolean(
        string="Collected by Driver", default=False, copy=False
    )
    delivery_collected_amount = fields.Monetary(
        string="Amount Collected by Driver",
        currency_field="currency_id",
        default=0.0,
        copy=False,
    )

    @api.model
    def get_active_delivery_employees(self):
        """Return only the minimum employee data needed by POS cashiers."""
        employees = self.env["hr.employee"].sudo().search(
            [
                ("active", "=", True),
                ("is_delivery_driver", "=", True),
            ],
            order="name",
        )
        result = []
        for employee in employees:
            orders = self.sudo().search([("delivery_employee_id", "=", employee.id)])
            result.append({
                "id": employee.id,
                "name": employee.name,
                "order_count": len(orders),
                "open_count": len(orders.filtered(
                    lambda order: order.delivery_status in ("pending", "assigned", "in_delivery")
                )),
                "delivered_count": len(orders.filtered(
                    lambda order: order.delivery_status == "delivered"
                )),
            })
        return result

    @api.model
    def _order_fields(self, ui_order):
        result = super()._order_fields(ui_order)
        driver_id = ui_order.get("delivery_employee_id") or False
        result.update({
            "delivery_employee_id": driver_id,
            "delivery_status": ui_order.get("delivery_status") or ("assigned" if driver_id else "pending"),
            "is_cash_on_delivery": bool(ui_order.get("is_cash_on_delivery")),
        })
        if driver_id:
            result["delivery_assigned_date"] = fields.Datetime.now()
        return result

    @api.constrains("delivery_employee_id")
    def _check_delivery_employee(self):
        for order in self:
            employee = order.delivery_employee_id
            if employee and (not employee.active or not employee.is_delivery_driver):
                raise ValidationError(_("The selected employee is not an active delivery driver."))

    def write(self, vals):
        vals = dict(vals)
        if "delivery_employee_id" in vals and vals["delivery_employee_id"]:
            employee = self.env["hr.employee"].sudo().browse(vals["delivery_employee_id"]).exists()
            if not employee or not employee.active or not employee.is_delivery_driver:
                raise UserError(_("Select an active employee marked as a Delivery Driver."))
            if "delivery_assigned_date" not in vals:
                vals["delivery_assigned_date"] = fields.Datetime.now()
            if "delivery_status" not in vals:
                vals["delivery_status"] = "assigned"
        if vals.get("delivery_employee_id") is False:
            vals.setdefault("delivery_status", "pending")
            vals.setdefault("is_cash_on_delivery", False)
            vals.setdefault("delivery_assigned_date", False)
        if vals.get("delivery_status") == "delivered" and "delivery_date" not in vals:
            vals["delivery_date"] = fields.Datetime.now()
        elif "delivery_status" in vals and vals.get("delivery_status") != "delivered":
            vals.setdefault("delivery_date", False)
        return super().write(vals)

    def action_delivery_start(self):
        for order in self:
            if not order.delivery_employee_id:
                raise UserError(_("Assign a delivery driver first."))
            if order.delivery_status in ("delivered", "cancelled"):
                raise UserError(_("This order can no longer be started for delivery."))
            order.write({"delivery_status": "in_delivery"})

    def action_delivery_mark_delivered(self):
        for order in self:
            if not order.delivery_employee_id:
                raise UserError(_("Assign a delivery driver first."))
            order.write({"delivery_status": "delivered"})

    def action_delivery_mark_failed(self):
        self.write({"delivery_status": "failed"})

    def action_delivery_record_collection(self):
        for order in self:
            if order.delivery_collected_amount < 0:
                raise UserError(_("Collected amount cannot be negative."))
            if order.delivery_collected_amount > order.amount_total:
                raise UserError(_("Collected amount cannot exceed the order total."))
            order.delivery_collected = True
