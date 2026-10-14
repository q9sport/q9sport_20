from odoo import api , fields , models
from odoo.exceptions import AccessError


class AccountMove ( models.Model ) :
    _inherit = 'account.move.line'

    account_analytic_enabled = fields.Boolean ( related='account_id.analytic_distribution_field' , readonly=True , )
    partner_type = fields.Selection ( related='account_id.choose_partner' , string="Partner Type" , store=True ,
                                      readonly=True , )
    partner_id = fields.Many2one ( 'res.partner' , string='Partner' , domain="[('id', 'in', allowed_partner_ids)]" , )
    allowed_partner_ids = fields.Many2many ( 'res.partner' , compute='_compute_allowed_partner_ids' ,
                                             string='Allowed Partners' , )

    @api.depends ( 'account_id' , 'account_id.choose_partner' )
    def _compute_allowed_partner_ids(self) :
        for line in self :
            if line.account_id.choose_partner == 'vendor' :
                line.allowed_partner_ids = self.env['res.partner'].search ( [
                    ('vendor' , '=' , True) ,
                ] )
            elif line.account_id.choose_partner == 'customer' :
                line.allowed_partner_ids = self.env['res.partner'].search ( [
                    ('customer' , '=' , True) ,
                ] )
            else :
                line.allowed_partner_ids = self.env['res.partner'].browse ( [] )
           
           
    ### identifying vendor or customer when choosing account  
    @api.onchange('account_id')
    def _onchange_account_id_partner(self):
        for line in self:
            line.partner_id = False

            if line.account_id.choose_partner == 'vendor':
                return {
                    'domain': {
                        'partner_id': [('vendor', '=', True)],
                    }
                }

            elif line.account_id.choose_partner == 'customer':
                return {
                    'domain': {
                        'partner_id': [('customer', '=', True)],
                    }
                }

            else:
                return {
                    'domain': {
                        'partner_id': [('id', '=', False)],
                    }
                }
                



    # journal_items_date=fields.Date(string="Item Date" ,  default=lambda self: self.env.context.get('default_move_id') andself.env['account.move'].browse( self.env.context['default_move_id']readonly=False , tracking=True , ondelete="restrict" ,store=True )# edit line date to be edited


class ChartsOfAccounts ( models.Model ) :
    _inherit = 'account.account'

    analytic_distribution = fields.Boolean ( string="Choose Analytic Distribution" , default=False , readonly=False ,
                                             tracking=True )
    analytic_distribution_field = fields.Boolean ( string="Choose Analytic Distribution" , default=False ,
                                                   readonly=False , tracking=True )
    choose_partner = fields.Selection ( [
        ('venor' , 'Vendor') ,
        ('customer' , 'Customer')] , string="Choose Partner Type" , store=True , tracking=True , searchable=True )


### editing contact ##
class AccountMove ( models.Model ) :
    _inherit = 'res.partner'

    customer = fields.Boolean ( string="Customer Type" , default=False , readonly=False , tracking=True )
    vendor = fields.Boolean ( string="Vendor Type" , default=False , readonly=False , tracking=True )

    @api.onchange ( 'supplier_rank' , 'customer_rank' )
    def identify_contact_type(self) :
        for partner in self :
            partner.customer = partner.customer_rank > 0
            partner.vendor = partner.supplier_rank > 0













