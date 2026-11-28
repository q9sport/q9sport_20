from odoo import fields, models


class PosConfig(models.Model):
    _inherit = 'pos.config'

    allowed_user_ids = fields.Many2many(
        'res.users',
        'tamadoo_pos_config_allowed_user_rel',
        'config_id',
        'user_id',
        string='Allowed Users',
        domain=[('share', '=', False)],
        help='Only these users see and open this Point of Sale. '
             'Leave empty to let every Point of Sale user open it. '
             'Point of Sale administrators always see every shop.',
    )
