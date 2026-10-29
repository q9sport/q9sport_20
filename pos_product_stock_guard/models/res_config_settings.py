# -*- coding: utf-8 -*-

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_stock_guard_show = fields.Boolean(related='pos_config_id.stock_guard_show', readonly=False)
    pos_stock_guard_qty_type = fields.Selection(related='pos_config_id.stock_guard_qty_type', readonly=False)
    pos_stock_guard_location_id = fields.Many2one(related='pos_config_id.stock_guard_location_id', readonly=False)
    pos_stock_guard_mode = fields.Selection(related='pos_config_id.stock_guard_mode', readonly=False)
    pos_stock_guard_override_group_id = fields.Many2one(related='pos_config_id.stock_guard_override_group_id', readonly=False)
