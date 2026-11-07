# -*- coding: utf-8 -*-
#############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2026-TODAY Cybrosys Technologies
#    Author: Cybrosys Techno Solutions
#
#    GNU LESSER GENERAL PUBLIC LICENSE (LGPL v3)
#
#############################################################################

from collections import defaultdict
from urllib.parse import quote

from odoo import models


def _get_product_field_val(product, fname, env):
    """Retrieve formatted value of a field for the given product."""

    if fname == 'arabic_name':
        if 'arabic_name' in product._fields:
            return product.arabic_name or ''
        return product.with_context(lang='ar_001').name or ''

    elif fname == 'qr_code':
        return (
            product.barcode
            or product.default_code
            or str(product.id)
        )

    elif fname == 'currency_id':
        return env.company.currency_id.name or ''

    elif fname == 'currency_symbol':
        return env.company.currency_id.symbol or ''

    elif fname == 'categ_id':
        return product.categ_id.display_name or ''

    elif fname == 'list_price':
        return str(product.list_price or 0.0)

    elif fname == 'name':
        return product.name or ''

    elif fname == 'type':
        if 'type' in product._fields:
            selection_dict = dict(
                product._fields['type']._description_selection(env)
            )
            return selection_dict.get(product.type) or str(
                product.type or ''
            )
        return str(product.type or '')

    elif fname in product._fields:
        val = product[fname]

        if hasattr(val, 'display_name'):
            return val.display_name or ''

        elif hasattr(val, 'name'):
            return val.name or ''

        return (
            str(val)
            if val is not False and val is not None
            else ''
        )

    return ''


def _get_qr_url(product, env, price=None):
    """Generate a QR image URL using Odoo's native barcode controller."""

    english_name = product.name or ''

    arabic_name = _get_product_field_val(
        product, 'arabic_name', env
    )

    price_value = (
        product.list_price if price is None else price
    )

    try:
        price_value = float(price_value or 0.0)
    except (ValueError, TypeError):
        price_value = 0.0

    company = product.company_id or env.company
    company_name = company.name or ''

    qr_data = (
        'Product: %s | Arabic: %s | Price: %.2f QAR (ر.ق) | Company: %s'
        % (
            english_name,
            arabic_name,
            price_value,
            company_name,
        )
    )

    encoded_data = quote(qr_data, safe='')

    return (
        '/report/barcode/'
        '?barcode_type=QR'
        '&value=%s'
        '&width=160'
        '&height=160'
        '&quiet=0'
    ) % encoded_data


def _prepare_datas(env, data):
    """Prepare product data for the dynamic label report."""

    active_model = (
        data.get('active_model')
        or env.context.get('active_model')
        or 'product.product'
    )

    if active_model == 'product.template':
        Product = env['product.template'].with_context(
            display_default_code=False
        )
    else:
        Product = env['product.product'].with_context(
            display_default_code=False
        )

    template_id = data.get('layout_wizard')

    template = (
        env['dynamic.template'].browse(template_id).exists()
        if template_id else False
    )

    fields_def = (
        template.dynamic_field_ids
        if template and template.dynamic_field_ids
        else []
    )

    cat_line = {
        'size': '14',
        'color': 'red',
    }

    price_line = {
        'size': '16',
        'color': 'green',
    }

    name_line = {
        'size': '14',
        'color': 'blue',
    }

    field_lines = []

    for fdef in fields_def:

        fname = (
            fdef.fd_name_id.name
            if fdef.fd_name_id else False
        )

        if not fname:
            continue

        c_val = (fdef.color or '').strip()
        s_val = fdef.size or '14'

        if fname == 'categ_id':
            cat_line = {
                'size': s_val,
                'color': c_val or 'red',
            }

        elif fname == 'list_price':
            price_line = {
                'size': s_val,
                'color': c_val or 'green',
            }

        elif fname == 'name':
            name_line = {
                'size': s_val,
                'color': c_val or 'blue',
            }

        field_lines.append({
            'fname': fname,
            'size': s_val,
            'color': c_val or '#000000',
        })

    quantity_by_product = defaultdict(list)

    # -----------------------------------------------------
    # NORMAL PRODUCTS
    # -----------------------------------------------------

    for product_qty, qnty in (
        data.get('quantity_by_product') or {}
    ).items():

        product = Product.browse(int(product_qty)).exists()

        if not product:
            continue

        barcode_val = (
            product.barcode
            or product.default_code
            or product.name
            or ''
        )

        prod_field_lines = []

        for fl in field_lines:

            val = _get_product_field_val(
                product, fl['fname'], env
            )

            prod_field_lines.append({
                'fname': fl['fname'],
                'val': val,
                'size': fl['size'],
                'color': fl['color'],
            })

        categ_name = (
            product.categ_id.display_name
            or product.categ_id.name
            or ''
        )

        price_value = product.list_price or 0.0

        quantity_by_product[product].append({

            'barcode': barcode_val,
            'qty': qnty,
            'product_name': product.name or '',

            'arabic_name': _get_product_field_val(
                product, 'arabic_name', env
            ),

            'categ_name': categ_name,
            'price': str(price_value),

            'qr_url': _get_qr_url(
                product, env, price_value
            ),

            'cat_line': cat_line,
            'price_line': price_line,
            'name_line': name_line,
            'field_lines': prod_field_lines,

        })

    # -----------------------------------------------------
    # CUSTOM BARCODES
    # -----------------------------------------------------

    if data.get('custom_barcodes'):

        for product_id, barcodes_qtys in (
            data.get('custom_barcodes') or {}
        ).items():

            for b_code, qty_val in barcodes_qtys:

                p_rec = Product.browse(
                    int(product_id)
                ).exists()

                if not p_rec:
                    continue

                prod_field_lines = []

                for fl in field_lines:

                    val = _get_product_field_val(
                        p_rec, fl['fname'], env
                    )

                    prod_field_lines.append({
                        'fname': fl['fname'],
                        'val': val,
                        'size': fl['size'],
                        'color': fl['color'],
                    })

                p_categ_name = (
                    p_rec.categ_id.display_name
                    or p_rec.categ_id.name
                    or ''
                )

                price_value = p_rec.list_price or 0.0

                quantity_by_product[p_rec].append({

                    'barcode': b_code,
                    'qty': qty_val,
                    'product_name': p_rec.name or '',

                    'arabic_name': _get_product_field_val(
                        p_rec, 'arabic_name', env
                    ),

                    'categ_name': p_categ_name,
                    'price': str(price_value),

                    'qr_url': _get_qr_url(
                        p_rec, env, price_value
                    ),

                    'cat_line': cat_line,
                    'price_line': price_line,
                    'name_line': name_line,
                    'field_lines': prod_field_lines,

                })

    data.update({
        'quantity': quantity_by_product,
    })

    return data


class ReportProductTemplateLabelDynamic(models.AbstractModel):
    """Product Dynamic Report."""

    _name = 'report.dynamic_product_label_print.report_dynamic'
    _description = 'Product Dynamic Report'

    def _get_report_values(self, docids, data=None):
        """Return prepared product data to QWeb."""

        data = data or {}

        return _prepare_datas(self.env, data)
