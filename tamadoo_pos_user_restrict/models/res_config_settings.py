from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_allowed_user_ids = fields.Many2many(
        related='pos_config_id.allowed_user_ids',
        readonly=False,
    )
