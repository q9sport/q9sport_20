from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    approval_state = fields.Selection(
        selection=[
            ("not_required", "Not Required"),
            ("draft", "Draft"),
            ("waiting", "Waiting for Approval"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="Approval Status",
        default="not_required",
        copy=False,
        readonly=True,
        tracking=True,
    )
    approval_requested_by = fields.Many2one(
        "res.users",
        string="Submitted By",
        readonly=True,
        copy=False,
        tracking=True,
    )
    approval_requested_date = fields.Datetime(
        string="Submission Date",
        readonly=True,
        copy=False,
    )
    approved_by = fields.Many2one(
        "res.users",
        string="Approved By",
        readonly=True,
        copy=False,
        tracking=True,
    )
    approval_date = fields.Datetime(
        string="Approval Date",
        readonly=True,
        copy=False,
    )
    rejected_by = fields.Many2one(
        "res.users",
        string="Rejected By",
        readonly=True,
        copy=False,
        tracking=True,
    )
    rejection_date = fields.Datetime(
        string="Rejection Date",
        readonly=True,
        copy=False,
    )
    rejection_reason = fields.Text(
        string="Rejection Reason",
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        internal_type_ids = set(
            self.env["stock.picking.type"].search([("code", "=", "internal")]).ids
        )
        for vals in vals_list:
            picking_type_id = vals.get("picking_type_id")
            if picking_type_id:
                vals.setdefault(
                    "approval_state",
                    "draft" if picking_type_id in internal_type_ids else "not_required",
                )
        return super().create(vals_list)

    def write(self, vals):
        # Keep the approval lifecycle aligned if an editable transfer changes type.
        if "picking_type_id" in vals and "approval_state" not in vals:
            picking_type = self.env["stock.picking.type"].browse(vals["picking_type_id"])
            vals = dict(vals)
            vals["approval_state"] = (
                "draft" if picking_type.code == "internal" else "not_required"
            )
        return super().write(vals)

    def _check_internal_transfer_approval(self):
        blocked = self.filtered(
            lambda picking: (
                picking.picking_type_id.code == "internal"
                and picking.approval_state != "approved"
            )
        )
        if blocked:
            names = ", ".join(blocked.mapped("display_name"))
            raise UserError(
                _(
                    "Internal transfers must be approved by an Inventory Manager "
                    "before validation. Transfers: %s"
                )
                % names
            )

    def button_validate(self):
        self._check_internal_transfer_approval()
        return super().button_validate()

    def _action_done(self, *args, **kwargs):
        # Server-side safeguard for direct calls that bypass button_validate.
        self._check_internal_transfer_approval()
        return super()._action_done(*args, **kwargs)

    def action_submit_for_approval(self):
        for picking in self:
            if picking.picking_type_id.code != "internal":
                raise UserError(_("Approval is only available for internal transfers."))
            if picking.state in ("done", "cancel"):
                raise UserError(_("A completed or cancelled transfer cannot be submitted."))
            if picking.approval_state not in ("draft", "rejected"):
                raise UserError(_("This transfer has already been submitted or approved."))
            picking.write({
                "approval_state": "waiting",
                "approval_requested_by": self.env.user.id,
                "approval_requested_date": fields.Datetime.now(),
                "approved_by": False,
                "approval_date": False,
                "rejected_by": False,
                "rejection_date": False,
                "rejection_reason": False,
            })
            picking.message_post(body=_("Internal transfer submitted for approval."))
        return True

    def action_approve_internal_transfer(self):
        if not self.env.user.has_group("stock.group_stock_manager"):
            raise AccessError(_("Only Inventory Managers can approve internal transfers."))
        for picking in self:
            if picking.picking_type_id.code != "internal":
                raise UserError(_("Only internal transfers can be approved here."))
            if picking.state in ("done", "cancel"):
                raise UserError(_("A completed or cancelled transfer cannot be approved."))
            if picking.approval_state != "waiting":
                raise UserError(_("Only transfers waiting for approval can be approved."))
            picking.write({
                "approval_state": "approved",
                "approved_by": self.env.user.id,
                "approval_date": fields.Datetime.now(),
                "rejected_by": False,
                "rejection_date": False,
            })
            picking.message_post(body=_("Internal transfer approved."))
        return True

    def action_reject_internal_transfer(self):
        if not self.env.user.has_group("stock.group_stock_manager"):
            raise AccessError(_("Only Inventory Managers can reject internal transfers."))
        for picking in self:
            if picking.picking_type_id.code != "internal":
                raise UserError(_("Only internal transfers can be rejected here."))
            if picking.state in ("done", "cancel"):
                raise UserError(_("A completed or cancelled transfer cannot be rejected."))
            if picking.approval_state != "waiting":
                raise UserError(_("Only transfers waiting for approval can be rejected."))
            picking.write({
                "approval_state": "rejected",
                "rejected_by": self.env.user.id,
                "rejection_date": fields.Datetime.now(),
            })
            body = _("Internal transfer rejected.")
            if picking.rejection_reason:
                body += _(" Reason: %s") % picking.rejection_reason
            picking.message_post(body=body)
        return True
