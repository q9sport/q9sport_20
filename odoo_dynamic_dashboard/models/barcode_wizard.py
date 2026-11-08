# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError


# =========================================================
# PRODUCT DATA HELPERS
# =========================================================

def _get_product_field_val(product, fname, env, purchase_line=None):
    """
    Retrieve formatted product value.
    Supports Arabic name, barcode, QR, currency, category,
    purchase price and normal product fields.
    """

    if not product:
        return ''

    # Arabic Product Name
    if fname == 'arabic_name':
        if 'arabic_name' in product._fields:
            return product.arabic_name or ''

        return (
            product.with_context(lang='ar_001').name
            or product.with_context(lang='ar').name
            or ''
        )

    # QR Code
    elif fname == 'qr_code':
        return (
            product.barcode
            or product.default_code
            or str(product.id)
        )

    # Barcode
    elif fname == 'barcode':
        return (
            product.barcode
            or product.default_code
            or str(product.id)
        )

    # Currency
    elif fname == 'currency_id':
        currency = (
            purchase_line.order_id.currency_id
            if purchase_line and purchase_line.order_id
            else env.company.currency_id
        )
        return currency.name or ''

    elif fname == 'currency_symbol':
        currency = (
            purchase_line.order_id.currency_id
            if purchase_line and purchase_line.order_id
            else env.company.currency_id
        )
        return currency.symbol or ''

    # Purchase Price
    elif fname == 'price_unit':
        if purchase_line:
            return str(purchase_line.price_unit or 0.0)
        return '0.0'

    # Product Category
    elif fname == 'categ_id':
        return product.categ_id.display_name or ''

    # Sales Price
    elif fname == 'list_price':
        return str(product.list_price or 0.0)

    # Product Name
    elif fname == 'name':
        return product.name or ''

    # Product Type
    elif fname == 'type':
        if 'type' in product._fields:
            selection_dict = dict(
                product._fields['type']._description_selection(env)
            )
            return (
                selection_dict.get(product.type)
                or str(product.type or '')
            )

        return ''

    # Any other product field
    elif fname in product._fields:
        value = product[fname]

        if not value:
            return ''

        if hasattr(value, 'display_name'):
            return value.display_name or ''

        if hasattr(value, 'name'):
            return value.name or ''

        return str(value)

    return ''


# =========================================================
# PREPARE PURCHASE ORDER BARCODE DATA
# =========================================================

def _prepare_purchase_barcode_data(env, wizard):
    """
    Prepare barcode report data using purchase order lines.

    Each product is converted into report data containing:
    Arabic name, English name, barcode, QR, purchase price,
    currency, quantity and dynamic field values.
    """

    quantity_by_product = defaultdict(list)

    for wizard_line in wizard.line_ids.filtered(
        lambda line: line.selected
    ):

        product = wizard_line.product_id
        purchase_line = wizard_line.purchase_line_id

        if not product:
            continue

        # Barcode
        barcode_value = (
            wizard_line.barcode
            or product.barcode
            or product.default_code
            or str(product.id)
        )

        # Arabic Name
        if 'arabic_name' in product._fields:
            arabic_name = product.arabic_name or ''
        else:
            arabic_name = (
                product.with_context(lang='ar_001').name
                or product.with_context(lang='ar').name
                or ''
            )

        # English Name
        english_name = (
            product.with_context(lang='en_US').name
            or product.name
            or ''
        )

        # Purchase Price
        purchase_price = (
            purchase_line.price_unit
            if purchase_line
            else wizard_line.price_unit
        )

        # Currency
        currency = (
            wizard.purchase_order_id.currency_id
            if wizard.purchase_order_id
            else env.company.currency_id
        )

        # Quantity
        quantity = max(
            1,
            int(wizard_line.quantity or 1)
        )

        # Product Category
        category_name = (
            product.categ_id.display_name
            if product.categ_id
            else ''
        )

        # QR Code
        qr_value = (
            product.barcode
            or product.default_code
            or str(product.id)
        )

        # Dynamic Fields
        dynamic_fields = []

        dynamic_field_names = [
            'arabic_name',
            'name',
            'barcode',
            'qr_code',
            'categ_id',
            'price_unit',
            'currency_id',
            'currency_symbol',
        ]

        for field_name in dynamic_field_names:
            dynamic_fields.append({
                'fname': field_name,
                'val': _get_product_field_val(
                    product,
                    field_name,
                    env,
                    purchase_line=purchase_line,
                ),
            })

        # Report Data
        item_data = {
            'product_id': product.id,

            'barcode': barcode_value,
            'qr_code': qr_value,

            'arabic_name': arabic_name,
            'product_name': english_name,

            'categ_name': category_name,

            'price': str(purchase_price or 0.0),

            'currency_id': currency.name or '',
            'currency_symbol': currency.symbol or '',

            'qty': quantity,

            'field_lines': dynamic_fields,

            'purchase_line_id': purchase_line.id
            if purchase_line else False,
        }

        quantity_by_product[product].append(item_data)

    return quantity_by_product


# =========================================================
# PURCHASE ORDER BARCODE WIZARD
# =========================================================

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
            ('code128', 'Code128'),
            ('qr', 'QR Code'),
        ],
        string='Barcode Type',
        default='code128',
        required=True,
    )

    label_format = fields.Selection(
        [
            ('dynamic', 'Dynamic Card 50x25mm'),
            ('dymo', 'Dymo'),
            ('2x7', '2 x 7'),
            ('4x7', '4 x 7'),
            ('4x12', '4 x 12'),
            ('zpl', 'ZPL'),
        ],
        string='Label Format',
        default='dynamic',
        required=True,
    )

    line_ids = fields.One2many(
        'purchase.order.barcode.wizard.line',
        'wizard_id',
        string='Products',
    )

    # =====================================================
    # PRINT SELECTED PRODUCTS
    # =====================================================

    def action_print_selected(self):
        return self.action_print_barcodes()

    def action_print_barcodes(self):
        self.ensure_one()

        selected_lines = self.line_ids.filtered(
            lambda line: line.selected
        )

        if not selected_lines:
            raise UserError(
                _('Please select at least one product.')
            )

        for line in selected_lines:

            if not line.product_id:
                raise UserError(
                    _('A selected line has no product.')
                )

            if line.quantity <= 0:
                raise UserError(
                    _('Product quantity must be greater than zero.')
                )

            if not (
                line.barcode
                or line.product_id.barcode
                or line.product_id.default_code
            ):
                raise UserError(
                    _(
                        'Product %s has no barcode or internal reference.'
                    ) % line.product_id.display_name
                )

        if self.label_format == 'zpl':
            raise UserError(
                _('ZPL printing is not supported by this PDF report.')
            )

        report_xmlid = (
            'odoo_dynamic_dashboard.action_purchase_order_barcodes_dynamic'
            if self.label_format == 'dynamic'
            else 'odoo_dynamic_dashboard.action_purchase_order_barcodes'
        )

        report = self.env.ref(
            report_xmlid,
            raise_if_not_found=False,
        )

        if not report:
            raise UserError(
                _('Barcode report was not found.')
            )

        return report.report_action(self)


# =========================================================
# PURCHASE ORDER BARCODE WIZARD LINE
# =========================================================

class PurchaseOrderBarcodeWizardLine(models.TransientModel):
    _name = 'purchase.order.barcode.wizard.line'
    _description = 'Purchase Order Barcode Wizard Line'

    wizard_id = fields.Many2one(
        'purchase.order.barcode.wizard',
        string='Wizard',
        required=True,
        ondelete='cascade',
    )

    selected = fields.Boolean(
        string='Select',
        default=True,
    )

    product_id = fields.Many2one(
        'product.product',
        string='Product',
        required=True,
    )

    purchase_line_id = fields.Many2one(
        'purchase.order.line',
        string='Purchase Order Line',
    )

    price_unit = fields.Float(
        string='Purchase Price',
        related='purchase_line_id.price_unit',
        readonly=True,
    )

    barcode = fields.Char(
        string='Barcode',
    )

    quantity = fields.Integer(
        string='Copies',
        default=1,
    )


# =========================================================
# PURCHASE ORDER BARCODE REPORT
# =========================================================

class PurchaseOrderBarcodeReport(models.AbstractModel):
    _name = 'report.odoo_dynamic_dashboard.purchase_order_barcode_template'
    _description = 'Purchase Order Barcode Report'

    @api.model
    def _get_report_values(self, docids, data=None):

        data = data or {}

        wizards = self.env[
            'purchase.order.barcode.wizard'
        ].browse(docids).exists()

        if not wizards:
            raise UserError(
                _('No barcode wizard was found.')
            )

        wizard = wizards[0]

        quantity_by_product = _prepare_purchase_barcode_data(
            self.env,
            wizard,
        )

        # Flatten products into labels.
        labels = []

        for product, item_list in quantity_by_product.items():

            for item_data in item_list:

                for copy_number in range(
                    int(item_data.get('qty', 1))
                ):
                    labels.append({
                        'product': product,
                        'item_data': item_data,
                        'copy_number': copy_number + 1,
                    })

        return {
            'doc_ids': wizard.ids,
            'doc_model': 'purchase.order.barcode.wizard',
            'docs': wizard,

            'wizard': wizard,

            'quantity': quantity_by_product,
            'labels': labels,

            'label_count': len(labels),

            'get_product_field_val': (
                lambda product, fname:
                _get_product_field_val(
                    product,
                    fname,
                    self.env,
                )
            ),
        }
