from odoo import api , fields , models
from odoo.exceptions import AccessError


class AccountMove ( models.Model ) :
    _inherit = 'account.move.line'


    date=fields.Date(string="Date" ,readonly=False ,tracking=True ,ondelete="restrict" ,default=lambda self: self.env.context.get('default_move_id') and self.env['account.move'].browse(self.env.context['default_move_id']).date,)# edit line date to be edited



class ChartsOfAccounts (models.Model):
    _inherit = 'account.account'







