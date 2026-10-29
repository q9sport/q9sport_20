# -*- coding: utf-8 -*-
{
    'name': 'POS Stock Display & Block',
    'version': '20.0.1.0.0',
    'category': 'Sales/Point of Sale',
    'summary': """Show the available stock on POS product cards and warn or block when a cashier sells more than is in stock
    POS Stock Display, POS Show Stock, POS Product Stock Badge, POS On Hand Quantity, POS Forecasted Quantity,
    POS Free to Use Quantity, POS Restrict Out of Stock, POS Block Out of Stock, POS Out of Stock Warning,
    POS Stock Location, POS Manager Override, Display Stock in POS, Restrict Out-of-Stock Products in POS
    --------
    POS Stock in Odoo, POS Stock - Display On Hand or Forecasted, Point Of Sale Manage Out of Stock Product,
    Point Of Sale Restrict Out Of Stock, POS Stock Sync, Point Of Sale Minimum Quantity, POS Product Quantity Limit,
    All in One POS Features in Odoo
    Odoo 20 Community and Enterprise
    """,
    'description': """
POS Stock Display & Block
=========================
Cashiers can't see in the register how many units are left, so they sell items the shop doesn't have and
the stock goes negative. This module shows the available quantity on every storable product card and,
per point of sale, warns or blocks when an order asks for more than is in stock.

Features
--------
* Stock badge on product cards: green when in stock, red at zero or below
* Quantity Shown per point of sale: On Hand, Free to Use or Forecasted
* Stock Location per point of sale, default the source location of the Operation Type (sub-locations included)
* Out-of-Stock Products: Allow, Warn (confirmation dialog) or Block
* Block also stops raising a line quantity with the numpad above what is left
* Can Sell Out of Stock: users of a chosen group (default Point of Sale / Administrator) confirm instead of being blocked
* The register lowers the badge as lines are added, across all open orders
* Services, consumables and combos are never counted or blocked
    """,
    'author': 'Awins',
    'support': 'odoo.expert.111@gmail.com',
    'depends': [
        'pos_stock',
    ],
    'data': [
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_product_stock_guard/static/src/app/**/*',
        ],
        'web.assets_tests': [
            'pos_product_stock_guard/static/tests/tours/**/*',
        ],
    },
    'images': [
        'static/description/banner.png',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
