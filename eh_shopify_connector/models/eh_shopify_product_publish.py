# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Publish Odoo products that Shopify does not have yet.

A manager picks products and a store; one job per product creates it in
Shopify with productSet and links every variant back by its option values.
productSet has no idempotency key, so before creating, the job looks for a
Shopify product with the same handle or a variant with the same internal
reference and links that one instead, by option values first: a retry after an
unanswered request never creates a duplicate. The image is sent by its own
job, so a slow upload never undoes the links. New products start as drafts
unless the manager chooses to put them on sale at once.
A product with more than 100 variants is created in the background, up to
Shopify's limit of 2048, and its variants are linked by their option values
once Shopify has finished.
"""
import datetime
import re
import unicodedata

from odoo import _, api, fields, models
from odoo.tools import plaintext2html

from .. import compat
from ..lib import documents, errors
from .eh_shopify_product import API_VARIANT_LIMIT, DEFAULT_OPTION, _odoo_option_key, _shopify_option_key

MAX_SYNC_VARIANTS = 100
MAX_VARIANTS = 2048
OPERATION_DELAYS = (5, 15, 30)
MAX_OPERATION_CHECKS = 120


def _handle(name, suffix):
    text = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode('ascii').lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')[:200] or 'product'
    return '%s-%s' % (text, suffix)


def _variant_values(variant):
    return variant.product_template_attribute_value_ids.filtered(
        lambda value: value.attribute_id.create_variant != 'no_variant')


class ShopifyVariantPublish(models.Model):
    _inherit = 'eh.shopify.variant'

    match_method = fields.Selection(selection_add=[('published', 'Published from Odoo')],
                                    ondelete={'published': 'set null'})


class ShopifyProductPublish(models.Model):
    _inherit = 'eh.shopify.product'

    @api.model
    def _enqueue_publish(self, store, template, status):
        return self.env['eh.shopify.job'].sudo()._enqueue(
            store, 'product.publish', name=_('Publish %s to Shopify') % template.display_name,
            payload={'template_id': template.id, 'status': status}, dedup_key='product.publish:%s' % template.id,
            resource_key='template:%s' % template.id, priority=6, record=template)

    def _enqueue_publish_check(self, store, template, operation_id, status, step):
        delay = OPERATION_DELAYS[min(step, len(OPERATION_DELAYS)) - 1]
        return self.env['eh.shopify.job'].sudo()._enqueue(
            store, 'product.publish.check', name=_('Check the Shopify product created for %s') % template.display_name,
            payload={'template_id': template.id, 'operation_id': operation_id, 'status': status, 'step': step},
            dedup_key='product.publish.check:%s:%s' % (template.id, step), resource_key='template:%s' % template.id,
            priority=6, record=template, run_at=fields.Datetime.now() + datetime.timedelta(seconds=delay))

    def _job_product_publish(self, job):
        store = job.store_id
        payload = job.payload or {}
        template = self.env['product.template'].browse(payload.get('template_id')).exists()
        if not template:
            return {'skipped': 'the product was removed'}
        template = template.with_context(lang=store._content_lang())
        variants = template.product_variant_ids
        if self.env['eh.shopify.variant'].search_count([('store_id', '=', store.id), ('product_id', 'in', variants.ids),
                                                        ('active_in_shopify', '=', True)]):
            return {'skipped': 'already linked to a Shopify product'}
        if self.env['eh.shopify.job'].sudo().search_count([
                ('store_id', '=', store.id), ('job_type', '=', 'product.publish.check'),
                ('res_model', '=', 'product.template'), ('res_id', '=', template.id),
                ('state', 'in', ('pending', 'running'))]):
            return {'skipped': 'Shopify is still creating this product'}
        if len(variants) > MAX_VARIANTS:
            raise errors.MappingError(_(
                '%(product)s has %(count)s variants, more than the %(max)s Shopify allows per product. Split it into '
                'several products first.') % {'product': template.display_name, 'count': len(variants),
                                               'max': MAX_VARIANTS})
        client = store._client()
        handle = _handle(template.name, template.id)
        existing = self._find_existing(store, client, handle, variants)
        if existing:
            found = self._fetch_product(store, existing, max_variants=API_VARIANT_LIMIT)
            linked = 0
            if found and not found.get('_too_large'):
                linked = self._link_published(store, found, variants)
            self._enqueue_product_import(store, existing, priority=5)
            return {'linked_existing': existing, 'variants': linked}
        status = payload.get('status') or 'DRAFT'
        product_input = self._publish_input(store, template, variants, handle, status)
        document = documents.PRODUCT_SET_ASYNC if len(variants) > MAX_SYNC_VARIANTS else documents.PRODUCT_SET
        try:
            result = client.mutate(store._doc_text(document), {'input': product_input}, document.payload_key)
        except errors.UserErrors as error:
            raise errors.MappingError(_('Shopify refused to create %(product)s: %(error)s') % {
                'product': template.display_name, 'error': error.message})
        if document is documents.PRODUCT_SET:
            return self._finish_publish(store, template, result.get('product') or {}, status, variants)
        operation = result.get('productSetOperation') or {}
        if not operation.get('id'):
            raise errors.MappingError(_('Shopify accepted %s but returned no operation to follow. Check the product in '
                                        'Shopify, then publish again to link it.') % template.display_name)
        self._enqueue_publish_check(store, template, operation['id'], status, 1)
        return {'operation': operation['id']}

    def _job_product_publish_check(self, job):
        """Follow a product Shopify creates in the background, then link it."""
        store = job.store_id
        payload = job.payload or {}
        template = self.env['product.template'].browse(payload.get('template_id')).exists()
        data = store._client().execute(store._doc_text(documents.PRODUCT_OPERATION),
                                       {'id': payload.get('operation_id')}).data or {}
        operation = data.get('productOperation') or {}
        name = template.display_name if template else payload.get('operation_id')
        problems = operation.get('userErrors') or []
        product = operation.get('product') or {}
        if problems and not product.get('id'):
            message = '; '.join(problem.get('message') or problem.get('code') or '' for problem in problems)
            raise errors.MappingError(_('Shopify refused to create %(product)s: %(error)s') % {
                'product': name, 'error': message})
        if not operation:
            raise errors.MappingError(_('Shopify no longer knows the operation creating %s. Check the product in '
                                        'Shopify, then publish again to link it.') % name)
        if operation.get('status') != 'COMPLETE' or not product.get('id'):
            step = int(payload.get('step') or 1)
            if step >= MAX_OPERATION_CHECKS:
                raise errors.MappingError(_('Shopify did not finish creating %s in time. Check the product in Shopify, '
                                            'then publish again to link it.') % name)
            if template:
                self._enqueue_publish_check(store, template, payload.get('operation_id'), payload.get('status'),
                                            step + 1)
            return {'status': operation.get('status')}
        if not template:
            return {'created_without_odoo_product': product['id']}
        template = template.with_context(lang=store._content_lang())
        return self._finish_publish(store, template, product, payload.get('status') or 'DRAFT',
                                    template.product_variant_ids)

    def _finish_publish(self, store, template, product, status, variants):
        linked = self._link_published(store, product, variants)
        binding = self._upsert_product(store, {'id': product['id'], 'handle': product.get('handle'),
                                               'title': template.name, 'status': status})
        binding.product_tmpl_id = template
        if template.image_1920:
            self.env['eh.shopify.job'].sudo()._enqueue(
                store, 'product.image', name=_('Send the image of %s to Shopify') % template.display_name,
                payload={'binding_id': binding.id, 'direction': 'push'}, dedup_key='product.image.push:%s' % binding.id,
                resource_key=product['id'], priority=6)
        self._enqueue_product_import(store, product['id'], priority=5)
        if linked:
            self.env['eh.shopify.store']._mark_inventory_dirty(variants)
        template.message_post(body=_('Published to Shopify store %(store)s.') % {'store': store.name})
        return {'product': product.get('id'), 'variants': linked}

    def _find_existing(self, store, client, handle, variants):
        data = client.execute(store._doc_text(documents.PRODUCT_BY_HANDLE), {'handle': handle}).data or {}
        found = (data.get('productByIdentifier') or {}).get('id')
        if found:
            return found
        skus = [variant.default_code.strip() for variant in variants if (variant.default_code or '').strip()]
        for sku in skus[:5]:
            data = client.execute(store._doc_text(documents.VARIANTS_BY_SKU),
                                  {'query': 'sku:"%s"' % sku.replace('"', '\\"')}).data or {}
            for node in (data.get('productVariants') or {}).get('nodes') or []:
                if (node.get('sku') or '') == sku and (node.get('product') or {}).get('id'):
                    return node['product']['id']
        return None

    def _kilograms(self, record):
        weight_uom = record.product_tmpl_id._get_weight_uom_id_from_ir_config_parameter()
        kilogram_uom = self.env.ref('uom.product_uom_kgm', raise_if_not_found=False)
        if weight_uom and kilogram_uom and weight_uom != kilogram_uom:
            return weight_uom._compute_quantity(record.weight, kilogram_uom, round=False)
        return record.weight

    def _publish_input(self, store, template, variants, handle, status):
        options = []
        for line in template.attribute_line_ids.filtered(lambda l: l.attribute_id.create_variant != 'no_variant'):
            used = variants.mapped('product_template_attribute_value_ids').filtered(
                lambda value, line=line: value.attribute_line_id == line)
            if used:
                options.append({'name': line.attribute_id.name, 'values': [{'name': value.name} for value in used]})
        if not options:
            options = [{'name': DEFAULT_OPTION[0], 'values': [{'name': DEFAULT_OPTION[1]}]}]
        hs_code = template.hs_code if 'hs_code' in template._fields else False
        entries = []
        for position, variant in enumerate(variants, 1):
            values = _variant_values(variant)
            entry = {'position': position, 'inventoryPolicy': 'DENY', 'optionValues': [
                {'optionName': value.attribute_id.name, 'name': value.name} for value in values
            ] or [{'optionName': DEFAULT_OPTION[0], 'name': DEFAULT_OPTION[1]}]}
            price = store._shopify_price(variant)
            if price is None:
                raise errors.MappingError(_(
                    '%(product)s has no price in the store currency %(currency)s. Choose a pricelist in %(currency)s '
                    'for variant prices on the store, then retry.') % {'product': template.display_name,
                                                                        'currency': store.currency_code})
            entry['price'] = '%.2f' % price
            item = {'tracked': bool(compat.is_storable(variant))}
            if (variant.default_code or '').strip():
                item['sku'] = variant.default_code.strip()
            if variant.weight:
                item['measurement'] = {'weight': {'value': round(self._kilograms(variant), 4), 'unit': 'KILOGRAMS'}}
            if hs_code:
                item['harmonizedSystemCode'] = hs_code
            entry['inventoryItem'] = item
            if (variant.barcode or '').strip():
                entry.update(self._barcode_input(store.api_version, variant.barcode.strip()))
            entries.append(entry)
        product_input = {'title': template.name, 'handle': handle, 'status': status, 'productOptions': options,
                         'variants': entries}
        if template.description_sale:
            product_input['descriptionHtml'] = plaintext2html(template.description_sale)
        return product_input

    def _link_published(self, store, product, variants):
        by_values = {_odoo_option_key(variant): variant for variant in variants}
        Variant = self.env['eh.shopify.variant']
        linked = 0
        for node in (product.get('variants') or {}).get('nodes') or []:
            variant = by_values.get(_shopify_option_key(node.get('selectedOptions')))
            if not variant:
                continue
            vals = {'store_id': store.id, 'shopify_gid': node['id'], 'product_gid': product['id'],
                    'inventory_item_gid': (node.get('inventoryItem') or {}).get('id'), 'product_id': variant.id,
                    'sku': node.get('sku') or False, 'match_method': 'published', 'active_in_shopify': True,
                    'title': variant.display_name}
            existing = Variant.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1)
            if existing.product_id and existing.product_id != variant:
                continue
            if existing:
                existing.write(vals)
            else:
                Variant.create(vals)
            linked += 1
        return linked
