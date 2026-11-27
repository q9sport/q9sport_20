{
    'name': 'TIVA POS Dark Theme',
    'version': '20.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Modern dark navy and gold POS theme inspired by TIVA POS layout',
    'description': 'Visual theme for Odoo 20 Point of Sale. Styles POS cards, category buttons, order panel, top controls and payment action with a dark navy/gold appearance.',
    'depends': ['point_of_sale'],
    'assets': {
        'point_of_sale._assets_pos': [
            'tiva_pos_theme/static/src/css/tiva_pos_theme.css',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
