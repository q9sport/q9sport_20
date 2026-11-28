from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    eg_pos_login_background_enable = fields.Boolean(string="POS Login Page Background",
                                                    related='pos_config_id.eg_pos_login_background_enable',
                                                    readonly=False)
    pos_login_background = fields.Binary(string="POS Login Background", related='pos_config_id.pos_login_background',
                                         readonly=False)
    allow_login_background = fields.Boolean(string="Allow Login Background",
                                            related='pos_config_id.allow_login_background',
                                            readonly=False)
