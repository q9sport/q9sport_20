{
    "name": "Q9 Daily Cashier Report",
    "version": "20.0.1.0.0",
    "category": "Point of Sale",
    "summary": "Daily cashier report with PDF and Excel exports",
    "depends": ["point_of_sale"],
    "data": [
        "security/ir.access.csv",
        "wizard/daily_cashier_wizard_views.xml",
        "report/daily_cashier_report.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
