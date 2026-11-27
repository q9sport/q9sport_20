# -*- coding: utf-8 -*-
{
    "name": "Q9SPORT Premium POS UI",
    "summary": "TIVA-inspired vertical POS layout for Odoo 20",
    "version": "20.0.1.2.0",
    "category": "Sales/Point of Sale",
    "author": "Q9SPORT",
    "website": "https://q9sport.com",
    "license": "LGPL-3",
    "depends": ["point_of_sale"],
    "assets": {
        "point_of_sale._assets_pos": [
            "pos_q9sport_premium_ui/static/src/scss/premium_pos.scss",
        ],
    },
    "installable": True,
    "application": False,
}
