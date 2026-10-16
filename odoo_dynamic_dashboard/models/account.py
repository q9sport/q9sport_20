from odoo import api , fields , models
from odoo.exceptions import AccessError


class AccountMove ( models.Model ) :
    _inherit = 'account.move.line'

    account_analytic_enabled = fields.Boolean ( related='account_id.analytic_distribution_field' , readonly=True , )
    partner_type = fields.Selection ( related='account_id.choose_partner' , string="Partner Type" , store=True , readonly=True , )
    journal_item_modified_date= fields.Date(string="Modified Date JI",readonly=False, )
    partner_id = fields.Many2one ( 'res.partner' , string='Partner' , )
    allowed_partner_ids = fields.Many2many ( 'res.partner' , compute='_compute_allowed_partner_ids' ,
                                             string='Allowed Partners' , )
    analytic_distribution_readonly = fields.Boolean (compute='_compute_analytic_distribution_readonly')

    @api.depends ( 'account_id' , 'account_id.choose_partner' )
    def _compute_allowed_partner_ids(self) :
        Partner = self.env['res.partner']

        for line in self :
            partner_type = line.account_id.choose_partner

            if partner_type == 'vendor' :
                line.allowed_partner_ids = Partner.search ( [
                    ('vendor' , '=' , True) ,
                ] )
            elif partner_type == 'customer' :
                line.allowed_partner_ids = Partner.search ( [
                    ('customer' , '=' , True) ,
                ] )
            else :
                line.allowed_partner_ids = Partner.browse ()
                

    @api.depends ( 'account_id' , 'account_id.analytic_distribution' )
    def _compute_analytic_distribution_readonly(self) :
        for line in self :
            line.analytic_distribution_readonly = (
                    not line.account_id
                    or not line.account_id.analytic_distribution
            )

    @api.onchange ( 'account_id' )
    def _onchange_account_id_partner(self) :
        for line in self :
            line.partner_id = False

    # journal_items_date=fields.Date(string="Item Date" ,  default=lambda self: self.env.context.get('default_move_id') andself.env['account.move'].browse( self.env.context['default_move_id']readonly=False , tracking=True , ondelete="restrict" ,store=True )# edit line date to be edited


class ChartsOfAccounts ( models.Model ) :
    _inherit = 'account.account'

    analytic_distribution = fields.Boolean ( string="Choose Analytic Distribution" , default=False , readonly=False ,
                                             tracking=True )
    analytic_distribution_field = fields.Boolean ( string="Choose Analytic Distribution" , default=False ,
                                                   readonly=False , tracking=True )
    choose_partner = fields.Selection ( [
         ('none' , 'None') ,
        ('vendor' , 'Vendor') ,
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
