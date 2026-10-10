# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Preview of the edit that makes the Shopify order hold the Odoo quantities."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..lib import errors


class ShopifyOrderEditWizard(models.TransientModel):
    _name = 'eh.shopify.order.edit.wizard'
    _description = 'Send Order Changes to Shopify'

    sale_order_id = fields.Many2one('sale.order', required=True, readonly=True)
    binding_id = fields.Many2one('eh.shopify.order', readonly=True)
    blocked_reason = fields.Char(readonly=True)
    line_ids = fields.One2many('eh.shopify.order.edit.wizard.line', 'wizard_id', readonly=True)
    notify_customer = fields.Boolean('Email the customer the updated order', default=False)
    staff_note = fields.Char('Note for staff', size=255)

    @api.model
    def _open_for(self, sale_order):
        binding = sale_order.sudo().eh_shopify_order_id
        if not binding:
            raise UserError(_('%s did not come from Shopify.') % sale_order.name)
        blocker = binding._edit_blocker()
        proposal = {'set': [], 'add': [], 'link': [], 'skipped': []}
        if not blocker:
            try:
                proposal = binding._edit_proposal(binding._fetch_order(binding.store_id, binding.shopify_gid))
            except errors.ShopifyError as error:
                blocker = _('Shopify could not be read: %s') % error.message
        if not blocker and not proposal['set'] and not proposal['add'] and not proposal['link']:
            blocker = _('Shopify already holds the quantities of this order.')
        lines = [(0, 0, {'action': 'set', 'name': c['name'], 'before': c['before'], 'after': c['after']})
                 for c in proposal['set']]
        lines += [(0, 0, {'action': 'add', 'name': c['name'], 'before': 0, 'after': c['after']}) for c in proposal['add']]
        lines += [(0, 0, {'action': 'link', 'name': c['name'], 'before': c['before'], 'after': c['after']})
                  for c in proposal['link']]
        lines += [(0, 0, {'action': 'skip', 'name': name, 'note': note}) for name, note in proposal['skipped']]
        wizard = self.create({'sale_order_id': sale_order.id, 'binding_id': binding.id, 'blocked_reason': blocker,
                              'line_ids': lines})
        return {'type': 'ir.actions.act_window', 'name': _('Send changes to Shopify'), 'res_model': self._name,
                'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def action_send(self):
        self.ensure_one()
        self.env['eh.shopify.job']._require_manager()
        binding = self.binding_id.sudo()
        blocker = binding._edit_blocker() or self.blocked_reason
        if blocker:
            raise UserError(blocker)
        binding._enqueue_edit_push(self.notify_customer, self.staff_note)
        self.sale_order_id.message_post(body=_('Changes queued for Shopify.'))
        return {'type': 'ir.actions.act_window_close'}


class ShopifyOrderEditWizardLine(models.TransientModel):
    _name = 'eh.shopify.order.edit.wizard.line'
    _description = 'Order Change for Shopify'

    wizard_id = fields.Many2one('eh.shopify.order.edit.wizard', required=True, ondelete='cascade')
    action = fields.Selection([('set', 'Change quantity'), ('add', 'Add'), ('link', 'Link to the Shopify line'),
                               ('skip', 'Not sent')], readonly=True)
    name = fields.Char('Item', readonly=True)
    before = fields.Integer('In Shopify', readonly=True)
    after = fields.Integer('After the edit', readonly=True)
    note = fields.Char(readonly=True)
