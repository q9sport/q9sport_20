import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


POSITIONS = [('left', 'Left'), ('right', 'Right'), ('top', 'Top'), ('bottom', 'Bottom')]
ACTION_CODES = [
    ('internal_note', 'Internal Note'), ('preset', 'Preset'),
    ('customer_note', 'Customer Note'),
    ('pricelist', 'Pricelist'), ('tax', 'Fiscal Position / Tax'),
    ('info', 'Product Information'),
]
PRESETS = {
    'ocean': ('#2563EB', '#0891B2'),
    'emerald': ('#047857', '#0E7490'),
    'sunset': ('#C2410C', '#BE185D'),
    'purple': ('#7C3AED', '#BE185D'),
    'neutral': ('#475569', '#0F766E'),
}
RANGES = {
    'cart_width': (25, 55), 'button_height': (44, 80),
    'keypad_height': (44, 76), 'font_size': (14, 22),
    'product_font_size': (14, 24), 'product_min_width': (110, 240),
    'spacing': (4, 20), 'keypad_spacing': (2, 12),
    'radius': (0, 24), 'shadow': (0, 3),
}
COLOR_FIELDS = (
    'primary_color', 'accent_color', 'background_color', 'surface_color',
    'text_color', 'success_color', 'warning_color', 'danger_color',
)
DESIGN_FIELDS = (
    'sales_keypad_position', 'sales_actions_position', 'sales_first_panel',
    'cart_side', 'cart_width', 'action_direction', 'density',
    'payment_keypad_position', 'payment_methods_position', 'payment_first_panel',
    'payment_summary_position', 'payment_inherit', 'payment_primary_color',
    'payment_accent_color', 'sticky_payment', 'preset', 'appearance',
    'button_height', 'keypad_height', 'font_size', 'product_font_size',
    'product_min_width', 'spacing', 'keypad_spacing', 'radius', 'shadow',
    'text_only', *COLOR_FIELDS,
)


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    values = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return sum(a * b for a, b in zip(values, (0.2126, 0.7152, 0.0722)))


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class PosUIDesignerProfile(models.Model):
    _name = 'pos.ui.designer.profile'
    _description = 'POS UI Design Profile'
    _check_company_auto = True
    _order = 'name, id'

    name = fields.Char(required=True, default='Ocean POS')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    config_ids = fields.One2many('pos.config', 'pui_profile_id', string='Assigned POS')
    action_ids = fields.One2many('pos.ui.designer.action', 'profile_id', string='Optional Actions', copy=True)
    sales_keypad_position = fields.Selection(POSITIONS, required=True, default='right')
    sales_actions_position = fields.Selection(POSITIONS, required=True, default='top')
    sales_first_panel = fields.Selection([('keypad', 'Keypad first'), ('actions', 'Actions first')], required=True, default='keypad')
    cart_side = fields.Selection([('left', 'Left'), ('right', 'Right')], required=True, default='left')
    cart_width = fields.Integer(default=36, required=True, string='Cart Share (%)')
    action_direction = fields.Selection([('horizontal', 'Horizontal'), ('vertical', 'Vertical')], required=True, default='horizontal')
    density = fields.Selection([('compact', 'Compact'), ('comfortable', 'Comfortable'), ('touch', 'Large Touch')], required=True, default='comfortable')
    payment_keypad_position = fields.Selection(POSITIONS, required=True, default='left')
    payment_methods_position = fields.Selection(POSITIONS, required=True, default='top')
    payment_first_panel = fields.Selection([('keypad', 'Keypad first'), ('methods', 'Payment methods first')], required=True, default='methods')
    payment_summary_position = fields.Selection(POSITIONS, required=True, default='right')
    payment_inherit = fields.Boolean(default=True, string='Use Sales Accent Colors')
    payment_primary_color = fields.Char(required=True, default='#2563EB')
    payment_accent_color = fields.Char(required=True, default='#0891B2')
    sticky_payment = fields.Boolean(default=True, string='Sticky Payment Controls')
    preset = fields.Selection([(k, v) for k, v in [('ocean', 'Ocean Blue'), ('emerald', 'Emerald'), ('sunset', 'Sunset'), ('purple', 'Purple'), ('neutral', 'Neutral')]], required=True, default='ocean')
    appearance = fields.Selection([('light', 'Light'), ('dark', 'Dark')], required=True, default='light')
    primary_color = fields.Char(required=True, default='#2563EB')
    accent_color = fields.Char(required=True, default='#0891B2')
    background_color = fields.Char(required=True, default='#EEF2F7')
    surface_color = fields.Char(required=True, default='#FFFFFF')
    text_color = fields.Char(required=True, default='#172033')
    success_color = fields.Char(required=True, default='#047857')
    warning_color = fields.Char(required=True, default='#B45309')
    danger_color = fields.Char(required=True, default='#B91C1C')
    button_height = fields.Integer(required=True, default=48)
    keypad_height = fields.Integer(required=True, default=48)
    font_size = fields.Integer(required=True, default=16)
    product_font_size = fields.Integer(required=True, default=16)
    product_min_width = fields.Integer(required=True, default=145)
    spacing = fields.Integer(required=True, default=10)
    keypad_spacing = fields.Integer(required=True, default=6)
    radius = fields.Integer(required=True, default=12)
    shadow = fields.Integer(required=True, default=1)
    text_only = fields.Boolean(default=True, string='Text-only Products / Categories')

    @api.model
    def _palette_vals(self, vals):
        result = dict(vals)
        if vals.get('preset') in PRESETS:
            for key, value in zip(('primary_color', 'accent_color'), PRESETS[vals['preset']]):
                result.setdefault(key, value)
        if 'appearance' in vals:
            colors = ('#111827', '#1F2937', '#F3F4F6') if vals['appearance'] == 'dark' else ('#EEF2F7', '#FFFFFF', '#172033')
            for key, value in zip(('background_color', 'surface_color', 'text_color'), colors):
                result.setdefault(key, value)
        return result

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for vals in vals_list:
            vals = self._palette_vals(vals)
            # The form client explicitly submits an empty one2many list for a
            # new profile. Treat it like an omitted value so every profile
            # starts with the supported Odoo 19 action controls.
            if not vals.get('action_ids'):
                vals['action_ids'] = [fields.Command.create({'code': code, 'sequence': i * 10}) for i, (code, _) in enumerate(ACTION_CODES)]
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        return super().write(self._palette_vals(vals))

    @api.onchange('preset')
    def _onchange_preset(self):
        if self.preset in PRESETS:
            self.primary_color, self.accent_color = PRESETS[self.preset]

    @api.onchange('appearance')
    def _onchange_appearance(self):
        self.update({key: value for key, value in self._palette_vals({'appearance': self.appearance}).items() if key != 'appearance'})

    @api.constrains(*RANGES)
    def _check_ranges(self):
        for profile in self:
            for name, (low, high) in RANGES.items():
                if not low <= profile[name] <= high:
                    raise ValidationError(_('%(field)s must be between %(low)s and %(high)s.', field=profile._fields[name].string, low=low, high=high))

    @api.constrains(*COLOR_FIELDS, 'payment_primary_color', 'payment_accent_color')
    def _check_colors(self):
        for profile in self:
            for name in (*COLOR_FIELDS, 'payment_primary_color', 'payment_accent_color'):
                if not re.fullmatch(r'#[0-9A-Fa-f]{6}', profile[name] or ''):
                    raise ValidationError(_('Colors must use the format #RRGGBB.'))
            if any(contrast(profile.text_color, bg) < 4.5 for bg in (profile.surface_color, profile.background_color)):
                raise ValidationError(_('Text must have at least 4.5:1 contrast against both the surface and background.'))

    @api.constrains('company_id', 'config_ids')
    def _check_assigned_company(self):
        for profile in self:
            if any(config.company_id != profile.company_id for config in profile.sudo().with_context(active_test=False).config_ids):
                raise ValidationError(_('A design profile and its assigned POS must belong to the same company.'))

    def _export_design(self):
        self.ensure_one()
        return {**{name: self[name] for name in DESIGN_FIELDS}, 'actions': {line.code: {'visible': line.visible, 'sequence': line.sequence} for line in self.action_ids}}

    def action_reset_defaults(self):
        self.check_access('write')
        defaults = self.default_get(list(DESIGN_FIELDS))
        for profile in self:
            profile.write({**defaults, 'action_ids': [fields.Command.clear(), *[fields.Command.create({'code': code, 'sequence': i * 10}) for i, (code, _) in enumerate(ACTION_CODES)]]})
        return True

    def action_duplicate(self):
        self.ensure_one()
        duplicate = self.copy({'name': _('%s (copy)', self.name), 'config_ids': []})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': duplicate.id, 'view_mode': 'form', 'target': 'current'}


class PosUIDesignerAction(models.Model):
    _name = 'pos.ui.designer.action'
    _description = 'POS Optional Action Appearance'
    _order = 'sequence, id'
    _check_company_auto = True

    profile_id = fields.Many2one('pos.ui.designer.profile', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='profile_id.company_id', store=True)
    code = fields.Selection(ACTION_CODES, required=True)
    sequence = fields.Integer(default=10)
    visible = fields.Boolean(default=True)

    @api.constrains('profile_id', 'code', 'sequence')
    def _check_action(self):
        for line in self:
            if not 0 <= line.sequence <= 999:
                raise ValidationError(_('Action sequence must be between 0 and 999.'))
            if self.search_count([('profile_id', '=', line.profile_id.id), ('code', '=', line.code)]) > 1:
                raise ValidationError(_('Each optional action may appear only once per profile.'))
