
# -*- coding: utf-8 -*-

import io
import os

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

from odoo import http
from odoo.http import request


class ArabicNameImage(http.Controller):

    @http.route(
        '/arabic_name_image/<int:product_tmpl_id>',
        type='http',
        auth='user',
        methods=['GET'],
    )
    def arabic_name_image(self, product_tmpl_id, **kwargs):

        product = request.env['product.template'].browse(
            product_tmpl_id
        ).exists()

        if not product:
            return request.not_found()

        arabic_name = product.arabic_name or ''

        if not arabic_name.strip():
            return request.not_found()

        # Arabic text shaping
        reshaper = arabic_reshaper.ArabicReshaper({
            'delete_harakat': False,
            'support_ligatures': True,
            'use_unshaped_instead_of_isolated': False,
        })

        reshaped_text = reshaper.reshape(arabic_name)

        # Right-to-left direction
        display_text = get_display(
            reshaped_text,
            base_dir='R',
        )

        # Get font path without Odoo internal functions
        current_dir = os.path.dirname(os.path.abspath(__file__))

        font_path = os.path.normpath(
            os.path.join(
                current_dir,
                '..',
                'static',
                'src',
                'fonts',
                'DejaVuSans.ttf',
            )
        )

        if not os.path.isfile(font_path):
            return request.make_response(
                'Arabic font file not found: %s' % font_path,
                headers=[('Content-Type', 'text/plain')],
                status=500,
            )

        # Load font
        font = ImageFont.truetype(
            font_path,
            32,
            layout_engine=ImageFont.Layout.BASIC,
        )

        # Measure text
        temp_image = Image.new(
            'RGBA',
            (10, 10),
            (255, 255, 255, 0),
        )

        draw = ImageDraw.Draw(temp_image)

        bbox = draw.textbbox(
            (0, 0),
            display_text,
            font=font,
        )

        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        padding = 10

        image = Image.new(
            'RGBA',
            (
                max(text_width + padding * 2, 1),
                max(text_height + padding * 2, 1),
            ),
            (255, 255, 255, 0),
        )

        draw = ImageDraw.Draw(image)

        draw.text(
            (padding - bbox[0], padding - bbox[1]),
            display_text,
            font=font,
            fill=(0, 0, 0, 255),
        )

        # Generate PNG
        output = io.BytesIO()

        image.save(
            output,
            format='PNG',
            optimize=True,
        )

        return request.make_response(
            output.getvalue(),
            headers=[
                ('Content-Type', 'image/png'),
                ('Cache-Control', 'no-store, no-cache, must-revalidate'),
            ],
        )
