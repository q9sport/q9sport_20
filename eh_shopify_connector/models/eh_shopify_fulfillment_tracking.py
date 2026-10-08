# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Send tracking changes made in Odoo after a shipment was sent to Shopify.

Only fulfillments Odoo created are updated; shipments made in Shopify keep the
tracking Shopify has. The customer is not emailed again for a correction.
Sending the same tracking twice leaves Shopify unchanged, and fulfillments
whose tracking already matches are skipped, so a retry sends nothing new.
"""
from odoo import _, models

from ..lib import documents


class ShopifyFulfillmentTracking(models.Model):
    _inherit = 'eh.shopify.order'

    def _job_fulfillment_tracking(self, job):
        store = job.store_id
        picking = self.env['stock.picking'].browse((job.payload or {}).get('picking_id')).exists()
        if not picking:
            return {'skipped': 'the delivery was removed'}
        records = self.env['eh.shopify.fulfillment'].sudo().search([
            ('store_id', '=', store.id), ('picking_id', '=', picking.id), ('origin', '=', 'odoo'),
            ('shopify_gid', '!=', False), ('status', '!=', 'CANCELLED')])
        if not records:
            return {'skipped': 'nothing was sent to Shopify for this delivery'}
        tracking = records[0].order_binding_id._tracking_for(picking)
        numbers = tracking.get('numbers') or []
        if not numbers:
            return {'skipped': 'the delivery has no tracking number', 'updated': []}
        company = tracking.get('company') or False
        text = ', '.join(numbers)
        client = store._client()
        document = documents.FULFILLMENT_TRACKING_UPDATE
        updated = []
        for record in records:
            if (record.tracking_numbers or '') == text and (record.tracking_company or False) == company:
                continue
            info = {'numbers': numbers}
            if company:
                info['company'] = company
            client.mutate(store._doc_text(document), {'fulfillmentId': record.shopify_gid, 'trackingInfoInput': info,
                                                      'notifyCustomer': False}, document.payload_key)
            record.write({'tracking_numbers': text, 'tracking_company': company})
            updated.append(record.name or record.shopify_gid)
        if updated:
            picking.message_post(body=_('New tracking sent to Shopify for %s.') % ', '.join(updated))
        return {'updated': updated}
