{
    "name": "INKERP Apps",
    "category": "Extra Tools",
    "version": "20.0.1.0.0",
    "author": "INKERP",
    "website": "www.inkerp.com",
    "summary": "INKERP Apps is a centralized Odoo platform that showcases the complete suite of business applications developed by INKERP, offering seamless navigation and an intuitive interface for users to explore, install, and manage Odoo modules efficiently. With flexible Kanban and List views, each app dynamically displays an \u201cInstall\u201d button for locally available addons or a \u201cBuy Now\u201d button linking to the official Odoo App Store. Users can easily refresh or auto-sync their app list to stay updated with the latest INKERP Odoo apps, ensuring continuous access to innovative, high-performance ERP, CRM, and productivity solutions that enhance business efficiency and digital transformation. INKERP Apps, ERP solutions, business management software, customization services, app store, app installation, app suite, integrations, business applications, productivity tools, CRM modules, accounting apps, project management modules, inventory management, HR management apps, ERP business solutions, automation tools, best apps for business growth, INKERP app suite, manage apps in one platform, centralized app management, install and buy apps easily, latest modules by INKERP, app synchronization and updates, explore premium applications, apps for digital transformation, all-in-one app management tool. .",
    "description": "INKERP Apps is a centralized platform that showcases the complete suite of applications developed by INKERP. Designed for seamless navigation, users can explore available apps in both Kanban and List views for enhanced usability. Each app listing dynamically displays an \"Install\" button if the app is available in the local addons, or a \"Buy Now\" button that redirects users to the official Odoo App Store when external purchase is required. Users have the flexibility to manually refresh the app list or enable automatic synchronization through the settings, ensuring they always have access to the latest offerings from INKERP.",
    "depends": [
        "base_setup"
    ],
    "data": [
        "security/ir.access.csv",
        "data/ir_cron.xml",
        "views/apps_inkerp.xml",
        "views/res_config_settings.xml",
        "wizards/import_apps.xml"
    ],
    "assets": {
        "web.assets_backend": [
            "eg_app_base/static/src/css/custom_css.css"
        ]
    },
    "post_init_hook": "_enable_auto_fetch_post_init",
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