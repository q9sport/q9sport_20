# -*- coding: utf-8 -*-
from odoo import models


# =========================================================
# EXTENSION OF PURCHASE ORDER BARCODE WIZARD
# =========================================================

class PurchaseOrderBarcodeWizard(models.TransientModel):
    _inherit = 'purchase.order.barcode.wizard'

    # The main wizard and its fields are defined in barcode_wizard.py.
    # Do not redefine _name or duplicate its fields here.

