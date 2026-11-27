from odoo import api, fields, models, _
from odoo.exceptions import AccessError
from .profile import DESIGN_FIELDS


class PosConfig(models.Model):
    _inherit = 'pos.config'

    pui_enabled = fields.Boolean(string='Enable POS UI Designer', default=False)
    pui_profile_id = fields.Many2one('pos.ui.designer.profile', string='POS Design Profile', check_company=True, ondelete='set null')
    pui_payload = fields.Json(compute='_compute_pui_payload', string='POS Design Snapshot')

    @api.depends('pui_enabled', 'pui_profile_id', 'pui_profile_id.active',
                 'pui_profile_id.action_ids', 'pui_profile_id.action_ids.sequence',
                 'pui_profile_id.action_ids.visible', 'pui_profile_id.action_ids.code',
                 *['pui_profile_id.' + name for name in DESIGN_FIELDS])
    def _compute_pui_payload(self):
        for config in self:
            # Only whitelisted visual settings are exposed to POS users. The
            # profile model itself remains manager-only.
            profile = config.pui_profile_id.sudo()
            config.pui_payload = {'enabled': True, **profile._export_design()} if config.pui_enabled and profile.active else {'enabled': False}

    def _check_pui_permission(self, vals):
        if {'pui_enabled', 'pui_profile_id', 'pui_payload'} & vals.keys() and not self.env.su and not self.env.user.has_group('point_of_sale.group_pos_manager'):
            raise AccessError(_('Only POS managers can configure POS UI Designer.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_pui_permission(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._check_pui_permission(vals)
        return super().write(vals)

    @api.model
    def _load_pos_data_read(self, records, config):
        result = super()._load_pos_data_read(records, config)
        # pos.config is force-loaded by upstream Odoo 19. Rebuild the snapshot
        # once during bootstrap; no RPC, polling or bus reloads are introduced.
        for row in result:
            row['pui_payload'] = self.browse(row['id']).pui_payload
        return result


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_pui_enabled = fields.Boolean(related='pos_config_id.pui_enabled', readonly=False)
    pos_pui_profile_id = fields.Many2one(related='pos_config_id.pui_profile_id', readonly=False)
