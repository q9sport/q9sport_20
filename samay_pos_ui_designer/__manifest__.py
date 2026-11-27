# -*- coding: utf-8 -*-
# Part of Samay POS UI Designer. See LICENSE file for full copyright and licensing details.

{
    'name': 'POS UI Designer | Modern POS Layout & Theme Visual Customizer',
    'version': '20.0.1.0.0',
    'category': 'Sales/Point of Sale',
    'summary': 'Next-generation visual POS Layout & Theme Designer for Odoo 20 (Fluid Responsive Grid, Zero Blank Space, OWL Live Preview, 4 Production Presets)',
    'images': ['static/description/banner.png'],
    'author': 'Samay Techno',
    'website': 'https://www.samaytechnology.com',
    'license': 'LGPL-3',
    'depends': ['point_of_sale'],
    'data': [
        'security/ir.access.csv',
	'security/designer_rules.xml',
        'views/profile_views.xml',
        'views/pos_config_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'samay_pos_ui_designer/static/src/common/designer.js',
            'samay_pos_ui_designer/static/src/common/designer.css',
            'samay_pos_ui_designer/static/src/backend/preview.js',
            'samay_pos_ui_designer/static/src/backend/preview.xml',
            'samay_pos_ui_designer/static/src/backend/preview.css',
        ],
        'point_of_sale._assets_pos': [
            'samay_pos_ui_designer/static/src/common/designer.js',
            'samay_pos_ui_designer/static/src/common/designer.css',
            'samay_pos_ui_designer/static/src/pos/designer_patch.js',
            'samay_pos_ui_designer/static/src/pos/designer_templates.xml',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
}
