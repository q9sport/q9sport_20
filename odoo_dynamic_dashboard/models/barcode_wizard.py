# -*- coding: utf-8 -*-

import base64

from odoo import fields, models, _
from odoo.exceptions import UserError


class PurchaseOrderBarcodeWizard(models.TransientModel):
    _name = 'purchase.order.barcode.wizard'
    _description = 'Purchase Order Barcode Wizard'

    purchase_order_id = fields.Many2one(
        'purchase.order',
        string='Purchase Order',
        readonly=True,
    )

    barcode_type = fields.Selection(
        [
            ('code128', 'Barcode 1D'),
            ('qr', 'QR Code'),
        ],
        string='Type',
        default='code128',
        required=True,
    )

    copies = fields.Integer(
        string='Copies',
        default=1,
        required=True,
    )

    label_format = fields.Selection(
        [
            ('dynamic', 'Dynamic Card (63 x 46 mm)'),
            ('dymo', 'Dymo'),
            ('2x7', '2 x 7'),
            ('4x7', '4 x 7'),
            ('4x12', '4 x 12'),
            ('zpl', 'ZPL Labels'),
        ],
        string='Format',
        default='dynamic',
        required=True,
    )

    line_ids = fields.One2many(
        'purchase.order.barcode.wizard.line',
        'wizard_id',
        string='Products',
    )

    # =====================================================
    # PRINT SELECTED
    # =====================================================

    def action_print_selected(self):
        self.ensure_one()
        return self.action_print_barcodes()

    # =====================================================
    # PRINT BARCODES
    # =====================================================

    def action_print_barcodes(self):
        self.ensure_one()

        selected_lines = self.line_ids.filtered(
            lambda line: line.selected
        )

        if not selected_lines:
            raise UserError(
                _('Please select at least one product.')
            )

        if self.copies <= 0:
            raise UserError(
                _('Copies must be greater than zero.')
            )

        if any(line.quantity <= 0 for line in selected_lines):
            raise UserError(
                _('Barcode quantity must be greater than zero.')
            )

        if any(not line.product_id for line in selected_lines):
            raise UserError(
                _('Some selected lines have no product.')
            )

        if any(
            not line.product_id.barcode
            for line in selected_lines
        ):
            raise UserError(
                _('Some selected products do not have a barcode.')
            )

        if self.label_format == 'zpl':
            raise UserError(_(
                'ZPL printing requires a separate Zebra printer report. '
                'Please select a PDF format for now.'
            ))

        report = self.env.ref(
            'odoo_dynamic_dashboard.action_purchase_order_barcodes'
        )

        return report.report_action(self)


# =========================================================
# WIZARD LINES
# =========================================================

class PurchaseOrderBarcodeWizardLine(models.TransientModel):
    _name = 'purchase.order.barcode.wizard.line'
    _description = 'Purchase Order Barcode Wizard Line'

    wizard_id = fields.Many2one(
        'purchase.order.barcode.wizard',
        required=True,
        ondelete='cascade',
    )

    selected = fields.Boolean(
        string='Select',
        default=False,
    )

    product_id = fields.Many2one(
        'product.product',
        string='Product',
        readonly=True,
    )

    purchase_line_id = fields.Many2one(
        'purchase.order.line',
        string='Purchase Order Line',
        readonly=True,
        ondelete='set null',
    )

    price_unit = fields.Float(
        string='Unit Price',
        related='purchase_line_id.price_unit',
        readonly=True,
    )

    barcode = fields.Char(
        related='product_id.barcode',
        string='Barcode',
        readonly=True,
    )

    quantity = fields.Integer(
        string='Labels',
        default=1,
    )


# =========================================================
# BARCODE REPORT
# =========================================================

class PurchaseOrderBarcodeReport(models.AbstractModel):
    _name = 'report.odoo_dynamic_dashboard.purchase_order_barcode_template'
    _description = 'Purchase Order Barcode Report'

    # =====================================================
    # GENERATE BARCODE IMAGE
    # =====================================================

    def _generate_barcode_image(
        self,
        barcode,
        barcode_type,
        product,
    ):
        """Generate barcode image directly as Base64."""

        try:
            if barcode_type == 'qr':
                image = self.env['ir.actions.report'].barcode(
                    'QR',
                    barcode,
                    width=250,
                    height=250,
                    humanreadable=0,
                    quiet=1,
                )
            else:
                image = self.env['ir.actions.report'].barcode(
                    'Code128',
                    barcode,
                    width=450,
                    height=120,
                    humanreadable=0,
                    quiet=1,
                )

            return base64.b64encode(image).decode('ascii')

        except Exception as error:
            raise UserError(_(
                'Cannot generate barcode for product: %s\nError: %s'
            ) % (product.display_name, str(error)))

    # =====================================================
    # PRODUCT NAMES
    # =====================================================

    def _get_product_names(self, product):
        """Get Arabic and English product names."""

        template = product.product_tmpl_id

        arabic_name = (
            getattr(template, 'arabic_name', False)
            or template.with_context(lang='ar_001').name
            or ''
        )

        english_name = (
            template.with_context(lang='en_US').name
            or template.name
            or product.display_name
            or ''
        )

        return arabic_name, english_name

    # =====================================================
    # PRICE AND CURRENCY
    # =====================================================

    def _get_line_price_currency(self, line, wizard):
        """Get purchase line price and currency."""

        purchase_line = line.purchase_line_id

        if purchase_line:
            price = purchase_line.price_unit

            currency = (
                purchase_line.currency_id
                or wizard.purchase_order_id.currency_id
            )
        else:
            price = line.price_unit or 0.0
            currency = wizard.purchase_order_id.currency_id

        return price or 0.0, currency

    # =====================================================
    # REPORT VALUES
    # =====================================================

    def _get_report_values(self, docids, data=None):

        docs = self.env[
            'purchase.order.barcode.wizard'
        ].browse(docids).exists()

        if not docs:
            raise UserError(
                _('The barcode wizard no longer exists.')
            )

        format_settings = {
            'dynamic': {
                'columns': 3,
                'capacity': 9,
            },
            'dymo': {
                'columns': 1,
                'capacity': 7,
            },
            '2x7': {
                'columns': 2,
                'capacity': 14,
            },
            '4x7': {
                'columns': 4,
                'capacity': 28,
            },
            '4x12': {
                'columns': 4,
                'capacity': 48,
            },
        }

        report_data = []

        for wizard in docs:

            settings = format_settings.get(
                wizard.label_format,
                format_settings['dynamic'],
            )

            columns = settings['columns']
            capacity = settings['capacity']

            labels = []

            selected_lines = wizard.line_ids.filtered(
                lambda line: line.selected
            )

            for line in selected_lines:

                product = line.product_id
                barcode = product.barcode or ''

                if not barcode:
                    raise UserError(_(
                        'Product %s does not have a barcode.'
                    ) % product.display_name)

                # Product names
                arabic_name, english_name = (
                    self._get_product_names(product)
                )

                # Barcode image
                barcode_base64 = self._generate_barcode_image(
                    barcode,
                    wizard.barcode_type,
                    product,
                )

                # Purchase price and currency
                price, currency = self._get_line_price_currency(
                    line,
                    wizard,
                )

                # Total labels
                total_copies = line.quantity * wizard.copies

                for copy_index in range(total_copies):

                    labels.append({
                        'product': product,
                        'barcode': barcode,
                        'barcode_base64': barcode_base64,
                        'barcode_type': wizard.barcode_type,

                        'arabic_name': arabic_name,
                        'english_name': english_name,

                        'price': price,
                        'currency': currency,
                    })

            # Split labels into pages and rows
            pages = []

            for page_start in range(0, len(labels), capacity):

                page_labels = labels[
                    page_start:page_start + capacity
                ]

                rows = []

                for row_start in range(
                    0,
                    len(page_labels),
                    columns,
                ):

                    rows.append(
                        page_labels[
                            row_start:row_start + columns
                        ]
                    )

                pages.append(rows)

            report_data.append({
                'wizard': wizard,
                'pages': pages,
                'label_count': len(labels),
                'columns': columns,
            })

        return {
            'doc_ids': docs.ids,
            'doc_model': 'purchase.order.barcode.wizard',
            'docs': docs,
            'report_data': report_data,
        }
