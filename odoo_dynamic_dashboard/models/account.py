from odoo import api , fields , models
from odoo.exceptions import AccessError


class AccountMove ( models.Model ) :
    _inherit = 'account.move.line'
    
    account_analytic_enabled = fields.Boolean(related='account_id.choose_analytic_distribution',readonly=True,)
    partner_type =field.Char(related='account_id.choose_partner',string="Partner Type" ,readonly=True,)


    #journal_items_date=fields.Date(string="Item Date" ,  default=lambda self: self.env.context.get('default_move_id') andself.env['account.move'].browse( self.env.context['default_move_id']readonly=False , tracking=True , ondelete="restrict" ,store=True )# edit line date to be edited



class ChartsOfAccounts (models.Model):
    _inherit = 'account.account'
    
    analytic_distribution = fields.Boolean(String="Choose Analytic Distribution" ,default=False,readonly=False, tracking=True)
    choose_partner = fields.Selection ( [
        ('venor' , 'Vendor') ,
        ('customer' , 'Customer') ] , string="Choose Partner Type" , store=True , tracking=True ,searchable=True )







