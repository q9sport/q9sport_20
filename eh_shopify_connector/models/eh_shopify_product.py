# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify products and their link to Odoo.

Each Shopify product gets a binding that mirrors what Shopify holds, and each
variant gets a variant binding. Variants are linked to Odoo products by an
existing link, then the exact internal reference, then the exact barcode.
Two Odoo candidates at any step stop matching for that variant with a reason;
nothing is ever guessed. A product deleted in Shopify is marked removed and
its Odoo products are left untouched.
"""
import datetime
import time

from odoo import _, api, fields, models

from ..lib import documents, errors, util

POLL_OVERLAP = datetime.timedelta(minutes=10)
FIRST_POLL_WINDOW = datetime.timedelta(days=1)
# One poll job queues at most this many products, then hands the rest to a
# follow-up job that resumes from the saved cursor.
POLL_SLICE = 2000
POLL_BUDGET = 40.0
# Products with more variants than this are read through a bulk export, which
# has no time limit, instead of paging variants inside one job.
API_VARIANT_LIMIT = 500
# Context keys. FROM_SHOPIFY holds the id of the store a write came from, so
# that store is not sent its own change back. DEFER_IMAGES moves image
# downloads out of bulk import chunks into their own jobs.
FROM_SHOPIFY = 'eh_shopify_from_shopify'
DEFER_IMAGES = 'eh_shopify_defer_images'
STAMP = '%Y-%m-%dT%H:%M:%SZ'
DEFAULT_OPTION = ('Title', 'Default Title')


def _parse_dt(value):
    if not value:
        return False
    try:
        return datetime.datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except (TypeError, ValueError):
        return False


def _variant_barcode(variant):
    if variant.get('barcode'):
        return variant['barcode'].strip()
    for node in ((variant.get('barcodes') or {}).get('nodes') or []):
        if node.get('value'):
            return node['value'].strip()
    return ''


def _odoo_option_key(variant):
    """Attribute and value names of an Odoo variant, as Shopify options."""
    values = variant.product_template_attribute_value_ids.filtered(
        lambda value: value.attribute_id.create_variant != 'no_variant')
    return tuple(sorted((value.attribute_id.name, value.name) for value in values)) or (DEFAULT_OPTION,)


def _shopify_option_key(options):
    return tuple(sorted((option.get('name'), option.get('value')) for option in options or [])) or (DEFAULT_OPTION,)


class ShopifyProduct(models.Model):
    _name = 'eh.shopify.product'
    _description = 'Shopify Product'
    _order = 'id desc'
    _rec_name = 'title'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    legacy_id = fields.Char('Shopify number', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', 'Odoo product', ondelete='set null', index=True)
    title = fields.Char(readonly=True)
    handle = fields.Char(readonly=True)
    status = fields.Char('Status in Shopify', readonly=True)
    vendor = fields.Char(readonly=True)
    product_type = fields.Char(readonly=True)
    tags = fields.Char(readonly=True)
    description_html = fields.Html(readonly=True, sanitize=True)
    category_name = fields.Char('Category', readonly=True)
    seo_title = fields.Char(readonly=True)
    seo_description = fields.Char(readonly=True)
    has_only_default_variant = fields.Boolean(readonly=True)
    is_gift_card = fields.Boolean(readonly=True)
    variant_count = fields.Integer(readonly=True)
    shopify_updated_at = fields.Datetime('Updated in Shopify', readonly=True)
    fingerprint = fields.Char(readonly=True)
    state = fields.Selection([
        ('linked', 'Linked'),
        ('unmatched', 'Not linked yet'),
        ('ambiguous', 'Needs a decision'),
        ('removed', 'Removed in Shopify'),
    ], readonly=True, index=True)
    reason = fields.Char(readonly=True)
    variant_ids = fields.One2many('eh.shopify.variant', 'product_binding_id', readonly=True)
    removed_at = fields.Datetime('Deleted in Shopify', readonly=True)
    last_seen_export_id = fields.Integer('Last seen in export', readonly=True, index=True, copy=False)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_product_gid_uniq "
                            "ON eh_shopify_product (store_id, shopify_gid)")

    # ------------------------------------------------------------------ triggers
    @api.model
    def _enqueue_product_import(self, store, gid, priority=7):
        return self.env['eh.shopify.job'].sudo()._enqueue(
            store, 'product.import', name=_('Import product %s') % gid.rsplit('/', 1)[-1], payload={'gid': gid},
            dedup_key='product:%s' % gid, resource_key=gid, priority=priority)

    def _job_products_poll(self, job):
        """Queue an import for every product changed since the last check.

        Shopify lists products oldest change first, so the watermark can move
        after every page. A large backlog is split into follow-up jobs that
        resume from the page cursor, so no single job runs out of time and a
        failure never repeats the work already queued."""
        store = job.store_id
        if not store.products_enabled:
            return {'skipped': 'product sync is switched off'}
        payload = job.payload or {}
        Job = self.env['eh.shopify.job'].sudo()
        if not payload.get('after') and Job.search_count([
                ('store_id', '=', store.id), ('job_type', '=', 'products.poll'), ('id', '!=', job.id),
                ('state', 'in', ('pending', 'running')), ('dedup_key', '=like', 'products.poll:%')]):
            return {'skipped': 'an earlier check is still reading changes'}
        now = fields.Datetime.now()
        since = payload.get('since') or ((store.products_polled_until or now - FIRST_POLL_WINDOW)
                                         - POLL_OVERLAP).strftime(STAMP)
        document = documents.PRODUCTS_UPDATED
        client = store._client()
        started = time.monotonic()
        newest = None
        queued = 0
        pages = client.iter_pages(store._doc_text(document), {'query': "updated_at:>='%s'" % since},
                                  document.connection, page_size=min(document.page_size, POLL_SLICE),
                                  after=payload.get('after'))
        for nodes, page_cursor, more in pages:
            for node in nodes:
                self._enqueue_product_import(store, node['id'])
                updated = _parse_dt(node.get('updatedAt'))
                if updated and (not newest or updated > newest):
                    newest = updated
                queued += 1
            if newest:
                store.sudo().products_polled_until = min(newest, now)
            if more and (queued >= POLL_SLICE or time.monotonic() - started > POLL_BUDGET):
                Job._enqueue(store, 'products.poll', name=job.name, payload={'since': since, 'after': page_cursor},
                             dedup_key='products.poll:%s' % page_cursor, priority=job.priority)
                return {'queued': queued, 'more': True}
        store.sudo().products_polled_until = min(newest, now) if newest else now
        return {'queued': queued}

    # ------------------------------------------------------------------ import
    def _fetch_product(self, store, gid, max_variants=None):
        """Read a product with all its variants. With ``max_variants``, a
        product with more variants is returned after the first page only, marked
        ``_too_large``, so the caller can take another path."""
        client = store._client()
        data = client.execute(store._doc_text(documents.PRODUCT), {'id': gid}).data or {}
        product = data.get('product')
        if not product:
            return None
        expected = (product.get('variantsCount') or {}).get('count')
        if max_variants and expected and expected > max_variants:
            product['_too_large'] = True
            return product
        nested = documents.PRODUCT_VARIANTS
        variants = client.complete_connection(product, 'variants', store._doc_text(nested), gid, ('node', 'variants'),
                                              page_size=nested.page_size)
        if expected is not None and expected != len(variants):
            raise errors.IncompleteData(_('Shopify reported %(expected)s variants for %(title)s but %(read)s were '
                                          'read.') % {'expected': expected, 'title': product.get('title'),
                                                      'read': len(variants)})
        product['variants'] = {'nodes': variants}
        return product

    def _job_product_import(self, job):
        store = job.store_id
        gid = (job.payload or {}).get('gid')
        product = self._fetch_product(store, gid, max_variants=API_VARIANT_LIMIT)
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', gid)], limit=1)
        if product is None:
            if binding:
                binding._mark_removed()
            return {'removed': gid}
        if product.get('_too_large'):
            return self.env['eh.shopify.bulk']._start_product_export(store, gid, job.create_date)
        digest = util.fingerprint(product)
        if binding and binding.fingerprint == digest and binding.state == 'linked':
            return {'unchanged': gid}
        binding = self._upsert_product(store, product)
        result = binding._link_variants(product)
        binding.fingerprint = digest
        return result

    def _upsert_product(self, store, product):
        seo = product.get('seo') or {}
        vals = {
            'store_id': store.id, 'shopify_gid': product['id'], 'legacy_id': str(product.get('legacyResourceId') or ''),
            'title': product.get('title'), 'handle': product.get('handle'), 'status': product.get('status'),
            'vendor': product.get('vendor'), 'product_type': product.get('productType'),
            'tags': ', '.join(product.get('tags') or []), 'description_html': product.get('descriptionHtml'),
            'category_name': (product.get('category') or {}).get('fullName'),
            'seo_title': seo.get('title'), 'seo_description': seo.get('description'),
            'has_only_default_variant': bool(product.get('hasOnlyDefaultVariant')),
            'is_gift_card': bool(product.get('isGiftCard')),
            'variant_count': (product.get('variantsCount') or {}).get('count') or 0,
            'shopify_updated_at': _parse_dt(product.get('updatedAt')), 'removed_at': False,
        }
        binding = self.search([('store_id', '=', store.id), ('shopify_gid', '=', product['id'])], limit=1)
        if binding:
            binding.write(vals)
        else:
            binding = self.create(vals)
        return binding

    def _link_variants(self, product):
        self.ensure_one()
        store = self.store_id
        Variant = self.env['eh.shopify.variant']
        linked, unmatched, ambiguous = [], [], []
        # A Shopify product already tied to one Odoo template (published from
        # Odoo, or fully linked before) links variants that have no reference
        # or barcode by their option values within that template.
        by_options = {}
        if self.product_tmpl_id:
            template = self.product_tmpl_id.with_context(lang=store._content_lang())
            by_options = {_odoo_option_key(variant): variant for variant in template.product_variant_ids}
        option_linked = self.env['product.product']
        for node in product['variants']['nodes']:
            item = node.get('inventoryItem') or {}
            weight = (item.get('measurement') or {}).get('weight') or {}
            sku = (node.get('sku') or '').strip()
            barcode = _variant_barcode(node)
            vals = {
                'store_id': store.id, 'shopify_gid': node['id'], 'product_gid': product['id'],
                'product_binding_id': self.id, 'inventory_item_gid': item.get('id'),
                'title': node.get('title') if not product.get('hasOnlyDefaultVariant') else product.get('title'),
                'sku': sku or False, 'barcode': barcode or False, 'active_in_shopify': True,
                'price': float(node.get('price') or 0.0),
                'compare_at_price': float(node.get('compareAtPrice') or 0.0),
                'inventory_policy': node.get('inventoryPolicy'), 'tracked': bool(item.get('tracked')),
                'requires_shipping': item.get('requiresShipping') is not False,
                'weight_value': float(weight.get('value') or 0.0), 'weight_unit': weight.get('unit') or False,
                'hs_code': item.get('harmonizedSystemCode') or False,
                'country_of_origin': item.get('countryCodeOfOrigin') or False,
                'option_values': ' / '.join('%s: %s' % (o.get('name'), o.get('value'))
                                            for o in node.get('selectedOptions') or []) or False,
                'position': node.get('position') or 0,
                'shopify_updated_at': _parse_dt(node.get('updatedAt')),
            }
            binding = Variant.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1)
            product_record, method, problem = Variant._match_catalogue_variant(store, binding, sku, barcode)
            if not product_record and problem == 'unmatched' and by_options:
                candidate = by_options.get(_shopify_option_key(node.get('selectedOptions')))
                if candidate and not Variant.search_count([
                        ('store_id', '=', store.id), ('product_id', '=', candidate.id),
                        ('shopify_gid', '!=', node['id']), ('active_in_shopify', '=', True)]):
                    product_record, method, problem = candidate, 'options', None
                    option_linked |= candidate
            if product_record:
                vals.update({'product_id': product_record.id, 'match_method': method})
                linked.append(binding or node['id'])
            elif problem == 'ambiguous':
                ambiguous.append(sku or barcode or node.get('title'))
            else:
                unmatched.append(sku or barcode or node.get('title'))
            if binding:
                binding.write(vals)
            else:
                Variant.create(vals)
        if option_linked:
            self.env['eh.shopify.store']._mark_inventory_dirty(option_linked)
        known = [node['id'] for node in product['variants']['nodes']]
        Variant.search([('product_binding_id', '=', self.id), ('shopify_gid', 'not in', known)]).write(
            {'active_in_shopify': False})
        state, reason = 'linked', False
        if ambiguous:
            state = 'ambiguous'
            reason = _('Several Odoo products match %s.') % ', '.join(ambiguous[:5])
        elif unmatched:
            state = 'unmatched'
            reason = _('No Odoo product matches %s.') % ', '.join(unmatched[:5])
        self.write({'state': state, 'reason': reason, 'product_tmpl_id': self._single_template().id})
        return {'state': state, 'linked': len(linked), 'unmatched': len(unmatched), 'ambiguous': len(ambiguous)}

    def _single_template(self):
        """The Odoo template behind this Shopify product, only when every variant
        still in Shopify is linked and all of them belong to that one template.
        Product level values (name, image, archiving, base price) never follow a
        partly linked or mixed product."""
        self.ensure_one()
        Template = self.env['product.template']
        variants = self.variant_ids.filtered('active_in_shopify')
        if not variants or any(not variant.product_id for variant in variants):
            return Template
        templates = variants.mapped('product_id.product_tmpl_id')
        return templates if len(templates) == 1 else Template

    def _mark_removed(self):
        self.write({'state': 'removed', 'reason': _('This product was deleted in Shopify. Odoo products are kept.'),
                    'removed_at': fields.Datetime.now()})
        self.mapped('variant_ids').write({'active_in_shopify': False})


class ShopifyVariantCatalogue(models.Model):
    _inherit = 'eh.shopify.variant'

    match_method = fields.Selection(selection_add=[('options', 'Same product and options')],
                                    ondelete={'options': 'set null'})
    product_binding_id = fields.Many2one('eh.shopify.product', 'Shopify product', ondelete='set null', index=True)
    price = fields.Float(readonly=True)
    compare_at_price = fields.Float(readonly=True)
    inventory_policy = fields.Char('When out of stock', readonly=True)
    tracked = fields.Boolean('Stock tracked in Shopify', readonly=True)
    requires_shipping = fields.Boolean(readonly=True)
    weight_value = fields.Float('Weight', readonly=True)
    weight_unit = fields.Char(readonly=True)
    hs_code = fields.Char('HS code', readonly=True)
    country_of_origin = fields.Char(readonly=True)
    option_values = fields.Char('Options', readonly=True)
    position = fields.Integer(readonly=True)

    @api.model
    def _match_catalogue_variant(self, store, binding, sku, barcode):
        """Return (product, method, problem) with problem in (None, 'ambiguous', 'unmatched')."""
        if binding.product_id and binding.product_id.active:
            return binding.product_id, binding.match_method or 'binding', None
        Product = self.env['product.product']
        domain = self._product_domain(store)
        for value, field, method in ((sku, 'default_code', 'sku'), (barcode, 'barcode', 'barcode')):
            if not value:
                continue
            found = Product.search(domain + [(field, '=', value)], limit=2)
            if len(found) > 1:
                return Product, None, 'ambiguous'
            if found:
                return found, method, None
        return Product, None, 'unmatched'
