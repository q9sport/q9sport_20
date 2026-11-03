# -*- coding: utf-8 -*-
from odoo import models, fields, _
from odoo.exceptions import UserError


class PurchaseOrderBarcodeWizard(models.TransientModel):
    _name = 'purchase.order.barcode.wizard'
    _description = 'Purchase Order Barcode Wizard'

    purchase_order_id = fields.Many2one(
        'purchase.order',
        string='Purchase Order',
        required=True,
    )

    line_ids = fields.One2many(
        'purchase.order.barcode.wizard.line',
        'wizard_id',
        string='Products',
    )

    def action_print_selected(self):
        self.ensure_one()

        selected_lines = self.line_ids.filtered(
            lambda line: line.selected and line.quantity > 0
        )

        if not selected_lines:
            raise UserError(
                _('Please select at least one product to print.')
            )

        products = selected_lines.mapped('product_id')

        # استدعاء نافذة إعدادات طباعة الليبل
        action = products.action_open_label_layout()

        return action


class PurchaseOrderBarcodeWizardLine(models.TransientModel):
    _name = 'purchase.order.barcode.wizard.line'
    _description = 'Purchase Order Barcode Wizard Line'

    wizard_id = fields.Many2one(
        'purchase.order.barcode.wizard',
        required=True,
        ondelete='cascade',
    )

    purchase_line_id = fields.Many2one(
        'purchase.order.line',
        string='Purchase Line',
    )

    product_id = fields.Many2one(
        'product.product',
        string='Product',
        required=True,
    )

    selected = fields.Boolean(
        string='Print',
        default=False,
    )

    quantity = fields.Integer(
        string='Labels Quantity',
        default=1,
    )
