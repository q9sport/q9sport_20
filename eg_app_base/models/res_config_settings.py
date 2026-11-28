from odoo import fields, models, api


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    is_auto_fetch_apps = fields.Boolean(string="Auto Fetch Apps", config_parameter="eg_app_base.is_auto_fetch_apps")

    @api.onchange('is_auto_fetch_apps')
    def _onchange_enable_auto_fetch_apps(self):
        cron_id = self.env.ref('eg_app_base.eg_ir_cron_for_weekly_auto_fetch_apps')
        cron_id.active = self.is_auto_fetch_apps
