import json
from odoo import models


class AccountGeneralLedgerReportHandler(models.AbstractModel):
    _inherit = 'account.general.ledger.report.handler'

    def _get_gl_engine_data(
        self, options, date_scope, formulas_dict, current_groupby,
        additional_domain=None, warnings=None
    ):
        result = super()._get_gl_engine_data(
            options,
            date_scope,
            formulas_dict,
            current_groupby,
            additional_domain=additional_domain,
            warnings=warnings,
        )

        if current_groupby != 'id_with_accumulated_balance':
            return result

        aml_ids = set()

        # Collect journal item IDs from the report grouping keys.
        for rows in result.values():
            for grouping_key, row in rows:
                if isinstance(grouping_key, str) and not grouping_key.startswith('balance_line_'):
                    try:
                        aml_id = json.loads(grouping_key)[1]
                        if aml_id:
                            aml_ids.add(aml_id)
                    except (ValueError, TypeError, IndexError):
                        continue

        # Read move reference and name in one batch.
        refs_by_aml_id = {}
        names_by_aml_id = {}

        for aml in self.env['account.move.line'].browse(aml_ids):
            refs_by_aml_id[aml.id] = aml.move_id.ref or ''
            names_by_aml_id[aml.id] = aml.move_id.name or ''

        # Add the reference and move name to each detail row.
        for rows in result.values():
            for grouping_key, row in rows:
                if isinstance(grouping_key, str) and not grouping_key.startswith('balance_line_'):
                    try:
                        aml_id = json.loads(grouping_key)[1]
                        row['move_ref'] = refs_by_aml_id.get(aml_id, '')
                        row['move_name'] = names_by_aml_id.get(aml_id, '')
                    except (ValueError, TypeError, IndexError):
                        row['move_ref'] = ''
                        row['move_name'] = ''
                else:
                    row['move_ref'] = ''
                    row['move_name'] = ''

        return result
