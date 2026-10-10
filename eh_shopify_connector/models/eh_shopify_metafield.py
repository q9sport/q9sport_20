# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Shopify metafields mapped to Odoo product fields.

A store maps a metafield of a Shopify product or variant, by namespace and key,
to a field of the Odoo product or variant, with a direction. Importing a
product reads only the mapped metafields and writes the converted values on a
product whose variants are all linked to one Odoo product, without sending them
back. Pushing reads Shopify first and sends only the values that differ, at
most 25 per request, each with the compare digest Shopify returned, so a value
changed in Shopify in the meantime is read again instead of overwritten. An
empty Odoo value never clears a Shopify metafield.

Customer and order metafields map to fields of the Odoo contact and sales order
and are imported only, because the connector never sends customers or orders.
"""
import datetime

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import html2plaintext, plaintext2html

from ..lib import documents, errors
from .eh_shopify_product import FROM_SHOPIFY

TYPES = [
    ('single_line_text_field', 'Single line text'),
    ('multi_line_text_field', 'Multi line text'),
    ('number_integer', 'Integer'),
    ('number_decimal', 'Decimal'),
    ('boolean', 'True or false'),
    ('date', 'Date'),
    ('date_time', 'Date and time'),
    ('url', 'URL'),
    ('color', 'Color'),
    ('json', 'JSON'),
]
COMPATIBLE = {
    'single_line_text_field': ('char', 'text', 'html', 'selection'),
    'multi_line_text_field': ('text', 'char', 'html'),
    'url': ('char', 'text'),
    'color': ('char',),
    'json': ('text', 'char'),
    'number_integer': ('integer', 'float', 'monetary'),
    'number_decimal': ('float', 'monetary', 'integer'),
    'boolean': ('boolean',),
    'date': ('date',),
    'date_time': ('datetime',),
}
MODELS = {'product': 'product.template', 'variant': 'product.product', 'customer': 'res.partner',
          'order': 'sale.order'}
IMPORT_ONLY = ('customer', 'order')
BATCH = 25
IDS_PER_REQUEST = 250


class ShopifyMetafieldMap(models.Model):
    _name = 'eh.shopify.metafield.map'
    _description = 'Shopify Metafield Mapping'
    _order = 'store_id, owner_type, namespace, key'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    owner_type = fields.Selection([('product', 'Product'), ('variant', 'Variant'), ('customer', 'Customer'),
                                   ('order', 'Order')], string='Shopify owner',
                                  required=True, default='product')
    namespace = fields.Char(required=True, default='custom')
    key = fields.Char(required=True)
    metafield_type = fields.Selection(TYPES, 'Shopify type', required=True, default='single_line_text_field')
    model_name = fields.Char(compute='_compute_model_name')
    field_id = fields.Many2one('ir.model.fields', 'Odoo field', required=True, ondelete='cascade')
    direction = fields.Selection([('import', 'Shopify to Odoo'), ('export', 'Odoo to Shopify'), ('both', 'Both ways')],
                                 required=True, default='import')
    active = fields.Boolean(default=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_metafield_map_uniq "
                            "ON eh_shopify_metafield_map (store_id, owner_type, namespace, key)")

    @api.depends('owner_type')
    def _compute_model_name(self):
        for mapping in self:
            mapping.model_name = MODELS.get(mapping.owner_type)

    @api.constrains('owner_type', 'metafield_type', 'field_id', 'namespace', 'key', 'direction')
    def _check_mapping(self):
        for mapping in self:
            if mapping.field_id.model != MODELS[mapping.owner_type]:
                raise ValidationError(_('A %(owner)s metafield maps to a field of %(model)s.') % {
                    'owner': mapping.owner_type, 'model': MODELS[mapping.owner_type]})
            if mapping.field_id.ttype not in COMPATIBLE[mapping.metafield_type]:
                raise ValidationError(_('A %(type)s metafield cannot fill the %(ttype)s field %(field)s.') % {
                    'type': dict(TYPES)[mapping.metafield_type], 'ttype': mapping.field_id.ttype,
                    'field': mapping.field_id.field_description})
            if mapping.owner_type in IMPORT_ONLY and mapping.direction != 'import':
                raise ValidationError(_('Customer and order metafields can only be imported from Shopify.'))
            for part in (mapping.namespace, mapping.key):
                if not part or '.' in part or ' ' in part.strip():
                    raise ValidationError(_('Namespace and key are single words without dots, for example custom and '
                                            'care_instructions.'))

    def _full_key(self):
        self.ensure_one()
        return '%s.%s' % (self.namespace.strip(), self.key.strip())

    # ------------------------------------------------------------------ conversion
    def _odoo_value(self, value):
        """Odoo value for a Shopify metafield value. Raises ValueError when it does not convert."""
        self.ensure_one()
        field = self.field_id
        ttype = field.ttype
        if value is None:
            return False
        if ttype == 'boolean':
            return str(value).strip().lower() == 'true'
        if ttype == 'integer':
            return int(round(float(value)))
        if ttype in ('float', 'monetary'):
            return float(value)
        if ttype == 'date':
            return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
        if ttype == 'datetime':
            return datetime.datetime.strptime(str(value)[:19], '%Y-%m-%dT%H:%M:%S')
        if ttype == 'html':
            return plaintext2html(str(value))
        if ttype == 'selection':
            options = dict(self.env[field.model]._fields[field.name]._description_selection(self.env))
            if value not in options:
                raise ValueError(value)
        return str(value)

    def _shopify_value(self, record):
        """Shopify value for the Odoo field of ``record``, or None when it is empty."""
        self.ensure_one()
        ttype = self.field_id.ttype
        value = record[self.field_id.name]
        if ttype == 'boolean':
            return 'true' if value else 'false'
        if value in (False, None, ''):
            return None
        if ttype in ('float', 'monetary', 'integer'):
            if self.metafield_type == 'number_integer':
                return str(int(round(value)))
            text = format(float(value), 'f').rstrip('0').rstrip('.')
            return text or '0'
        if ttype == 'date':
            return fields.Date.to_string(value)
        if ttype == 'datetime':
            return value.strftime('%Y-%m-%dT%H:%M:%SZ')
        if ttype == 'html':
            return html2plaintext(value).strip() or None
        return str(value)

    def _differs(self, record, shopify_value):
        self.ensure_one()
        try:
            theirs = self._odoo_value(shopify_value)
        except (ValueError, TypeError):
            return True
        ours = record[self.field_id.name]
        if self.field_id.ttype in ('float', 'monetary'):
            return abs((ours or 0.0) - (theirs or 0.0)) > 1e-6
        if self.field_id.ttype == 'html':
            return html2plaintext(ours or '').strip() != html2plaintext(theirs or '').strip()
        return (ours or False) != (theirs or False)

    @api.model
    def _exported_field_names(self, model):
        return set(self.sudo().search([('active', '=', True), ('direction', 'in', ('export', 'both')),
                                       ('field_id.model', '=', model)]).mapped('field_id.name'))


class ShopifyStoreMetafields(models.Model):
    _inherit = 'eh.shopify.store'

    metafield_map_ids = fields.One2many('eh.shopify.metafield.map', 'store_id', string='Metafield mappings')

    def _metafield_maps(self, owner_type, directions):
        self.ensure_one()
        return self.metafield_map_ids.filtered(
            lambda mapping: mapping.active and mapping.owner_type == owner_type and mapping.direction in directions)

    def _exports_metafields(self):
        self.ensure_one()
        return bool(self.metafield_map_ids.filtered(lambda m: m.active and m.direction in ('export', 'both')))

    def _pushes_products(self):
        return super()._pushes_products() or self._exports_metafields()


class ShopifyProductMetafields(models.Model):
    _inherit = 'eh.shopify.product'

    def _metafield_links(self):
        return {variant.shopify_gid: variant.product_id for variant in self.variant_ids
                if variant.product_id and variant.active_in_shopify}

    def _read_metafields(self, client, product_maps, variant_maps, variant_gids):
        store = self.store_id
        found = {}
        if product_maps:
            data = client.execute(store._doc_text(documents.PRODUCT_METAFIELDS),
                                  {'id': self.shopify_gid, 'keys': [m._full_key() for m in product_maps]}).data or {}
            nodes = (((data.get('product') or {}).get('metafields') or {}).get('nodes')) or []
            found[self.shopify_gid] = {'%s.%s' % (n['namespace'], n['key']): n for n in nodes}
        if variant_maps and variant_gids:
            keys = [m._full_key() for m in variant_maps]
            for start in range(0, len(variant_gids), IDS_PER_REQUEST):
                data = client.execute(store._doc_text(documents.VARIANT_METAFIELDS),
                                      {'ids': variant_gids[start:start + IDS_PER_REQUEST], 'keys': keys}).data or {}
                for node in data.get('nodes') or []:
                    if node and node.get('id'):
                        found[node['id']] = {'%s.%s' % (n['namespace'], n['key']): n
                                             for n in ((node.get('metafields') or {}).get('nodes') or [])}
        return found

    # ------------------------------------------------------------------ import
    def _link_variants(self, product):
        result = super()._link_variants(product)
        store = self.store_id
        product_maps = store._metafield_maps('product', ('import', 'both'))
        variant_maps = store._metafield_maps('variant', ('import', 'both'))
        if product_maps or variant_maps:
            self.with_context(**{FROM_SHOPIFY: store.id})._pull_metafields(product_maps, variant_maps)
        return result

    def _metafield_vals(self, maps, record, nodes, notes):
        vals = {}
        for mapping in maps:
            node = nodes.get(mapping._full_key())
            if not node or node.get('value') in (None, ''):
                continue
            if node.get('type') and node['type'] != mapping.metafield_type:
                notes.append(_('Metafield %(key)s is a %(type)s in Shopify, not what the mapping expects.') % {
                    'key': mapping._full_key(), 'type': node['type']})
                continue
            try:
                value = mapping._odoo_value(node['value'])
            except (ValueError, TypeError):
                notes.append(_('Metafield %s could not be converted.') % mapping._full_key())
                continue
            if mapping._differs(record, node['value']):
                vals[mapping.field_id.name] = value
        return vals

    def _pull_metafields(self, product_maps, variant_maps):
        store = self.store_id
        template = self._single_template()
        links = self._metafield_links()
        found = self._read_metafields(store._client(), product_maps if template else product_maps.browse(),
                                      variant_maps, list(links))
        notes = []
        if template:
            target = template.sudo().with_context(lang=store._content_lang())
            vals = self._metafield_vals(product_maps, target, found.get(self.shopify_gid, {}), notes)
            if vals:
                target.write(vals)
        for gid, record in links.items():
            vals = self._metafield_vals(variant_maps, record, found.get(gid, {}), notes)
            if vals:
                record.sudo().write(vals)
        if notes:
            self.reason = ' '.join(dict.fromkeys(filter(None, [self.reason] + notes)))

    # ------------------------------------------------------------------ push
    def _job_product_push(self, job):
        result = super()._job_product_push(job)
        if not isinstance(result, dict) or 'skipped' in result or 'removed' in result:
            return result
        binding = self.browse((job.payload or {}).get('binding_id')).exists()
        store = binding.store_id
        product_maps = store._metafield_maps('product', ('export', 'both'))
        variant_maps = store._metafield_maps('variant', ('export', 'both'))
        if product_maps or variant_maps:
            result = dict(result, metafields=binding._push_metafields(product_maps, variant_maps))
        return result

    def _push_metafields(self, product_maps, variant_maps):
        self.ensure_one()
        store = self.store_id
        client = store._client()
        template = self._single_template()
        links = self._metafield_links()
        found = self._read_metafields(client, product_maps if template else product_maps.browse(), variant_maps,
                                      list(links))
        inputs = []

        def collect(maps, owner_gid, record):
            current = found.get(owner_gid, {})
            for mapping in maps:
                value = mapping._shopify_value(record)
                if value is None:
                    continue
                node = current.get(mapping._full_key())
                if node and node.get('value') is not None and not mapping._differs(record, node['value']):
                    continue
                inputs.append({'ownerId': owner_gid, 'namespace': mapping.namespace.strip(), 'key': mapping.key.strip(),
                               'type': mapping.metafield_type, 'value': value,
                               'compareDigest': node.get('compareDigest') if node else None})

        if template:
            collect(product_maps, self.shopify_gid, template.with_context(lang=store._content_lang()))
        for gid, record in links.items():
            collect(variant_maps, gid, record)
        sent = 0
        document = documents.METAFIELDS_SET
        for start in range(0, len(inputs), BATCH):
            batch = inputs[start:start + BATCH]
            try:
                client.mutate(store._doc_text(document), {'metafields': batch}, document.payload_key)
            except errors.UserErrors as error:
                codes = ' '.join(str(problem.get('code') or '') for problem in error.errors).upper()
                if 'STALE' in codes or 'DIGEST' in codes:
                    raise errors.TransientError(_('A mapped metafield changed in Shopify while it was being sent. It '
                                                  'is read again.'), retry_after=30)
                self.reason = _('Shopify refused a metafield: %s') % error.message
                continue
            sent += len(batch)
        return sent


class ProductTemplateMetafields(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        result = super().write(vals)
        if self.env['eh.shopify.metafield.map']._exported_field_names('product.template') & set(vals):
            self.env['eh.shopify.product']._queue_push_for(
                self.with_context(active_test=False).mapped('product_variant_ids'))
        return result


class ProductProductMetafields(models.Model):
    _inherit = 'product.product'

    def write(self, vals):
        result = super().write(vals)
        if self.env['eh.shopify.metafield.map']._exported_field_names('product.product') & set(vals):
            self.env['eh.shopify.product']._queue_push_for(self)
        return result
