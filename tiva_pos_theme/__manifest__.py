{
    'name': 'TIVA POS - Elegant Dark UI',
    'version': '20.0.1.1.0',
    'category': 'Point of Sale',
    'summary': 'Elegant navy POS skin with subtle gold accents for Odoo 20',
    'description': 'Visual styling for Odoo 20 POS. Keeps native POS behavior and uses a restrained navy/gold palette, refined product cards, buttons, panels, and checkout controls.',
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
