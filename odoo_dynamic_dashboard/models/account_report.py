from odoo import api, models
from odoo.fields import Domain


class AccountReport(models.Model):
    _inherit = 'account.report'

    def _init_options_account_selection(self, options, previous_options):
        """Initialize the Chart of Accounts selector."""
        previous_ids = previous_options.get('account_selection_ids', [])
        account_ids = [int(x) for x in previous_ids if str(x).isdigit()]

        accounts = self.env['account.account'].search([
            ('company_ids', 'in', self.env.companies.ids),
        ])

        selected_accounts = accounts.filtered(lambda account: account.id in account_ids)

        options['display_account_selection'] = True
        options['account_selection_ids'] = selected_accounts.ids
        options['selected_account_names'] = selected_accounts.mapped(
            lambda account: f'{account.code} - {account.name}'
        )

    def _get_options_domain(self, options, date_scope):
        domain = super()._get_options_domain(options, date_scope)

        account_ids = options.get('account_selection_ids', [])
        if account_ids:
            domain &= Domain('account_id', 'in', [int(x) for x in account_ids])

        return domain
