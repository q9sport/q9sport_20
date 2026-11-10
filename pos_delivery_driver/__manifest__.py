# -*- coding: utf-8 -*-
{
    "name": "POS Delivery Driver Management",
    "version": "20.0.3.0.0",
    "category": "Sales/Point of Sale",
    "summary": "Assign and track delivery drivers from Point of Sale",
    "author": "Custom Development",
    "description": "Manage delivery drivers, assign POS orders, track delivery status and collections.",
    "depends": ["point_of_sale", "hr"],
    "data": [
        "views/hr_employee_views.xml",
        "views/delivery_driver_menu.xml",
        "views/pos_order_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "pos_delivery_driver/static/src/app/delivery_driver_order.js",
            "pos_delivery_driver/static/src/app/delivery_driver_auto.js",
            "pos_delivery_driver/static/src/app/delivery_driver_popup.js",
            "pos_delivery_driver/static/src/app/delivery_driver_popup.xml",
            "pos_delivery_driver/static/src/app/delivery_driver.scss",
        ],
    },
    "images": ["static/description/icon.png"],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
