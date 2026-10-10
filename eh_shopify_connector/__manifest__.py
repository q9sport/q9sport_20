# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
{
    'name': 'Shopify Connector',
    'version': '20.0.1.0.0',
    'category': 'Sales/Sales',
    'sequence': 120,
    'summary': 'Shopify Odoo connector on the GraphQL Admin API: orders, stock, catalogue, refunds, returns, '
               'payouts, B2B price lists and customers with signed webhooks and a durable sync engine, '
               'Shopify integration, Shopify ERP sync, Odoo 16 17 18 19 20.',
    'description': 'A Shopify connector for Odoo built on the current GraphQL Admin API. Connects with a Dev '
                   'Dashboard app (client credentials or install link) or an existing admin token, stores '
                   'credentials encrypted, verifies every webhook signature, processes work through a durable '
                   'job engine with retries and a single exceptions inbox, and falls back to scheduled polling '
                   'whenever webhooks cannot reach Odoo.',
    'author': 'ERP Heritage',
    'company': 'ERP Heritage',
    'maintainer': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au',
    'support': 'info@erpheritage.com.au',
    'images': [
        'static/description/banner.gif',
        'static/description/shot_hero.png',
        'static/description/shot_parity.png',
        'static/description/shot_payout.png',
        'static/description/shot_stock.png',
        'static/description/shot_jobs.png',
        'static/description/shot_exception.png',
        'static/description/shot_setup.png',
    ],
    'license': 'OPL-1',
    'price': 0.0,
    'currency': 'USD',
    'depends': ['mail', 'sale_management', 'sale_stock', 'account', 'stock_delivery'],
    'external_dependencies': {'python': ['requests', 'cryptography']},
    'data': [
        'security/eh_shopify_security.xml',
        'data/ir_cron.xml',
        'views/eh_shopify_job_views.xml',
        'views/eh_shopify_event_views.xml',
        'views/eh_shopify_webhook_views.xml',
        'views/eh_shopify_location_views.xml',
        'views/eh_shopify_order_views.xml',
        'views/eh_shopify_inventory_views.xml',
        'views/eh_shopify_product_views.xml',
        'views/eh_shopify_bulk_views.xml',
        'views/eh_shopify_match_views.xml',
        'views/eh_shopify_refund_views.xml',
        'views/eh_shopify_return_views.xml',
        'views/eh_shopify_payout_views.xml',
        'views/eh_shopify_dispute_views.xml',
        'views/stock_picking_views.xml',
        'wizards/eh_shopify_setup_wizard_views.xml',
        'wizards/eh_shopify_cancel_wizard_views.xml',
        'wizards/eh_shopify_stock_compare_wizard_views.xml',
        'wizards/eh_shopify_publish_wizard_views.xml',
        'wizards/eh_shopify_refund_wizard_views.xml',
        'wizards/eh_shopify_order_edit_wizard_views.xml',
        'views/eh_shopify_store_views.xml',
        'views/eh_shopify_menus.xml',
        'security/ir.access.csv',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_shopify_connector/static/tests/tours/**/*',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'application': True,
    'installable': True,
}
