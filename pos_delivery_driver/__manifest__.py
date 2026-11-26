{
    "name": "POS Delivery Driver Management",
    "version": "20.0.4.0.1",
    "category": "Sales/Point of Sale",
    "summary": "Assign and track delivery drivers from Point of Sale",
    "author": "Custom Development",
    "description": """
        Manage delivery drivers, assign POS orders,
        track delivery status and collections.
    """,
    "depends": [
        "base",
        "point_of_sale",
        "hr",
        "account",
    ],
    "data": [
        # "security/ir.access.csv",
        "views/hr_employee_views.xml",
        "views/delivery_driver_menu.xml",
        "views/pos_order_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            # "pos_delivery_driver/static/src/app/delivery_driver_popup.js",
            "pos_delivery_driver/static/src/app/delivery_driver_auto.js",
            # "pos_delivery_driver/static/src/app/delivery_driver_popup.xml",
            "pos_delivery_driver/static/src/app/delivery_driver.scss",
            "pos_delivery_driver/static/src/js/delivery_orders_button.js",
            "pos_delivery_driver/static/src/xml/delivery_orders_button.xml",
            

        ],
    },
    "images": [
        "static/description/icon.png",
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
