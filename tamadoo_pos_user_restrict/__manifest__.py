{
    'name': 'POS Shop Access per User - Restrict Point of Sale by User',
    'version': '20.0.1.0.0',
    'category': 'Sales/Point of Sale',
    'summary': 'Restrict Point of Sale access per user: each cashier sees and opens only the POS shops you allow. Administrators keep every shop.',
    'description': """
POS Shop Access per User
========================

By default every Point of Sale user sees every shop on the Point of Sale
dashboard and can open any of them. This module adds an Allowed Users list
to each shop.

* Cashiers only see the shops that list them (or shops with an empty list)
* A shop they are not allowed in cannot be opened, even with a direct link
* Point of Sale administrators and Settings administrators always see every shop
* One field in Point of Sale > Configuration > Settings, also shown in the shop list
""",
    'author': 'Tamadoo',
    'website': 'https://tamadoo-odoo.github.io/modules/tamadoo_pos_user_restrict/',
    'maintainer': 'Sandi Gautama',
    'support': 'tama.odoo@gmail.com',
    'license': 'LGPL-3',
    'depends': ['point_of_sale'],
    'data': [
        'security/ir.access.csv',
        'views/res_config_settings_views.xml',
    ],
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
