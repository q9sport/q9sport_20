from odoo import api , fields , models
from odoo.exceptions import AccessError


class AccountMove ( models.Model ) :
    _inherit = 'res.partner'
    customer = fields.Boolean(String="Customer_Type" ,default=False,readonly=False, tracking=True)
    vendor = fields.Boolean(String="Vendor_Type" ,default=False,readonly=False, tracking=True)
    
  
  
  
