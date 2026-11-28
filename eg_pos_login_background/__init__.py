from . import models


def _enable_eg_pos_login_background_post_init(env):
    env['ir.config_parameter'].sudo().set_str("eg_pos_login_background.eg_pos_login_background_enable", 'True')
