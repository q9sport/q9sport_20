from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    _APPROVAL_PROTECTED_FIELDS = {
        "approval_state",
        "approval_requested_by",
        "approval_requested_date",
        "approved_by",
        "approval_date",
        "rejected_by",
        "rejection_date",
    }

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
        "res.users", string="Submitted By", readonly=True, copy=False, tracking=True
    )
    approval_requested_date = fields.Datetime(
        string="Submission Date", readonly=True, copy=False
    )
    approved_by = fields.Many2one(
        "res.users", string="Approved By", readonly=True, copy=False, tracking=True
    )
    approval_date = fields.Datetime(
        string="Approval Date", readonly=True, copy=False
    )
    rejected_by = fields.Many2one(
        "res.users", string="Rejected By", readonly=True, copy=False, tracking=True
    )
    rejection_date = fields.Datetime(
        string="Rejection Date", readonly=True, copy=False
    )
    rejection_reason = fields.Text(string="Rejection Reason", copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        internal_types = self.env["stock.picking.type"].search(
            [("code", "=", "internal")]
        )
        internal_type_ids = set(internal_types.ids)
        prepared_vals = []
        for incoming_vals in vals_list:
            vals = dict(incoming_vals)
            # Never accept caller-supplied approval status or audit metadata at creation.
            for protected_field in self._APPROVAL_PROTECTED_FIELDS:
                vals.pop(protected_field, None)
            picking_type_id = vals.get("picking_type_id")
            if picking_type_id:
                vals["approval_state"] = (
                    "draft" if picking_type_id in internal_type_ids else "not_required"
                )
            prepared_vals.append(vals)
        return super().create(prepared_vals)

    def write(self, vals):
        vals = dict(vals)

        # Protect workflow status and audit fields against direct ORM/RPC writes.
        if self._APPROVAL_PROTECTED_FIELDS.intersection(vals) and not self.env.su:
            raise AccessError(
                _("Approval workflow fields can only be changed by the approval actions.")
            )

        # Automatically initialize approval status if the operation type changes.
        if "picking_type_id" in vals and "approval_state" not in vals:
            picking_type = self.env["stock.picking.type"].browse(vals["picking_type_id"])
            vals["approval_state"] = (
                "draft" if picking_type.code == "internal" else "not_required"
            )
            return super().write(vals)

        return super().write(vals)

    def _approval_write(self, vals):
        """Write workflow fields after the calling action has checked authorization."""
        return self.sudo().write(vals)

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
                    "Internal transfers must be approved by a member of the "
                    "Inventory Administrator group before validation. Transfers: %s"
                ) % names
            )

    def button_validate(self):
        self._check_internal_transfer_approval()
        return super().button_validate()

    def _action_done(self, *args, **kwargs):
        # Protect direct server-side completion calls as well as the Validate button.
        self._check_internal_transfer_approval()
        return super()._action_done(*args, **kwargs)

    def action_submit_for_approval(self):
        for picking in self:
            if picking.picking_type_id.code != "internal":
                raise UserError(_("Approval is available for internal transfers only."))
            if picking.state in ("done", "cancel"):
                raise UserError(_("A completed or cancelled transfer cannot be submitted."))
            if picking.approval_state not in ("draft", "rejected"):
                raise UserError(_("This transfer has already been submitted or approved."))

            picking._approval_write({
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
            raise AccessError(
                _("Only members of the Inventory Administrator group can approve transfers.")
            )

        for picking in self:
            if picking.picking_type_id.code != "internal":
                raise UserError(_("Only internal transfers can be approved."))
            if picking.state in ("done", "cancel"):
                raise UserError(_("A completed or cancelled transfer cannot be approved."))
            if picking.approval_state != "waiting":
                raise UserError(_("Only transfers waiting for approval can be approved."))

            picking._approval_write({
                "approval_state": "approved",
                "approved_by": self.env.user.id,
                "approval_date": fields.Datetime.now(),
                "rejected_by": False,
                "rejection_date": False,
                "rejection_reason": False,
            })
            picking.message_post(body=_("Internal transfer approved."))
        return True

    def action_reject_internal_transfer(self):
        if not self.env.user.has_group("stock.group_stock_manager"):
            raise AccessError(
                _("Only members of the Inventory Administrator group can reject transfers.")
            )

        for picking in self:
            if picking.picking_type_id.code != "internal":
                raise UserError(_("Only internal transfers can be rejected."))
            if picking.state in ("done", "cancel"):
                raise UserError(_("A completed or cancelled transfer cannot be rejected."))
            if picking.approval_state != "waiting":
                raise UserError(_("Only transfers waiting for approval can be rejected."))

            picking._approval_write({
                "approval_state": "rejected",
                "rejected_by": self.env.user.id,
                "rejection_date": fields.Datetime.now(),
            })
            body = _("Internal transfer rejected.")
            if picking.rejection_reason:
                body += _(" Reason: %s") % picking.rejection_reason
            picking.message_post(body=body)
        return True
