# -*- coding: utf-8 -*-
{
    "name": "POS Split Payment & Qatar Delivery Customer",
    "version": "20.0.13.0.0",
    "category": "Sales/Point of Sale",
    "summary": "Compact split payments and inline Qatar customer lookup and delivery details",
    "description": "Manual split payment inputs, phone-based customer lookup, Qatar address numbers, Arabic GPS place name and QAR receivable validation.",
    "author": "Custom POS Extension",
    "license": "LGPL-3",
    "depends": ["point_of_sale", "account"],
    "data": [],
    "assets": {
        "point_of_sale._assets_pos": [
            "pos_split_textbox_20/static/src/js/split_payment.js",
            "pos_split_textbox_20/static/src/js/qatar_customer.js",
            "pos_split_textbox_20/static/src/xml/split_payment.xml",
            "pos_split_textbox_20/static/src/xml/qatar_customer.xml",
            "pos_split_textbox_20/static/src/xml/ticket_refund_icon.xml",
            "pos_split_textbox_20/static/src/scss/split_payment.scss",
        ],
    },
    "installable": True,
    "application": False,
}
