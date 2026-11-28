{
    "name": "POS Login Screen Background",
    "version": "20.0.1.0.1",
    "category": "Point of Sale",
    "summary": "POS Login Background allows businesses to customize and personalize the Point of Sale login interface with a branded background image. This module helps apply POS branding, change the POS lock background, and create a custom login background that matches the company identity. With this feature, users can easily rebrand the POS interface, apply a custom POS background, and enhance the overall appearance of the Point of Sale system with a personalized design. POS login background, Odoo POS login background, Point of Sale custom background, POS branding, POS brand background, Odoo POS customization Background,  POS login page customization, POS lock background, POS lock screen background, change POS background, custom POS login background, Odoo Point of Sale background change, POS rebrand background, POS login screen branding, branded POS login page, POS design customization, POS background configuration, POS appearance customization, Odoo POS UI branding, POS login page theme, custom POS login design, POS system personalization, POS login background image, POS brand identity customization. .",
    "description": "POS Login Screen Background allows you to customize the background image of the Point of Sale (POS) login screen in Odoo.",
    "author": "INKERP",
    "website": "www.inkerp.com",
    "depends": [
        "point_of_sale",
        "eg_app_base"
    ],
    "data": [
        "views/pos_config_view.xml"
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "eg_pos_login_background/static/src/xml/**/*"
        ]
    },
    "post_init_hook": "_enable_eg_pos_login_background_post_init",
    "demo": [],
    "license": "OPL-1",
    "installable": True,
    "application": True,
    "auto_install": False,
    "price": "0.0",
    "currency": "EUR",
    "images": [
        "static/description/banner.gif"
    ]
}