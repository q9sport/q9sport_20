# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Customer and order metafields imported into the Odoo contact and sales order.

Only stores with customer or order mappings make the extra read. Order
metafields are read whenever an order is imported or updated. Customer
metafields are read on every customer update, and once when an order first
meets a linked customer, so a busy customer does not cost a read per order.
Values are written only where they differ; empty Shopify values are skipped.
"""
from odoo import api, fields, models

from ..lib import documents, errors

OWNERS = {'customer': (documents.CUSTOMER_METAFIELDS, 'customer'), 'order': (documents.ORDER_METAFIELDS, 'order')}
IMPORTS = ('import', 'both')


class ShopifyMetafieldMapParties(models.Model):
    _inherit = 'eh.shopify.metafield.map'

    @api.model
    def _pull_owner(self, store, owner_type, gid, record):
        """Read the mapped metafields of one customer or order and write what
        differs. Returns the notes and whether Shopify returned the owner."""
        maps = store._metafield_maps(owner_type, IMPORTS)
        if not maps or not record:
            return [], False
        document, root = OWNERS[owner_type]
        data = store._client().execute(store._doc_text(document), {'id': gid, 'keys': [m._full_key() for m in maps]},
                                       tolerate=errors.is_protected_data_error).data or {}
        owner = data.get(root)
        if not owner:
            return [], False
        nodes = ((owner.get('metafields') or {}).get('nodes')) or []
        notes = []
        vals = self.env['eh.shopify.product']._metafield_vals(
            maps, record, {'%s.%s' % (node['namespace'], node['key']): node for node in nodes}, notes)
        if vals:
            record.sudo().write(vals)
        return notes, True


class ShopifyCustomerMetafields(models.Model):
    _inherit = 'eh.shopify.customer'

    metafields_read_at = fields.Datetime('Metafields read', readonly=True, copy=False)

    def _pull_metafields(self):
        self.ensure_one()
        if self.kind != 'customer' or not self.partner_id or self.removed_in_shopify:
            return []
        notes, found = self.env['eh.shopify.metafield.map']._pull_owner(self.store_id, 'customer', self.shopify_gid,
                                                                        self.partner_id)
        if found:
            self.metafields_read_at = fields.Datetime.now()
        return notes

    def _job_customer_sync(self, job):
        result = super()._job_customer_sync(job)
        if isinstance(result, dict) and result.get('customer') and job.store_id._metafield_maps('customer', IMPORTS):
            binding = self._binding(job.store_id, result['customer'])
            notes = binding._pull_metafields() if binding else []
            if notes:
                result['metafield_notes'] = notes
        return result


class ShopifyOrderMetafields(models.Model):
    _inherit = 'eh.shopify.order'

    def _apply_to_odoo(self, order):
        result = super()._apply_to_odoo(order)
        store = self.store_id
        sale_order = self.sale_order_id
        notes = []
        if sale_order and sale_order.state != 'cancel' and store._metafield_maps('order', IMPORTS):
            notes += self.env['eh.shopify.metafield.map']._pull_owner(store, 'order', self.shopify_gid, sale_order)[0]
        customer = self.customer_binding_id
        if customer and not customer.metafields_read_at and store._metafield_maps('customer', IMPORTS):
            notes += customer._pull_metafields()
        if notes and isinstance(result, dict):
            result['metafield_notes'] = list(dict.fromkeys(notes))
        return result
