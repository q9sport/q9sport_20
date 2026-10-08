# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime
import json
import logging

from psycopg2 import IntegrityError

from odoo import _, api, fields, models

from ..lib import documents, errors

_logger = logging.getLogger(__name__)


def _parse_triggered_at(value):
    if not value:
        return False
    try:
        return datetime.datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return False


class ShopifyEvent(models.Model):
    """One verified webhook delivery. Stored before processing, so a crash
    after the acknowledgement never loses the change."""

    _name = 'eh.shopify.event'
    _description = 'Shopify Webhook Delivery'
    _order = 'id desc'
    _rec_name = 'topic'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    webhook_id = fields.Char('Delivery ID', required=True)
    event_id = fields.Char('Event ID', index=True)
    topic = fields.Char(required=True, index=True)
    api_version = fields.Char('API version')
    triggered_at = fields.Datetime('Triggered')
    received_at = fields.Datetime('Received', default=fields.Datetime.now, index=True)
    resource_gid = fields.Char('Shopify record', index=True)
    payload = fields.Json('Raw payload')
    state = fields.Selection([
        ('queued', 'Queued'),
        ('processed', 'Processed'),
        ('ignored', 'Ignored'),
        ('failed', 'Failed'),
    ], default='queued', required=True, index=True)
    job_id = fields.Many2one('eh.shopify.job', ondelete='set null')
    note = fields.Char()
    payload_text = fields.Text('Payload', compute='_compute_payload_text')

    @api.depends('payload')
    def _compute_payload_text(self):
        for event in self:
            event.payload_text = json.dumps(event.payload, indent=2, sort_keys=True, default=str) \
                if event.payload else False

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_event_webhook_uniq "
                            "ON eh_shopify_event (store_id, webhook_id)")

    @api.model
    def _receive(self, store, headers, payload):
        """Persist a verified delivery and queue it. Returns (event, created)."""
        webhook_id = headers.get('X-Shopify-Webhook-Id')
        existing = self.sudo().search([('store_id', '=', store.id), ('webhook_id', '=', webhook_id)], limit=1)
        if existing:
            return existing, False
        resource_gid = False
        if isinstance(payload, dict):
            # A merge carries no resource id; it queues behind the customer Shopify kept.
            resource_gid = payload.get('admin_graphql_api_id') or payload.get('admin_graphql_api_customer_kept_id') \
                or False
        vals = {
            'store_id': store.id,
            'webhook_id': webhook_id,
            'event_id': headers.get('X-Shopify-Event-Id'),
            'topic': (headers.get('X-Shopify-Topic') or '').lower(),
            'api_version': headers.get('X-Shopify-API-Version'),
            'triggered_at': _parse_triggered_at(headers.get('X-Shopify-Triggered-At')),
            'resource_gid': resource_gid,
            'payload': payload,
        }
        try:
            with self.env.cr.savepoint():
                event = self.sudo().create(vals)
        except IntegrityError:
            return self.sudo().search([('store_id', '=', store.id), ('webhook_id', '=', webhook_id)], limit=1), False
        job = self.env['eh.shopify.job'].sudo()._enqueue(
            store, 'event.process', name=_('Webhook %s') % vals['topic'], payload={'event_id': event.id},
            dedup_key='event:%s' % event.id, resource_key=resource_gid or None, priority=3, record=event)
        event.job_id = job
        return event, True

    # ------------------------------------------------------------------ processing
    def _topic_handlers(self):
        return {
            'app/uninstalled': '_on_app_uninstalled',
            'shop/update': '_on_shop_update',
            'locations/create': '_on_location_change',
            'locations/update': '_on_location_change',
            'locations/delete': '_on_location_change',
            'orders/create': '_on_order_change',
            'orders/updated': '_on_order_change',
            'orders/paid': '_on_order_change',
            'orders/cancelled': '_on_order_change',
            'orders/fulfilled': '_on_order_change',
            'orders/partially_fulfilled': '_on_order_change',
            'orders/edited': '_on_order_change',
            'fulfillments/create': '_on_fulfillment_change',
            'fulfillments/update': '_on_fulfillment_change',
            'fulfillment_orders/hold_released': '_on_fulfillment_order_ready',
            'fulfillment_orders/scheduled_fulfillment_order_ready': '_on_fulfillment_order_ready',
            'fulfillment_orders/line_items_prepared_for_pickup': '_on_prepared_for_pickup',
            'inventory_levels/update': '_on_inventory_level_update',
            'products/create': '_on_product_change',
            'products/update': '_on_product_change',
            'products/delete': '_on_product_delete',
            'bulk_operations/finish': '_on_bulk_finish',
            'refunds/create': '_on_refund_create',
            'returns/request': '_on_return_change',
            'returns/approve': '_on_return_change',
            'returns/process': '_on_return_change',
            'returns/close': '_on_return_change',
            'returns/decline': '_on_return_change',
            'returns/cancel': '_on_return_change',
            'returns/reopen': '_on_return_change',
            'returns/update': '_on_return_change',
            'disputes/create': '_on_dispute_change',
            'disputes/update': '_on_dispute_change',
            'customers/create': '_on_customer_change',
            'customers/update': '_on_customer_change',
            'customers/delete': '_on_customer_delete',
            'customers/merge': '_on_customer_merge',
            'companies/create': '_on_company_change',
            'companies/update': '_on_company_change',
            'companies/delete': '_on_company_delete',
            # Shopify sends these to the address set in the app itself, not to a subscription.
            'customers/data_request': '_on_customer_data_request',
            'customers/redact': '_on_customer_redact',
            'shop/redact': '_on_shop_redact',
        }

    def _job_process(self, job):
        event = self.browse((job.payload or {}).get('event_id')).exists()
        if not event:
            return {'skipped': 'event removed'}
        if event.store_id != job.store_id:
            return {'skipped': 'event belongs to another store'}
        handler = event._topic_handlers().get(event.topic)
        if not handler:
            event.write({'state': 'ignored', 'note': _('No action is needed for this topic yet.')})
            return {'ignored': event.topic}
        result = getattr(event, handler)() or {'processed': event.topic}
        event.write({'state': 'ignored' if 'ignored' in result else 'processed'})
        return result

    def _identity_matches(self):
        """Shopify signs the body, not the headers, so shop level topics are
        only trusted when the signed body describes this very store."""
        payload = self.payload or {}
        store = self.store_id
        domain = (payload.get('myshopify_domain') or '').lower()
        if domain and domain != store.shop_domain:
            return False
        shop_id = payload.get('id')
        if shop_id and store.shop_gid and str(shop_id) != store.shop_gid.rsplit('/', 1)[-1]:
            return False
        return True

    def _on_app_uninstalled(self):
        if not self._identity_matches():
            self.note = _('The delivery does not describe this store, so it was ignored.')
            return {'ignored': 'identity mismatch'}
        store = self.store_id
        issued = store._credential().sudo().token_issued_at
        if self.triggered_at and issued and self.triggered_at < issued:
            self.note = _('The store was reconnected after this uninstall, so it stays connected.')
            return {'ignored': 'reconnected after uninstall'}
        try:
            store._client().execute(documents.SHOP_INFO.text_for(store.api_version))
        except errors.AuthError:
            store._on_app_uninstalled()
            return {'store': 'disconnected'}
        self.note = _('Shopify still accepts this store credentials, so the store stays connected.')
        return {'ignored': 'credentials still valid'}

    def _on_shop_update(self):
        if not self._identity_matches():
            self.note = _('The delivery does not describe this store, so it was ignored.')
            return {'ignored': 'identity mismatch'}
        self.env['eh.shopify.job'].sudo()._enqueue(self.store_id, 'store.check', name=_('Check connection'),
                                                  dedup_key='store.check', priority=5)
        return {'queued': 'store.check'}

    def _on_order_change(self):
        payload = self.payload or {}
        gid = payload.get('admin_graphql_api_id')
        if not gid and payload.get('id'):
            gid = 'gid://shopify/Order/%s' % payload['id']
        if not gid and (payload.get('order_edit') or {}).get('order_id'):
            gid = 'gid://shopify/Order/%s' % payload['order_edit']['order_id']
        if not gid:
            return {'skipped': 'no order id in payload'}
        if not self.store_id.orders_enabled:
            return {'skipped': 'order import is switched off'}
        self.env['eh.shopify.order'].sudo()._enqueue_import(self.store_id, gid, priority=4)
        return {'queued': gid}

    def _on_fulfillment_change(self):
        payload = self.payload or {}
        order_id = payload.get('order_id')
        if not order_id:
            return {'skipped': 'no order id in payload'}
        store = self.store_id
        if not store.orders_enabled:
            return {'skipped': 'order import is switched off'}
        gid = 'gid://shopify/Order/%s' % order_id
        Order = self.env['eh.shopify.order'].sudo()
        if Order.search_count([('store_id', '=', store.id), ('shopify_gid', '=', gid), ('sale_order_id', '!=', False)]):
            Order._enqueue_fulfillment_import(store, gid, priority=4)
        else:
            Order._enqueue_import(store, gid, priority=4)
        return {'queued': gid}

    def _on_fulfillment_order_ready(self):
        """A hold was released or a scheduled fulfillment order became due:
        retry the shipments of that order that were waiting on it."""
        payload = self.payload or {}
        gid = (payload.get('fulfillment_order') or {}).get('id') or payload.get('admin_graphql_api_id')
        if not gid:
            return {'skipped': 'no fulfillment order id in payload'}
        if str(gid).isdigit():
            gid = 'gid://shopify/FulfillmentOrder/%s' % gid
        store = self.store_id
        data = store._client().execute(store._doc_text(documents.FULFILLMENT_ORDER_ORDER), {'id': gid}).data or {}
        order_gid = (data.get('node') or {}).get('orderId')
        if not order_gid:
            return {'skipped': 'the fulfillment order is not visible any more'}
        waiting = self.env['eh.shopify.job'].sudo().search([
            ('store_id', '=', store.id), ('job_type', '=', 'fulfillment.push'), ('resource_key', '=', order_gid),
            ('state', 'in', ('failed', 'dead')), ('error_kind', '!=', 'ambiguous')])
        if waiting:
            waiting.action_retry()
        return {'retried': len(waiting), 'order': order_gid}

    def _on_prepared_for_pickup(self):
        """Staff marked a pickup ready in Shopify: record it on the Odoo delivery."""
        payload = self.payload or {}
        gid = (payload.get('fulfillment_order') or {}).get('id') or payload.get('admin_graphql_api_id')
        if not gid:
            return {'skipped': 'no fulfillment order id in payload'}
        if str(gid).isdigit():
            gid = 'gid://shopify/FulfillmentOrder/%s' % gid
        store = self.store_id
        data = store._client().execute(store._doc_text(documents.FULFILLMENT_ORDER_ORDER), {'id': gid}).data or {}
        order_gid = (data.get('node') or {}).get('orderId')
        binding = self.env['eh.shopify.order'].sudo().search([('store_id', '=', store.id),
                                                               ('shopify_gid', '=', order_gid)], limit=1)
        if not order_gid or not binding.sale_order_id:
            return {'skipped': 'the order is not in Odoo'}
        pickings = binding.sale_order_id.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and p.state not in ('done', 'cancel')
            and not p.eh_shopify_pickup_ready_at)
        pickings.write({'eh_shopify_pickup_ready_at': fields.Datetime.now()})
        for picking in pickings:
            picking.message_post(body=_('Marked ready for pickup in Shopify.'))
        return {'ready': pickings.mapped('name')}

    def _on_inventory_level_update(self):
        """Stock changed in Shopify, for example edited by hand there: queue the
        product so the quantity Odoo can sell is applied again."""
        payload = self.payload or {}
        item = payload.get('inventory_item_id')
        if item is None or item == '':
            return {'skipped': 'no inventory item in payload'}
        gid = item if str(item).startswith('gid://') else 'gid://shopify/InventoryItem/%s' % item
        store = self.store_id
        if not store.inventory_enabled:
            return {'skipped': 'stock sync is switched off'}
        products = self.env['eh.shopify.variant'].sudo().search([
            ('store_id', '=', store.id), ('inventory_item_gid', '=', gid), ('product_id', '!=', False)]).mapped('product_id')
        if not products:
            return {'skipped': 'the item is not linked to an Odoo product'}
        self.env['eh.shopify.store'].sudo()._mark_inventory_dirty(products)
        return {'queued': products.ids}

    def _on_product_change(self):
        store = self.store_id
        if not store.products_enabled:
            return {'ignored': 'product sync is switched off'}
        payload = self.payload or {}
        gid = payload.get('admin_graphql_api_id') or ('gid://shopify/Product/%s' % payload['id'] if payload.get('id')
                                                     else None)
        if not gid:
            return {'skipped': 'no product id in payload'}
        self.env['eh.shopify.product'].sudo()._enqueue_product_import(store, gid, priority=6)
        return {'queued': gid}

    def _on_product_delete(self):
        store = self.store_id
        payload = self.payload or {}
        gid = payload.get('admin_graphql_api_id') or ('gid://shopify/Product/%s' % payload['id'] if payload.get('id')
                                                     else None)
        if not gid:
            return {'skipped': 'no product id in payload'}
        binding = self.env['eh.shopify.product'].sudo().search([('store_id', '=', store.id), ('shopify_gid', '=', gid)])
        if not binding:
            return {'ignored': 'the product is not known in Odoo'}
        binding._mark_removed()
        return {'removed': gid}

    def _on_bulk_finish(self):
        payload = self.payload or {}
        gid = payload.get('admin_graphql_api_id')
        if not gid:
            return {'skipped': 'no bulk operation id in payload'}
        bulk = self.env['eh.shopify.bulk'].sudo().search([('store_id', '=', self.store_id.id),
                                                          ('operation_gid', '=', gid), ('state', '=', 'running')], limit=1)
        if not bulk:
            return {'ignored': 'the export is not waiting in Odoo'}
        bulk._enqueue_poll()
        return {'polling': gid}

    def _on_refund_create(self):
        payload = self.payload or {}
        store = self.store_id
        if not store.orders_enabled:
            return {'ignored': 'order import is switched off'}
        gid = payload.get('admin_graphql_api_id') or ('gid://shopify/Refund/%s' % payload['id'] if payload.get('id')
                                                     else None)
        if not gid or not payload.get('order_id'):
            return {'skipped': 'no refund or order id in payload'}
        order_gid = 'gid://shopify/Order/%s' % payload['order_id']
        Order = self.env['eh.shopify.order'].sudo()
        if Order.search_count([('store_id', '=', store.id), ('shopify_gid', '=', order_gid), ('sale_order_id', '!=', False)]):
            self.env['eh.shopify.refund'].sudo()._enqueue_refund_import(store, gid, order_gid, priority=4)
        else:
            Order._enqueue_import(store, order_gid, priority=4)
        return {'queued': gid}

    def _on_return_change(self):
        payload = self.payload or {}
        store = self.store_id
        if not store.orders_enabled:
            return {'ignored': 'order import is switched off'}
        gid = payload.get('admin_graphql_api_id') or ('gid://shopify/Return/%s' % payload['id'] if payload.get('id')
                                                     else None)
        if not gid:
            return {'skipped': 'no return id in payload'}
        order = payload.get('order') or {}
        order_gid = order.get('admin_graphql_api_id') or (
            'gid://shopify/Order/%s' % (order.get('id') or payload.get('order_id'))
            if (order.get('id') or payload.get('order_id')) else None)
        self.env['eh.shopify.return'].sudo()._enqueue_return_sync(store, gid, order_gid)
        return {'queued': gid}

    def _resource_gid(self, kind):
        payload = self.payload or {}
        return payload.get('admin_graphql_api_id') or (
            'gid://shopify/%s/%s' % (kind, payload['id']) if payload.get('id') else None)

    def _on_customer_change(self):
        gid = self._resource_gid('Customer')
        if not gid:
            return {'skipped': 'no customer id in payload'}
        Customer = self.env['eh.shopify.customer'].sudo()
        if not self.store_id.customers_sync_all and not Customer._binding(self.store_id, gid):
            return {'skipped': 'the customer is not linked in Odoo'}
        Customer._enqueue_sync(self.store_id, gid)
        return {'queued': gid}

    def _on_customer_delete(self):
        gid = self._resource_gid('Customer')
        if not gid:
            return {'skipped': 'no customer id in payload'}
        self.env['eh.shopify.customer']._binding(self.store_id, gid)._mark_removed()
        return {'removed': gid}

    def _on_customer_merge(self):
        payload = self.payload or {}
        kept = payload.get('admin_graphql_api_customer_kept_id')
        deleted = payload.get('admin_graphql_api_customer_deleted_id')
        if not kept or not deleted or (payload.get('status') or 'COMPLETED').upper() != 'COMPLETED':
            return {'skipped': 'the merge did not complete'}
        Customer = self.env['eh.shopify.customer'].sudo()
        store = self.store_id
        old = Customer.search([('store_id', '=', store.id), ('shopify_gid', '=', deleted)], limit=1)
        new = Customer.search([('store_id', '=', store.id), ('shopify_gid', '=', kept)], limit=1)
        if old and not new:
            try:
                with self.env.cr.savepoint():
                    old.write({'shopify_gid': kept, 'removed_in_shopify': False})
            except IntegrityError:
                # The kept customer was linked meanwhile: retire the old link instead.
                old.invalidate_recordset()
                old._mark_removed()
        elif old:
            old._mark_removed()
        Customer._enqueue_sync(store, kept)
        return {'merged': kept}

    def _on_company_change(self):
        gid = self._resource_gid('Company')
        if not gid:
            return {'skipped': 'no company id in payload'}
        self.env['eh.shopify.customer'].sudo()._enqueue_sync(self.store_id, gid, kind='company')
        return {'queued': gid}

    def _on_company_delete(self):
        gid = self._resource_gid('Company')
        if not gid:
            return {'skipped': 'no company id in payload'}
        self.env['eh.shopify.customer']._binding(self.store_id, gid)._mark_removed()
        return {'removed': gid}

    def _compliance_todo(self, summary, note, partner=None):
        target = partner if partner else self.store_id
        target.sudo().activity_schedule('mail.mail_activity_data_todo', summary=summary, note=note,
                                        user_id=(self.store_id.order_user_id or self.env.user).id)
        self.note = note

    def _customer_of_request(self):
        payload = self.payload or {}
        customer = payload.get('customer') or {}
        gid = customer.get('admin_graphql_api_id') or self._resource_gid('Customer')
        binding = self.env['eh.shopify.customer'].sudo()._binding(self.store_id, gid) if gid else False
        return gid, (binding or self.env['eh.shopify.customer'].sudo().browse())

    def _on_customer_data_request(self):
        """A customer asked Shopify for a copy of what is held about them."""
        gid, binding = self._customer_of_request()
        orders = (self.payload or {}).get('orders_requested') or []
        name = binding.partner_id.display_name or ((self.payload or {}).get('customer') or {}).get('email') or gid
        self._compliance_todo(_('Shopify data request'), _(
            'A customer asked Shopify for a copy of their personal data: %(customer)s. Send what Odoo holds about '
            'them within 30 days. Orders named in the request: %(orders)s.') % {
                'customer': name or _('a customer of this store'),
                'orders': ', '.join(str(order) for order in orders) or _('none')},
            binding.partner_id)
        self.payload = {'handled': 'customers/data_request'}
        return {'data_request': gid or True}

    def _on_customer_redact(self):
        """Shopify asks for a customer to be erased, 10 days after the last order."""
        gid, binding = self._customer_of_request()
        partner = binding.partner_id
        if binding:
            binding._redact()
        self._compliance_todo(_('Shopify erasure request'), _(
            'Shopify asked for the personal data of %s to be erased. What the connector stored about this customer '
            'was cleared. Remove or anonymise what Odoo still holds, keeping the records the law requires.')
            % (partner.display_name or gid or _('a customer of this store')), partner)
        self.payload = {'handled': 'customers/redact'}
        return {'redacted': gid or True}

    def _on_shop_redact(self):
        """Shopify asks for the shop data to be erased, 48 hours after the app was uninstalled."""
        result = self.store_id.sudo()._redact_store()
        self.payload = {'handled': 'shop/redact'}
        return result

    def _on_dispute_change(self):
        payload = self.payload or {}
        gid = payload.get('admin_graphql_api_id') or (
            'gid://shopify/ShopifyPaymentsDispute/%s' % payload['id'] if payload.get('id') else None)
        if not gid:
            return {'skipped': 'no dispute id in payload'}
        self.env['eh.shopify.dispute'].sudo()._enqueue_sync(self.store_id, gid)
        return {'queued': gid}

    def _on_location_change(self):
        run_at = fields.Datetime.now() + datetime.timedelta(seconds=30)
        self.env['eh.shopify.job'].sudo()._enqueue(self.store_id, 'locations.sync', name=_('Sync locations'),
                                                  dedup_key='locations.sync', priority=6, run_at=run_at)
        return {'queued': 'locations.sync'}
