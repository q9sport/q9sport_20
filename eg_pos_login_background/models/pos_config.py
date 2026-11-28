from odoo import fields, models


class PosConfig(models.Model):
    _inherit = 'pos.config'

    eg_pos_login_background_enable = fields.Boolean(string="POS Login Page Background", default=True)
    pos_login_background = fields.Binary(string="POS Login Background")
    allow_login_background = fields.Boolean(string="Allow Login Background")
