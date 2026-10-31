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
    date = fields.Date(string='Date', required=True, default=fields.Date.context_today, index=True, copy=True, related=False, compute=False,readonly=False,)
    analytic_distribution_readonly = fields.Boolean ( compute='_compute_analytic_distribution_readonly' )
    #copy_line_button = fields.Boolean ( string="Copy" , compute="_compute_copy_line_button" , )

    def action_copy_reverse_line(self) :
        self.ensure_one ()

        move = self.move_id

        if not move :
            raise UserError ( "سطر القيد غير مرتبط بقيد محاسبي." )

        if move.state != 'draft' :
            raise UserError ( "يمكن نسخ وعكس السطر في القيود المسودة فقط." )

        if self.display_type != 'product' and self.display_type != 'payment_term' and self.display_type != 'tax' and self.display_type != 'rounding' and self.display_type != 'epd' and self.display_type != 'cogs' and self.display_type != 'asset_receivable' and self.display_type != 'line_section' and self.display_type != 'line_note' :
            # السماح فقط بسطر محاسبي فعلي
            pass

        if self.display_type in ('line_section' , 'line_note') :
            raise UserError ( "لا يمكن عكس سطر عنوان أو ملاحظة." )

        vals = self.copy_data ()[0]

        # عكس المدين والدائن
        vals['debit'] = self.credit
        vals['credit'] = self.debit

        # الحفاظ على نفس القيد
        vals['move_id'] = move.id

        # إنشاء السطر الجديد
        new_line = self.create ( vals )

        return {
            'type' : 'ir.actions.client' ,
            'tag' : 'reload' ,
        }

    ##############

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
    def copy_journal_line(self) :
        self.ensure_one ()

        move = self.move_id

        if move.state != 'draft' :
            return False

        vals = self.copy_data ()[0]

        vals.update ( {
            'move_id' : move.id ,
            'sequence' : self.sequence + 1 ,
            'debit' : self.credit ,
            'credit' : self.debit ,
            'amount_currency' : -self.amount_currency ,
        } )

        self.env['account.move.line'].with_context (
            check_move_validity=False
        ).create ( vals )

        return {
            'type' : 'ir.actions.client' ,
            'tag' : 'reload' ,
        }
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
        ('customer' , 'Customer') ,
        ('all' , 'All')] , string="Choose Partner Type" , store=True , tracking=True , searchable=True )


## editing product
class ProductTemplate ( models.Model ) :
    _inherit = 'product.template'

    arabic_name = fields.Char ( string="Arabic Name" , tracking=True , store=True )
    attribute_id = fields.Many2one ( 'product.attribute' , string='Product Attribute' ,
                                     related='attribute_line_ids.attribute_id' , store=True , )
    product_tags_id = fields.Many2many (
        comodel_name='product.tag' ,
        string='Season' ,
        relation='product_template_product_tag_rel' ,
        column1='product_template_id' ,
        column2='product_tag_id' ,
    )


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


class AccountAnalyticLine ( models.Model ) :
    _inherit = 'account.analytic.line'

    debit_credit_type = fields.Selection (
        [
            ('debit' , 'Debit') ,
            ('credit' , 'Credit') ,
        ] ,
        string='Type' ,
        compute='_compute_debit_credit_display' ,
    )

    debit_credit_amount = fields.Monetary (
        string='Amount_' ,
        currency_field='currency_id' ,
        compute='_compute_debit_credit_display' ,
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

    @api.depends ( 'amount' )
    def _compute_debit_credit_display(self) :
        for line in self :
            line.debit_credit_type = (
                'debit' if line.amount < 0 else 'credit'
            )


# ### class for editing the general ledger
# class AccountReport(models.Model):
#     _inherit = 'account.report'

#     def _report_engine_journal_item_description(
#         self, aml_id, options=None, date_scope=None,
#         formulas_dict=None, groupby=None, warnings=None,
#     ):
#         aml = self.env['account.move.line'].browse(aml_id).exists()
class AccountReport ( models.Model ) :
    _inherit = 'account.report'

    def _report_engine_general_ledger(
            self , expressions , options , date_scope ,
            current_groupby , next_groupby ,
            offset=0 , limit=None , warnings=None
    ) :
        result = super ()._report_engine_general_ledger (
            expressions ,
            options ,
            date_scope ,
            current_groupby ,
            next_groupby ,
            offset=offset ,
            limit=limit ,
            warnings=warnings ,
        )
        return result
