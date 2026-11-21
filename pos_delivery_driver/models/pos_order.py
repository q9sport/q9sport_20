# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


DELIVERY_PRODUCT_ID = 38
DELIVERY_PRODUCT_BARCODE = "10002"


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    is_delivery_driver = fields.Boolean(
        string="Delivery Driver",
        default=False,
        index=True,
    )

    delivery_order_ids = fields.One2many(
        "pos.order",
        "delivery_employee_id",
        string="Delivery Orders",
        readonly=True,
    )

    delivery_order_count = fields.Integer(
        string="Total Delivery Orders",
        compute="_compute_delivery_statistics",
    )

    delivery_pending_count = fields.Integer(
        string="Open Deliveries",
        compute="_compute_delivery_statistics",
    )

    delivery_delivered_count = fields.Integer(
        string="Delivered Orders",
        compute="_compute_delivery_statistics",
    )

    delivery_failed_count = fields.Integer(
        string="Failed Orders",
        compute="_compute_delivery_statistics",
    )

    @api.depends(
        "delivery_order_ids",
        "delivery_order_ids.delivery_status",
        "delivery_order_ids.is_delivery_order",
    )
    def _compute_delivery_statistics(self):
        for employee in self:
            orders = employee.delivery_order_ids.filtered(
                lambda order: order.is_delivery_order
            )

            employee.delivery_order_count = len(orders)

            employee.delivery_pending_count = len(
                orders.filtered(
                    lambda order: order.delivery_status in (
                        "pending",
                        "assigned",
                        "in_delivery",
                    )
                )
            )

            employee.delivery_delivered_count = len(
                orders.filtered(
                    lambda order: order.delivery_status == "delivered"
                )
            )

            employee.delivery_failed_count = len(
                orders.filtered(
                    lambda order: order.delivery_status == "failed"
                )
            )

    def _get_delivery_action(self, title, extra_domain=None):
        self.ensure_one()

        domain = [
            ("is_delivery_order", "=", True),
            ("delivery_employee_id", "=", self.id),
        ]

        if extra_domain:
            domain += extra_domain

        return {
            "type": "ir.actions.act_window",
            "name": title,
            "res_model": "pos.order",
            "view_mode": "kanban,list,form",
            "domain": domain,
            "context": {
                "group_by": [
                    "delivery_employee_id",
                    "delivery_status",
                ],
            },
        }

    def action_view_delivery_orders(self):
        self.ensure_one()
        return self._get_delivery_action(
            _("Delivery Orders - %s") % self.name
        )

    def action_view_open_delivery_orders(self):
        self.ensure_one()
        return self._get_delivery_action(
            _("Open Delivery Orders"),
            [
                (
                    "delivery_status",
                    "in",
                    ["pending", "assigned", "in_delivery"],
                )
            ],
        )

    def action_view_delivered_delivery_orders(self):
        self.ensure_one()
        return self._get_delivery_action(
            _("Delivered Orders"),
            [("delivery_status", "=", "delivered")],
        )

    def action_view_failed_delivery_orders(self):
        self.ensure_one()
        return self._get_delivery_action(
            _("Failed Orders"),
            [("delivery_status", "=", "failed")],
        )


class PosOrder(models.Model):
    _inherit = "pos.order"

    is_delivery_order = fields.Boolean(
        string="Delivery Order",
        compute="_compute_is_delivery_order",
        store=True,
        index=True,
        copy=False,
    )

    delivery_employee_id = fields.Many2one(
        "hr.employee",
        string="Delivery Driver",
        index=True,
        copy=False,
        ondelete="restrict",
        domain=[
            ("active", "=", True),
            ("is_delivery_driver", "=", True),
        ],
    )

    delivery_status = fields.Selection(
        [
            ("pending", "Created"),
            ("assigned", "Dispatched"),
            ("in_delivery", "Driver Enroute"),
            ("delivered", "Delivered"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="Delivery Status",
        default="pending",
        index=True,
        copy=False,
    )

    delivery_assigned_date = fields.Datetime(
        string="Assigned Date",
        readonly=True,
        copy=False,
    )

    delivery_date = fields.Datetime(
        string="Delivered Date",
        readonly=True,
        copy=False,
    )

    is_cash_on_delivery = fields.Boolean(
        string="Cash on Delivery",
        default=True,
        copy=False,
    )

    delivery_collected = fields.Boolean(
        string="Collected by Driver",
        default=False,
        copy=False,
    )

    delivery_collected_amount = fields.Monetary(
        string="Collected Amount",
        currency_field="currency_id",
        default=0.0,
        copy=False,
    )

    delivery_payment_status = fields.Selection(
        [
            ("unpaid", "Unpaid"),
            ("partial", "Partially Paid"),
            ("paid", "Paid"),
        ],
        string="Payment Status",
        compute="_compute_delivery_payment_status",
        store=True,
    )

    delivery_customer_address = fields.Char(
        string="Customer Address",
        related="partner_id.contact_address",
        readonly=True,
    )

    delivery_latitude = fields.Float(
        string="Customer Latitude",
        related="partner_id.partner_latitude",
        readonly=True,
    )

    delivery_longitude = fields.Float(
        string="Customer Longitude",
        related="partner_id.partner_longitude",
        readonly=True,
    )

    delivery_location_url = fields.Char(
        string="Customer Map",
        compute="_compute_delivery_location_url",
    )
    account_move_payment_state = fields.Selection(
        related="account_move.payment_state",
        string="Invoice Payment Status",
        readonly=True,)

    # ---------------------------------------------------------
    # Detect delivery product
    # ---------------------------------------------------------

    @api.depends(
        "lines",
        "lines.product_id",
        "lines.product_id.barcode",
    )
    def _compute_is_delivery_order(self):
        for order in self:
            order.is_delivery_order = any(
                line.product_id
                and (
                    line.product_id.id == DELIVERY_PRODUCT_ID
                    or (
                        (line.product_id.barcode or "").strip()
                        == DELIVERY_PRODUCT_BARCODE
                    )
                )
                for line in order.lines
            )

    # ---------------------------------------------------------
    # Payment status
    # ---------------------------------------------------------

    @api.depends("amount_paid", "amount_total")
    def _compute_delivery_payment_status(self):
        for order in self:
            paid = order.amount_paid or 0.0
            total = order.amount_total or 0.0

            if paid <= 0 or total <= 0:
                order.delivery_payment_status = "unpaid"
            elif paid >= total:
                order.delivery_payment_status = "paid"
            else:
                order.delivery_payment_status = "partial"

    # ---------------------------------------------------------
    # Customer GPS URL
    # ---------------------------------------------------------

    @api.depends("delivery_latitude", "delivery_longitude")
    def _compute_delivery_location_url(self):
        for order in self:
            if order.delivery_latitude and order.delivery_longitude:
                order.delivery_location_url = (
                    "https://www.google.com/maps?q=%s,%s"
                    % (
                        order.delivery_latitude,
                        order.delivery_longitude,
                    )
                )
            else:
                order.delivery_location_url = False

    # ---------------------------------------------------------
    # Drivers for POS popup
    # ---------------------------------------------------------

    @api.model
    def get_active_delivery_employees(self):
        employees = self.env["hr.employee"].sudo().search(
            [
                ("active", "=", True),
                ("is_delivery_driver", "=", True),
            ],
            order="name asc",
        )

        result = []

        for employee in employees:
            orders = self.sudo().search(
                [
                    ("is_delivery_order", "=", True),
                    ("delivery_employee_id", "=", employee.id),
                ]
            )

            open_orders = orders.filtered(
                lambda order: order.delivery_status in (
                    "pending",
                    "assigned",
                    "in_delivery",
                )
            )

            result.append({
                "id": employee.id,
                "name": employee.name,
                "order_count": len(orders),
                "open_count": len(open_orders),
                "delivered_count": len(
                    orders.filtered(
                        lambda order: order.delivery_status == "delivered"
                    )
                ),
                "failed_count": len(
                    orders.filtered(
                        lambda order: order.delivery_status == "failed"
                    )
                ),
            })

        return result

    # ---------------------------------------------------------
    # Receive custom POS fields
    # ---------------------------------------------------------

    @api.model
    def _order_fields(self, ui_order):
        result = super()._order_fields(ui_order)

        driver_id = ui_order.get("delivery_employee_id") or False

        result.update({
            "delivery_employee_id": driver_id,
            "delivery_status": (
                ui_order.get("delivery_status")
                or ("assigned" if driver_id else "pending")
            ),
            "is_cash_on_delivery": bool(
                ui_order.get("is_cash_on_delivery", True)
            ),
        })

        if driver_id:
            result["delivery_assigned_date"] = fields.Datetime.now()

        return result

    # ---------------------------------------------------------
    # Validate driver
    # ---------------------------------------------------------

    @api.constrains("delivery_employee_id")
    def _check_delivery_employee(self):
        for order in self:
            employee = order.delivery_employee_id

            if employee and (
                not employee.active
                or not employee.is_delivery_driver
            ):
                raise ValidationError(
                    _("The selected employee is not an active delivery driver.")
                )

    # ---------------------------------------------------------
    # Update delivery data
    # ---------------------------------------------------------

    def write(self, vals):
        vals = dict(vals)

        if vals.get("delivery_employee_id"):
            employee = self.env["hr.employee"].sudo().browse(
                vals["delivery_employee_id"]
            ).exists()

            if (
                not employee
                or not employee.active
                or not employee.is_delivery_driver
            ):
                raise UserError(
                    _("Select an active employee marked as a Delivery Driver.")
                )

            vals.setdefault(
                "delivery_assigned_date",
                fields.Datetime.now(),
            )

            vals.setdefault("delivery_status", "assigned")

        elif "delivery_employee_id" in vals:
            vals.setdefault("delivery_status", "pending")
            vals.setdefault("delivery_assigned_date", False)

        if vals.get("delivery_status") == "delivered":
            vals.setdefault("delivery_date", fields.Datetime.now())
        elif "delivery_status" in vals:
            vals.setdefault("delivery_date", False)

        return super().write(vals)

    # ---------------------------------------------------------
    # Open driver assignment wizard
    # ---------------------------------------------------------

    def action_open_delivery_driver_wizard(self):
        self.ensure_one()

        if not self.is_delivery_order:
            raise UserError(_("This is not a delivery order."))

        if self.delivery_status in ("delivered", "cancelled"):
            raise UserError(
                _("A completed or cancelled order cannot be reassigned.")
            )

        view = self.env.ref(
            "pos_delivery_driver.view_pos_delivery_driver_assign_wizard_form"
        )

        wizard = self.env[
            "pos.delivery.driver.assign.wizard"
        ].sudo().create({
            "pos_order_id": self.id,
            "delivery_employee_id": self.delivery_employee_id.id or False,
        })

        return {
            "type": "ir.actions.act_window",
            "name": _("Assign Delivery Driver"),
            "res_model": "pos.delivery.driver.assign.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "views": [(view.id, "form")],
            "target": "new",
        }

    # ---------------------------------------------------------
    # Delivery actions
    # ---------------------------------------------------------

    def action_delivery_start(self):
        for order in self:
            if not order.is_delivery_order:
                raise UserError(_("This is not a delivery order."))

            if not order.delivery_employee_id:
                raise UserError(_("Assign a delivery driver first."))

            if order.delivery_status in ("delivered", "cancelled"):
                raise UserError(_("This order can no longer be started."))

            order.write({
                "delivery_status": "in_delivery",
            })

        return True

    def action_delivery_mark_delivered(self):
        for order in self:
            if not order.is_delivery_order:
                raise UserError(_("This is not a delivery order."))

            if not order.delivery_employee_id:
                raise UserError(_("Assign a delivery driver first."))

            if order.delivery_status in ("cancelled", "failed"):
                raise UserError(_("This order cannot be marked as delivered."))

            order.write({
                "delivery_status": "delivered",
            })

        return True

    def action_delivery_mark_failed(self):
        for order in self:
            if not order.is_delivery_order:
                raise UserError(_("This is not a delivery order."))

            if order.delivery_status in ("delivered", "cancelled"):
                raise UserError(_("This order can no longer be marked as failed."))

            order.write({
                "delivery_status": "failed",
            })

        return True

    def action_delivery_record_collection(self):
        for order in self:
            if not order.is_delivery_order:
                raise UserError(_("This is not a delivery order."))

            if order.delivery_collected:
                raise UserError(_("Collection has already been confirmed."))

            if order.delivery_collected_amount <= 0:
                raise UserError(_("Enter the collected amount first."))

            if order.delivery_collected_amount > order.amount_total:
                raise UserError(
                    _("Collected amount cannot exceed the order total.")
                )

            order.write({
                "delivery_collected": True,
            })

        return True

    # ---------------------------------------------------------
    # Register invoice payment
    # ---------------------------------------------------------

    def action_register_payment(self):
        self.ensure_one()

        invoice = self.account_move

        if not invoice:
            raise UserError(
                _("There is no invoice linked to this POS order.")
            )

        if invoice.state != "posted":
            raise UserError(
                _("You can only register payment for a posted invoice.")
            )

        if invoice.payment_state == "paid":
            raise UserError(
                _("This invoice is already fully paid.")
            )

        return invoice.action_register_payment()

    # ---------------------------------------------------------
    # Payment history
    # ---------------------------------------------------------

    def action_view_delivery_payments(self):
        self.ensure_one()

        return {
            "type": "ir.actions.act_window",
            "name": _("POS Payments"),
            "res_model": "pos.payment",
            "view_mode": "list,form",
            "domain": [
                ("pos_order_id", "=", self.id),
            ],
            "target": "current",
        }

    # ---------------------------------------------------------
    # Open customer map
    # ---------------------------------------------------------

    def action_view_delivery_location(self):
        self.ensure_one()

        if not self.delivery_location_url:
            raise UserError(
                _("No GPS coordinates are available for this customer.")
            )

        return {
            "type": "ir.actions.act_url",
            "url": self.delivery_location_url,
            "target": "new",
        }


# =============================================================
# DELIVERY DRIVER ASSIGN WIZARD
# =============================================================

class PosDeliveryDriverAssignWizard(models.TransientModel):
    _name = "pos.delivery.driver.assign.wizard"
    _description = "Assign POS Delivery Driver"

    pos_order_id = fields.Many2one(
        "pos.order",
        string="Delivery Order",
        required=True,
        readonly=True,
    )

    delivery_employee_id = fields.Many2one(
        "hr.employee",
        string="Delivery Driver",
        required=True,
        domain=[
            ("active", "=", True),
            ("is_delivery_driver", "=", True),
        ],
        default=lambda self: self.env["pos.order"].browse(
            self.env.context.get("default_pos_order_id")
        ).delivery_employee_id.id,
    )

    def action_assign_driver(self):
        self.ensure_one()

        order = self.pos_order_id.sudo().exists()
        driver = self.delivery_employee_id.sudo()

        if not order or not order.is_delivery_order:
            raise UserError(
                _("The delivery order could not be found.")
            )

        if order.delivery_status in ("delivered", "cancelled"):
            raise UserError(
                _("A completed or cancelled order cannot be reassigned.")
            )

        if (
            not driver
            or not driver.active
            or not driver.is_delivery_driver
        ):
            raise UserError(
                _("Select an active employee marked as a Delivery Driver.")
            )

        order.write({
            "delivery_employee_id": driver.id,
        })

        return {
            "type": "ir.actions.act_window_close",
        }
