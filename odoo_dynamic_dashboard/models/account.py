from odoo import api , fields , models
from odoo.exceptions import AccessError


class AccountMove ( models.Model ) :
    _inherit = 'account.move.line'


    date=fields.Date(string="Date" , readonly=False , tracking=True , ondelete="restrict" ,store=True )# edit line date to be edited



class ChartsOfAccounts (models.Model):
    _inherit = 'account.account'







