{
    'name': 'TIVA POS Reference UI',
    'version': '20.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Dark navy and gold POS interface inspired by the supplied TIVA reference',
    'description': '''A dark-only visual redesign for Odoo 20 Point of Sale. Removes white surfaces,
styles product tiles, order panel, category controls, search, dialogs and action buttons.
It does not replace Odoo's business logic or payment flow.''',
    'depends': ['point_of_sale'],
    'assets': {
        'point_of_sale._assets_pos': [
            'tiva_pos_reference_ui/static/src/css/tiva_pos_reference_ui.css',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
