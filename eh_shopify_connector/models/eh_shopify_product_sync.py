# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Keep linked products in step between Odoo and Shopify.

Each store chooses, separately for product details and for prices, whether
Odoo updates Shopify, Shopify updates Odoo, or both stay separate. Separate is
the default, so linking an existing catalogue never renames anything. A push
reads Shopify first and sends only the values that differ. Descriptions are
never pushed, so a formatted Shopify description is not replaced by plain
text. Product level values (name, image, archiving, base price) only follow a
Shopify product whose variants are all linked to one Odoo product.

Prices follow the tax basis of both sides: a store whose prices include tax
gets tax included prices, whatever Odoo's taxes do, and prices are only copied
between matching currencies. Names are read and written in the language of the
store's company. Writes that come from a store carry that store's id in the
context, so they are never sent back to it but still reach other stores. A
pricelist rule that is not about one product rechecks the store's prices, and
so does a daily check, because rules can depend on dates.
"""
import datetime
import time

from odoo import _, api, fields, models

from .. import compat
from ..lib import documents, errors, versions
from .eh_shopify_product import API_VARIANT_LIMIT, FROM_SHOPIFY, _variant_barcode
from .eh_shopify_product_create import WEIGHT_TO_KG

MASTER = [('off', 'Keep separate'), ('odoo', 'Odoo updates Shopify'), ('shopify', 'Shopify updates Odoo')]
TEMPLATE_FIELDS = {'name', 'active', 'hs_code', 'weight', 'list_price', 'image_1920', 'taxes_id'}
VARIANT_FIELDS = {'default_code', 'barcode', 'weight', 'active', 'lst_price'}
VARIANT_BATCH = 100
PUSH_DELAY = datetime.timedelta(seconds=20)
PRICE_BATCH = 500
PRICE_CHECK_BUDGET = 40.0
PRICE_CHECK_EVERY = datetime.timedelta(hours=24)


class ShopifyStoreSync(models.Model):
    _inherit = 'eh.shopify.store'

    master_content = fields.Selection(MASTER, string='Product details', default='off', required=True,
                                      help='Name, internal reference, barcode, weight and HS code of linked products.')
    master_price = fields.Selection(MASTER, string='Prices', default='off', required=True,
                                    help='Sales price of linked variants. With a pricelist for variant prices, that '
                                         'pricelist is used.')
    prices_checked_at = fields.Datetime('Prices last checked', readonly=True, copy=False)

    def write(self, vals):
        result = super().write(vals)
        if {'master_price', 'product_pricelist_id'} & set(vals):
            self.filtered(lambda store: store.master_price == 'odoo' and store.products_enabled)._queue_price_check()
        return result

    def _pushes_products(self):
        """True when edits in Odoo are sent to this store's products."""
        self.ensure_one()
        return 'odoo' in (self.master_content, self.master_price)

    def _queue_price_check(self):
        Job = self.env['eh.shopify.job'].sudo()
        for store in self:
            Job._enqueue_after_running(store, 'products.price_check', 'products.price_check',
                                       name=_('Check prices sent to Shopify'), priority=8,
                                       run_at=fields.Datetime.now() + PUSH_DELAY)

    def _content_lang(self):
        """Language product names are read and written in for this store."""
        self.ensure_one()
        lang = self.company_id.partner_id.lang
        if lang and lang != 'en_US' and self.env['res.lang'].sudo().search_count([('code', '=', lang)]):
            return lang
        return 'en_US'

    def _sale_taxes(self, product):
        company_ids = compat.company_lookup_ids(self.company_id)
        return product.sudo().taxes_id.filtered(lambda tax: tax.company_id.id in company_ids)

    def _shopify_price(self, product):
        """Price Shopify should show for ``product``: the pricelist price, or the
        sales price, with tax added or removed to match whether the store's
        prices include tax. None when the currencies differ."""
        self.ensure_one()
        pricelist = self.product_pricelist_id
        currency = pricelist.currency_id if pricelist else self.company_id.currency_id
        if self.currency_code and currency.name != self.currency_code:
            return None
        price = pricelist._get_product_price(product, 1.0) if pricelist else product.lst_price
        taxes = self._sale_taxes(product)
        if taxes:
            result = taxes.compute_all(price, currency, 1.0, product=product)
            price = result['total_included'] if self.taxes_included else result['total_excluded']
        return price

    def _odoo_price(self, product, price):
        """Odoo sales price for a Shopify price, following whether the store's
        prices include tax and whether the product's taxes do. None when the
        product mixes taxes that include and exclude the price."""
        self.ensure_one()
        taxes = self._sale_taxes(product)
        if not taxes:
            return price
        included = set(taxes.mapped('price_include'))
        if len(included) > 1:
            return None
        variant = product if product._name == 'product.product' else None
        result = taxes.with_context(force_price_include=bool(self.taxes_included)).compute_all(
            price, self.company_id.currency_id, 1.0, product=variant)
        return result['total_included'] if included.pop() else result['total_excluded']


class ShopifyVariantSync(models.Model):
    _inherit = 'eh.shopify.variant'

    sync_note = fields.Char('Sync note', readonly=True)


class ShopifyProductSync(models.Model):
    _inherit = 'eh.shopify.product'

    # ------------------------------------------------------------------ queueing
    @api.model
    def _enqueue_push(self, binding):
        return self.env['eh.shopify.job'].sudo()._enqueue_after_running(
            binding.store_id, 'product.push', 'product.push:%s' % binding.id,
            name=_('Send %s to Shopify') % (binding.title or binding.shopify_gid), payload={'binding_id': binding.id},
            resource_key=binding.shopify_gid, priority=7, run_at=fields.Datetime.now() + PUSH_DELAY)

    @api.model
    def _queue_push_for(self, products):
        origin = self.env.context.get(FROM_SHOPIFY)
        if not products or origin is True:
            return
        bindings = self.env['eh.shopify.variant'].sudo().search([
            ('product_id', 'in', products.ids), ('product_binding_id', '!=', False)]).mapped('product_binding_id')
        bindings = bindings.filtered(lambda binding: binding.state != 'removed' and binding.store_id.id != origin
                                     and binding.store_id.products_enabled
                                     and binding.store_id.state in ('connected', 'attention')
                                     and binding.store_id._pushes_products())
        for binding in bindings:
            self._enqueue_push(binding)

    # ------------------------------------------------------------------ push
    @staticmethod
    def _barcode_input(api_version, barcode):
        if versions._key(api_version) >= versions._key('2026-10'):
            return {'barcodes': [{'value': barcode}]}
        return {'barcode': barcode}

    def _shopify_weight(self, record, node):
        if not record.weight:
            return None
        weight_uom = record.product_tmpl_id._get_weight_uom_id_from_ir_config_parameter()
        kilogram_uom = self.env.ref('uom.product_uom_kgm', raise_if_not_found=False)
        kilograms = record.weight
        if weight_uom and kilogram_uom and weight_uom != kilogram_uom:
            kilograms = weight_uom._compute_quantity(record.weight, kilogram_uom, round=False)
        current = ((node.get('inventoryItem') or {}).get('measurement') or {}).get('weight') or {}
        unit = current.get('unit') or 'KILOGRAMS'
        value = round(kilograms / WEIGHT_TO_KG.get(unit, 1.0), 4)
        if current.get('value') is not None and abs(float(current['value']) - value) < 0.0005:
            return None
        return {'value': value, 'unit': unit}

    def _variant_inputs(self, store, product, push_content, push_price):
        Variant = self.env['eh.shopify.variant']
        entries = []
        for node in product['variants']['nodes']:
            record = Variant.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1).product_id
            if not record:
                continue
            entry = {'id': node['id']}
            if push_price:
                price = store._shopify_price(record)
                if price is not None and abs(price - float(node.get('price') or 0.0)) >= 0.005:
                    entry['price'] = '%.2f' % price
            if push_content:
                item = {}
                sku = (record.default_code or '').strip()
                if sku and sku != (node.get('sku') or ''):
                    item['sku'] = sku
                barcode = (record.barcode or '').strip()
                if barcode and barcode != _variant_barcode(node):
                    entry.update(self._barcode_input(store.api_version, barcode))
                weight = self._shopify_weight(record, node)
                if weight:
                    item['measurement'] = {'weight': weight}
                hs_code = record.product_tmpl_id.hs_code if 'hs_code' in record.product_tmpl_id._fields else False
                if hs_code and hs_code != (node.get('inventoryItem') or {}).get('harmonizedSystemCode'):
                    item['harmonizedSystemCode'] = hs_code
                if item:
                    entry['inventoryItem'] = item
            if len(entry) > 1:
                entries.append(entry)
        return entries

    @staticmethod
    def _variant_index(problem):
        field = problem.get('field') or []
        for position, part in enumerate(field):
            if part == 'variants' and position + 1 < len(field) and str(field[position + 1]).isdigit():
                return int(field[position + 1])
        return None

    def _product_from_mirror(self, product):
        """Variants of a very large product as last imported, so a push compares
        against them instead of paging thousands of variants inside one job."""
        self.ensure_one()
        nodes = []
        for variant in self.variant_ids.filtered('active_in_shopify'):
            weight = {'value': variant.weight_value, 'unit': variant.weight_unit} if variant.weight_unit else None
            nodes.append({'id': variant.shopify_gid, 'price': '%.2f' % variant.price, 'sku': variant.sku or '',
                          'barcode': variant.barcode or '',
                          'inventoryItem': {'harmonizedSystemCode': variant.hs_code or None,
                                            'measurement': {'weight': weight}}})
        return dict(product, variants={'nodes': nodes})

    def _job_product_push(self, job):
        binding = self.browse((job.payload or {}).get('binding_id')).exists()
        if not binding or binding.state == 'removed':
            return {'skipped': 'the product is not linked'}
        store = binding.store_id
        push_content, push_price = store.master_content == 'odoo', store.master_price == 'odoo'
        if not (push_content or push_price or store._pushes_products()):
            return {'skipped': 'Odoo does not update this store'}
        product = self._fetch_product(store, binding.shopify_gid, max_variants=API_VARIANT_LIMIT)
        if product is None:
            binding._mark_removed()
            return {'removed': binding.shopify_gid}
        if product.get('_too_large'):
            product = binding._product_from_mirror(product)
        client = store._client()
        result = {'product': False, 'variants': 0, 'refused': 0}
        template = binding._single_template().with_context(lang=store._content_lang())
        if push_content and template:
            product_input = {'id': product['id']}
            if template.name and template.name != product.get('title'):
                product_input['title'] = template.name
            if not template.active and product.get('status') != 'ARCHIVED':
                product_input['status'] = 'ARCHIVED'
            if len(product_input) > 1:
                try:
                    client.mutate(store._doc_text(documents.PRODUCT_UPDATE), {'product': product_input},
                                  documents.PRODUCT_UPDATE.payload_key)
                    result['product'] = True
                except errors.UserErrors as error:
                    binding.reason = _('Shopify refused the product update: %s') % error.message
                    result['refused'] += 1
            note = binding._push_image(client, template)
            if note:
                binding.reason = note
        entries = self._variant_inputs(store, product, push_content, push_price)
        document = documents.VARIANTS_BULK_UPDATE
        Variant = self.env['eh.shopify.variant'].sudo()
        for start in range(0, len(entries), VARIANT_BATCH):
            batch = entries[start:start + VARIANT_BATCH]
            refused = {}
            try:
                client.mutate(store._doc_text(document), {'productId': product['id'], 'variants': batch},
                              document.payload_key)
            except errors.UserErrors as error:
                for problem in error.errors:
                    index = self._variant_index(problem)
                    if index is None or index >= len(batch):
                        raise errors.MappingError(_('Shopify refused the variant update of %(title)s: %(error)s') % {
                            'title': binding.title, 'error': error.message})
                    refused[batch[index]['id']] = problem.get('message') or problem.get('code')
            for entry in batch:
                variant = Variant.search([('store_id', '=', store.id), ('shopify_gid', '=', entry['id'])], limit=1)
                if entry['id'] in refused:
                    variant.sync_note = refused[entry['id']][:250]
                    result['refused'] += 1
                    continue
                vals = {'sync_note': False}
                item = entry.get('inventoryItem') or {}
                if 'price' in entry:
                    vals['price'] = float(entry['price'])
                if 'sku' in item:
                    vals['sku'] = item['sku']
                if entry.get('barcode') or entry.get('barcodes'):
                    vals['barcode'] = entry.get('barcode') or entry['barcodes'][0]['value']
                if item.get('measurement'):
                    vals.update({'weight_value': item['measurement']['weight']['value'],
                                 'weight_unit': item['measurement']['weight']['unit']})
                if item.get('harmonizedSystemCode'):
                    vals['hs_code'] = item['harmonizedSystemCode']
                variant.write(vals)
                result['variants'] += 1
        return result

    def _job_products_price_check(self, job):
        """Queue a push for every linked product whose Odoo price no longer
        matches the price last seen in Shopify. Runs in slices by variant."""
        store = job.store_id
        if store.master_price != 'odoo' or not store.products_enabled:
            return {'skipped': 'Odoo does not send prices to this store'}
        after = (job.payload or {}).get('after_id') or 0
        if not after:
            store.sudo().prices_checked_at = fields.Datetime.now()
        Variant = self.env['eh.shopify.variant'].sudo()
        started = time.monotonic()
        stale = self.browse()
        checked = 0
        while True:
            variants = Variant.search([('store_id', '=', store.id), ('id', '>', after), ('product_id', '!=', False),
                                       ('active_in_shopify', '=', True), ('product_binding_id', '!=', False)],
                                      order='id', limit=PRICE_BATCH)
            if not variants:
                break
            for variant in variants:
                price = store._shopify_price(variant.product_id)
                if price is not None and abs(round(price, 2) - variant.price) >= 0.005:
                    stale |= variant.product_binding_id
            checked += len(variants)
            after = variants[-1].id
            if time.monotonic() - started > PRICE_CHECK_BUDGET:
                self.env['eh.shopify.job'].sudo()._enqueue(
                    store, 'products.price_check', name=job.name, payload={'after_id': after},
                    dedup_key='products.price_check:%s' % after, priority=job.priority)
                break
        for binding in stale.filtered(lambda record: record.state != 'removed'):
            self._enqueue_push(binding)
        return {'checked': checked, 'queued': len(stale)}

    # ------------------------------------------------------------------ pull
    def _link_variants(self, product):
        result = super()._link_variants(product)
        store = self.store_id
        if 'shopify' in (store.master_content, store.master_price):
            self.with_context(**{FROM_SHOPIFY: store.id})._apply_shopify_values(product)
        return result

    def _apply_shopify_values(self, product):
        store = self.store_id
        Variant = self.env['eh.shopify.variant']
        Product = self.env['product.product'].sudo().with_context(active_test=False)
        linked = []
        for node in product['variants']['nodes']:
            record = Variant.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1).product_id
            if record:
                linked.append((node, record.sudo()))
        if not linked:
            return
        template = self._single_template().sudo().with_context(lang=store._content_lang())
        notes = []
        if store.master_content == 'shopify':
            for node, record in linked:
                vals = {}
                sku = (node.get('sku') or '').strip()
                if sku and sku != record.default_code:
                    vals['default_code'] = sku
                barcode = _variant_barcode(node)
                if barcode and barcode != record.barcode:
                    if Product.search([('barcode', '=', barcode), ('id', '!=', record.id)], limit=1):
                        notes.append(_('Barcode %s is used by another product in Odoo.') % barcode)
                    else:
                        vals['barcode'] = barcode
                weight = ((node.get('inventoryItem') or {}).get('measurement') or {}).get('weight') or {}
                if weight.get('value'):
                    kilograms = float(weight['value']) * WEIGHT_TO_KG.get(weight.get('unit'), 1.0)
                    weight_uom = record.product_tmpl_id._get_weight_uom_id_from_ir_config_parameter()
                    kilogram_uom = self.env.ref('uom.product_uom_kgm', raise_if_not_found=False)
                    amount = kilograms
                    if weight_uom and kilogram_uom and weight_uom != kilogram_uom:
                        amount = kilogram_uom._compute_quantity(kilograms, weight_uom, round=False)
                    if abs((record.weight or 0.0) - amount) > 1e-6:
                        vals['weight'] = amount
                if vals:
                    record.write(vals)
            if template:
                if product.get('title') and product['title'] != template.name:
                    template.write({'name': product['title']})
                note = self._pull_image(product, template, force=True)
                if note:
                    notes.append(note)
        if store.master_price == 'shopify' and template:
            notes.extend(self._pull_prices(store, template, linked))
        if notes:
            self.reason = ' '.join(dict.fromkeys(notes))

    def _pull_prices(self, store, template, linked):
        currency = store.company_id.currency_id
        if store.currency_code and currency.name != store.currency_code:
            return [_('Prices were not copied: Shopify sells in %(store)s and the company uses %(company)s.') % {
                'store': store.currency_code, 'company': currency.name}]
        prices = {}
        for node, record in linked:
            price = store._odoo_price(record, float(node.get('price') or 0.0))
            if price is None:
                return [_('Prices were not copied: the taxes of %s both include and exclude the price.') %
                        record.display_name]
            prices[record.id] = price
        base = min(prices.values())
        notes = []
        if abs(template.list_price - base) >= 0.005:
            template.write({'list_price': base})
        if len(template.product_variant_ids) > 1:
            for _node, record in linked:
                notes.extend(self._set_variant_price(store, template, record, prices[record.id], base))
        return notes

    def _set_variant_price(self, store, template, record, price, base):
        combination = record.product_template_attribute_value_ids
        if len(template.attribute_line_ids) == 1 and len(combination) == 1:
            if abs(combination.price_extra - (price - base)) >= 0.005:
                combination.sudo().write({'price_extra': price - base})
            return []
        pricelist = store.product_pricelist_id
        if not pricelist:
            return [_('Variants have different prices in Shopify. Choose a pricelist for variant prices on the store '
                      'to copy them.')]
        if store.currency_code and pricelist.currency_id.name != store.currency_code:
            return [_('Variant prices were not copied: the pricelist %(pricelist)s is not in %(currency)s.') % {
                'pricelist': pricelist.display_name, 'currency': store.currency_code}]
        Item = self.env['product.pricelist.item'].sudo()
        item = Item.search([('pricelist_id', '=', pricelist.id), ('applied_on', '=', '0_product_variant'),
                            ('product_id', '=', record.id), ('min_quantity', '<=', 1)], limit=1)
        if item:
            if abs(item.fixed_price - price) >= 0.005:
                item.write({'compute_price': 'fixed', 'fixed_price': price})
        else:
            Item.create({'pricelist_id': pricelist.id, 'applied_on': '0_product_variant', 'product_tmpl_id': template.id,
                         'product_id': record.id, 'compute_price': 'fixed', 'fixed_price': price, 'min_quantity': 0})
        return []


class ProductTemplateShopifySync(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        result = super().write(vals)
        if TEMPLATE_FIELDS & set(vals):
            self.env['eh.shopify.product']._queue_push_for(
                self.with_context(active_test=False).mapped('product_variant_ids'))
        return result


class ProductProductShopifySync(models.Model):
    _inherit = 'product.product'

    def write(self, vals):
        result = super().write(vals)
        if VARIANT_FIELDS & set(vals):
            self.env['eh.shopify.product']._queue_push_for(self)
        return result


class ProductTemplateAttributeValueShopifySync(models.Model):
    _inherit = 'product.template.attribute.value'

    def write(self, vals):
        result = super().write(vals)
        if 'price_extra' in vals:
            self.env['eh.shopify.product']._queue_push_for(
                self.with_context(active_test=False).mapped('product_tmpl_id.product_variant_ids'))
        return result


class PricelistItemShopifySync(models.Model):
    _inherit = 'product.pricelist.item'

    def _eh_shopify_scope(self):
        """Products these rules price for stores where Odoo sends prices, and the
        stores to recheck in full for rules about a category, everything, or
        another pricelist's result."""
        Store = self.env['eh.shopify.store'].sudo()
        products = self.env['product.product']
        if not self or self.env.context.get(FROM_SHOPIFY) is True:
            return products, Store
        stores = Store.search([('product_pricelist_id', 'in', self.sudo().mapped('pricelist_id').ids),
                               ('master_price', '=', 'odoo'), ('products_enabled', '=', True)])
        if not stores:
            return products, Store
        broad = self.filtered(lambda item: item.applied_on not in ('0_product_variant', '1_product'))
        narrow = self - broad
        products = narrow.mapped('product_id') | narrow.filtered(lambda item: not item.product_id).mapped(
            'product_tmpl_id.product_variant_ids')
        return products, (stores if broad else Store)

    def _eh_shopify_notify(self, products, stores):
        origin = self.env.context.get(FROM_SHOPIFY)
        self.env['eh.shopify.product']._queue_push_for(products)
        stores.filtered(lambda store: store.id != origin)._queue_price_check()

    @api.model_create_multi
    def create(self, vals_list):
        items = super().create(vals_list)
        items._eh_shopify_notify(*items._eh_shopify_scope())
        return items

    def write(self, vals):
        before_products, before_stores = self._eh_shopify_scope()
        result = super().write(vals)
        after_products, after_stores = self._eh_shopify_scope()
        self._eh_shopify_notify(before_products | after_products, before_stores | after_stores)
        return result

    def unlink(self):
        products, stores = self._eh_shopify_scope()
        result = super().unlink()
        self.env['product.pricelist.item']._eh_shopify_notify(products, stores)
        return result
