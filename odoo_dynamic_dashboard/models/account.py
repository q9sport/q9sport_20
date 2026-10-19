from odoo import api , fields , models
from odoo.exceptions import AccessError


class AccountMove ( models.Model ) :
    _inherit = 'account.move.line'

    account_analytic_enabled = fields.Boolean ( related='account_id.analytic_distribution_field' , readonly=True , )
    partner_type = fields.Selection ( related='account_id.choose_partner' , string="Partner Type" , store=True ,
                                      readonly=True , )
    journal_item_modified_date = fields.Date ( string="Modified Date JI" , readonly=False , )
    partner_id = fields.Many2one ( 'res.partner' , string='Partner' , )
    allowed_partner_ids = fields.Many2many ( 'res.partner' , compute='_compute_allowed_partner_ids' ,
                                             string='Allowed Partners' , )
    analytic_distribution_readonly = fields.Boolean ( compute='_compute_analytic_distribution_readonly' )



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


    #### copy the journal item ####
    def copy_journal_line(self):
        self.ensure_one()

        if self.move_id.state != 'draft':
            return False

        self.copy({
            'move_id': self.move_id.id,
            'sequence': self.sequence + 1,
            'debit': 0.0,
            'credit': 0.0,
            'amount_currency': 0.0,
        })

        return True
        # self.ensure_one ()
        #
        # move = self.move_id
        #
        # if move.state != 'draft' :
        #     return False
        #
        # self.copy ( {
        #     'move_id' : move.id ,
        #     'sequence' : self.sequence + 1 ,
        # } )
        #
        # return True

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

    analytic_plan_ids = fields.Many2many ( comodel_name='account.analytic.plan' , string='Analytic Plans' , )

    analytic_account_ids = fields.Many2many ( comodel_name='account.analytic.account' , string='Analytic Accounts' , )

    choose_partner = fields.Selection ( [
        ('none' , 'None') ,
        ('vendor' , 'Vendor') ,
        ('customer' , 'Customer')] , string="Choose Partner Type" , store=True , tracking=True , searchable=True )


## editing product
class ProductTemplate ( models.Model ) :
    _inherit = 'product.template'

    arabic_name = fields.Char ( string="Arabic Name" , tracking=True , store=True )
    attribute_id = fields.Many2one ( 'product.attribute' , string='Product Attribute' ,
                                     related='attribute_line_ids.attribute_id' , store=True , )


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



class AccountAnalyticLine(models.Model):
    _inherit = 'account.analytic.line'

    debit_credit_type = fields.Selection(
        [
            ('debit', 'Debit'),
            ('credit', 'Credit'),
        ],
        string='Type',
        compute='_compute_debit_credit_display',
    )

    debit_credit_amount = fields.Monetary(
        string='Amount_',
        currency_field='currency_id',
        compute='_compute_debit_credit_display',
    )

    journal_item_modified_date = fields.Date (
        string="Modified Date JI" ,
        compute='_compute_journal_item_modified_date' ,
    )

    def _compute_journal_item_modified_date(self) :
        MoveLine = self.env['account.move.line']

        for line in self :
            move_line = MoveLine.search ( [
                ('analytic_line_ids' , 'in' , line.id) ,
            ] , limit=1 )

            line.journal_item_modified_date = (
                move_line.journal_item_modified_date
                if move_line else False
            )
            

    @api.depends('amount')
    def _compute_debit_credit_display(self):
        for line in self:
            line.debit_credit_type = (
                'debit' if line.amount < 0 else 'credit'
            )
            line.debit_credit_amount = abs(line.amount)

    
            
