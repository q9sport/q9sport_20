# -*- coding: utf-8 -*-
from odoo import models, _
from odoo.exceptions import UserError


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    def action_open_barcode_wizard(self):
        self.ensure_one()

        lines = self.order_line.filtered(
            lambda line: not line.display_type and line.product_id
        )

        if not lines:
            raise UserError(
                _('There are no products in this Purchase Order.')
            )

        wizard = self.env['purchase.order.barcode.wizard'].create({
            'purchase_order_id': self.id,
            'line_ids': [
                (0, 0, {
                    'product_id': line.product_id.id,
                    'purchase_line_id': line.id,
                    'selected': False,
                    # 'quantity': 1,
                })
                for line in lines
            ],
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('Select Products for Barcode Printing'),
            'res_model': 'purchase.order.barcode.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
            'target': 'new',
        }
