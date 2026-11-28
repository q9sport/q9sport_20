from odoo import fields, models, _, release, api

class AppsInkerp(models.Model):
    _name = 'app.data.inkerp'
    _description = 'INKERP Apps'

    name = fields.Char(string="Name", readonly=True)
    sequence = fields.Integer(string="Sequence")
    technical_name = fields.Char(string="Technical Name", readonly=True)
    icon = fields.Binary(string="Icon", readonly=True)
    icon_name = fields.Char(string="Icon Name", readonly=True)
    is_odoo = fields.Boolean(string="Redirect to odoo apps", default=True, readonly=True)
    app_url = fields.Char(string="App Url", compute='compute_app_url', readonly=True)
    app_status = fields.Selection(
        [('installed', 'Installed'), ('installable', 'Installable'), ('need_buy', 'Need To Buy')],
        default='need_buy')
    category_id = fields.Many2one('ir.module.category', string='Category', index=True, readonly=True)
    special_category_id = fields.Many2one('special.category', string='Special Category', index=True, readonly=True)
    description = fields.Html(string="Description")
    summary = fields.Text(string="Summary")

    # Offer Fields
    is_offer = fields.Boolean(string="In Offer")
    offer_price = fields.Char(string="Offer Price")
    actual_price = fields.Char(string="Actual Price")
    offer_end = fields.Date(string="Offers End Date")
    is_free = fields.Boolean(string="Free App")


    def compute_module_state(self):
        for app_id in self:
            app_id.app_status = 'need_buy'
            module_id = self.env['ir.module.module'].search([('name', '=', app_id.technical_name)], limit=1)
            if not module_id:
                app_id.app_status = 'need_buy'
            else:
                app_id.app_status = 'installable' if module_id.state == 'installable' else 'installed'

    def compute_app_url(self):
        for app_id in self:
            app_id.app_url = f'https://apps.odoo.com/apps/modules/{release.version}/{app_id.technical_name}' if app_id.technical_name else 'https://apps.odoo.com/apps/modules/browse?author=INKERP'

    def button_contact_us(self):
        return {
            'type': "ir.actions.act_url",
            'url': 'https://inkerp.com/contact/',
            'target': '_blank'
        }

    def button_buy(self):
        return {
            'type': "ir.actions.act_url",
            'url': self.app_url,
            'target': '_blank'
        }

    def button_install(self):
        module = self.env['ir.module.module'].search([('name', '=', self.technical_name)], limit=1)
        module.button_immediate_install()
        self.app_status = 'installed'

    def button_upgrade(self):
        module = self.env['ir.module.module'].search([('name', '=', self.technical_name)], limit=1)
        module.button_immediate_upgrade()

    def button_uninstall(self):
        self.app_status = 'installable'
        module = self.env['ir.module.module'].search([('name', '=', self.technical_name)], limit=1)
        return {
            'type': 'ir.actions.act_window',
            'target': 'new',
            'name': _('Uninstall module'),
            'view_mode': 'form',
            'res_model': 'base.module.uninstall',
            'context': {'default_module_id': module.id},
        }
