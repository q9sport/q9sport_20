# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Create Odoo products for Shopify products that Odoo does not have yet.

Only products with no variant linked at all are created, so an existing Odoo
product is never extended behind the user's back. Shopify options become
dynamic attributes and Odoo creates exactly the combinations Shopify has, so a
product with thousands of possible combinations never explodes into all of
them. Prices follow Shopify on Odoo's tax basis, names use the store company's
language, and weights and stock tracking follow Shopify; anything that cannot
be copied safely (a barcode already used by another product, prices in another
currency or that need a pricelist) is left out and explained on the product.
"""
from odoo import Command, _, fields, models
from odoo.tools import html2plaintext

from .. import compat
from ..lib import errors
from .eh_shopify_product import FROM_SHOPIFY, _variant_barcode

WEIGHT_TO_KG = {'KILOGRAMS': 1.0, 'GRAMS': 0.001, 'POUNDS': 0.45359237, 'OUNCES': 0.028349523125}


class ProductAttribute(models.Model):
    _inherit = 'product.attribute'

    eh_shopify_created = fields.Boolean('Created for Shopify', readonly=True, copy=False)


class ShopifyStoreProductCreate(models.Model):
    _inherit = 'eh.shopify.store'

    product_create_missing = fields.Boolean(
        'Create missing products in Odoo', default=False,
        help='Create an Odoo product for a Shopify product when none of its variants matches an Odoo product.')
    product_pricelist_id = fields.Many2one(
        'product.pricelist', 'Pricelist for variant prices', check_company=True,
        help='When variants of a product have different prices in Shopify, each variant gets a fixed price here.')


class ShopifyProductCreate(models.Model):
    _inherit = 'eh.shopify.product'

    def _link_variants(self, product):
        result = super()._link_variants(product)
        if (self.state == 'unmatched' and self.store_id.product_create_missing and not product.get('isGiftCard')
                and not self.variant_ids.filtered(lambda v: v.product_id and v.active_in_shopify)):
            self.with_context(**{FROM_SHOPIFY: self.store_id.id})._create_odoo_product(product)
            result = dict(result, state=self.state, created=self.product_tmpl_id.id)
        return result

    # ------------------------------------------------------------------ attributes
    def _dynamic_attribute(self, name):
        Attribute = self.env['product.attribute'].sudo()
        attribute = Attribute.search([('name', '=ilike', name), ('create_variant', '=', 'dynamic')], limit=1)
        return attribute or Attribute.create({'name': name, 'create_variant': 'dynamic', 'eh_shopify_created': True})

    def _attribute_value(self, attribute, name):
        Value = self.env['product.attribute.value'].sudo()
        value = Value.search([('attribute_id', '=', attribute.id), ('name', '=', name)], limit=1)
        return value or Value.create({'attribute_id': attribute.id, 'name': name})

    # ------------------------------------------------------------------ creation
    def _create_odoo_product(self, product):
        self.ensure_one()
        store = self.store_id
        nodes = sorted(product['variants']['nodes'], key=lambda node: node.get('position') or 0)
        if not nodes:
            return
        tracked = any((node.get('inventoryItem') or {}).get('tracked') for node in nodes)
        shopify_prices = {node['id']: float(node.get('price') or 0.0) for node in nodes}
        vals = {'name': product.get('title') or product['id'], 'sale_ok': True,
                'purchase_ok': True, 'company_id': store.company_id.id,
                'description_sale': html2plaintext(product.get('descriptionHtml') or '') or False}
        vals.update(compat.product_type_vals(self.env, 'storable' if tracked else 'consu'))
        hs_code = next(((node.get('inventoryItem') or {}).get('harmonizedSystemCode') for node in nodes
                        if (node.get('inventoryItem') or {}).get('harmonizedSystemCode')), None)
        if hs_code and 'hs_code' in self.env['product.template']._fields:
            vals['hs_code'] = hs_code
        options = [] if product.get('hasOnlyDefaultVariant') else sorted(
            product.get('options') or [], key=lambda option: option.get('position') or 0)
        attributes, values = {}, {}
        lines = []
        for option in options:
            attribute = self._dynamic_attribute(option['name'])
            attributes[option['name']] = attribute
            names = []
            for node in nodes:
                for selected in node.get('selectedOptions') or []:
                    if selected.get('name') == option['name'] and selected.get('value') not in names:
                        names.append(selected.get('value'))
            for name in names:
                values[(option['name'], name)] = self._attribute_value(attribute, name)
            lines.append(Command.create({'attribute_id': attribute.id, 'value_ids': [
                Command.set([values[(option['name'], name)].id for name in names])]}))
        if lines:
            vals['attribute_line_ids'] = lines
        template = self.env['product.template'].sudo().with_context(lang=store._content_lang()).create(vals)
        Variant = self.env['eh.shopify.variant']
        prices, problems = self._odoo_prices(template, shopify_prices)
        base_price = min(prices.values()) if prices else 0.0
        if prices:
            template.write({'list_price': base_price})
        differing = len({round(price, 2) for price in prices.values()}) > 1
        for node in nodes:
            if lines:
                combination = self.env['product.template.attribute.value']
                for selected in node.get('selectedOptions') or []:
                    value = values.get((selected.get('name'), selected.get('value')))
                    combination |= template.attribute_line_ids.product_template_value_ids.filtered(
                        lambda ptav, value=value: ptav.product_attribute_value_id == value)
                variant = template._create_product_variant(combination)
            else:
                variant = template.product_variant_id
            if not variant:
                raise errors.MappingError(_('Odoo could not create the variant %(variant)s of %(product)s.') % {
                    'variant': node.get('title'), 'product': template.name})
            problems.extend(self._fill_variant(template, variant, node))
            price = prices.get(node['id'], base_price)
            if differing and abs(price - base_price) >= 0.005:
                problems.extend(self._variant_price(template, variant, combination if lines else None, price,
                                                    len(lines)))
            Variant.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])]).write(
                {'product_id': variant.id, 'match_method': 'created'})
        image_note = self._pull_image(product, template)
        if image_note:
            problems.append(image_note)
        self.write({'state': 'linked', 'product_tmpl_id': template.id,
                    'reason': ' '.join(dict.fromkeys(problems)) or False})

    def _odoo_prices(self, template, shopify_prices):
        """Shopify prices on Odoo's tax basis, keyed by variant id, with notes.
        Empty when they cannot be copied."""
        store = self.store_id
        currency = store.company_id.currency_id
        if store.currency_code and currency.name != store.currency_code:
            return {}, [_('Prices were not copied: Shopify sells in %(store)s and the company uses %(company)s.') % {
                'store': store.currency_code, 'company': currency.name}]
        prices = {}
        for gid, price in shopify_prices.items():
            converted = store._odoo_price(template, price)
            if converted is None:
                return {}, [_('Prices were not copied: the taxes of %s both include and exclude the price.') %
                            template.display_name]
            prices[gid] = converted
        return prices, []

    def _fill_variant(self, template, variant, node):
        problems = []
        vals = {}
        sku = (node.get('sku') or '').strip()
        if sku:
            vals['default_code'] = sku
        barcode = _variant_barcode(node)
        if barcode:
            clash = self.env['product.product'].sudo().with_context(active_test=False).search(
                [('barcode', '=', barcode), ('id', '!=', variant.id)], limit=1)
            if clash:
                problems.append(_('Barcode %(barcode)s already belongs to %(product)s, so it was left empty on '
                                  '%(variant)s.') % {'barcode': barcode, 'product': clash.display_name,
                                                     'variant': variant.display_name})
            else:
                vals['barcode'] = barcode
        weight = ((node.get('inventoryItem') or {}).get('measurement') or {}).get('weight') or {}
        if weight.get('value'):
            kilograms = float(weight['value']) * WEIGHT_TO_KG.get(weight.get('unit'), 1.0)
            weight_uom = template._get_weight_uom_id_from_ir_config_parameter()
            kilogram_uom = self.env.ref('uom.product_uom_kgm', raise_if_not_found=False)
            amount = kilograms
            if weight_uom and kilogram_uom and weight_uom != kilogram_uom:
                amount = kilogram_uom._compute_quantity(kilograms, weight_uom, round=False)
            target = variant if 'weight' in variant._fields and not variant._fields['weight'].related else template
            target.write({'weight': amount})
        if vals:
            variant.write(vals)
        return problems

    def _variant_price(self, template, variant, combination, price, line_count):
        base = template.list_price
        if line_count == 1 and combination:
            combination.write({'price_extra': price - base})
            return []
        pricelist = self.store_id.product_pricelist_id
        currency = self.store_id.currency_code
        if pricelist and currency and pricelist.currency_id.name != currency:
            return [_('Variant prices were not copied: the pricelist %(pricelist)s is not in %(currency)s.') % {
                'pricelist': pricelist.display_name, 'currency': currency}]
        if pricelist:
            self.env['product.pricelist.item'].sudo().create({
                'pricelist_id': pricelist.id, 'applied_on': '0_product_variant', 'product_tmpl_id': template.id,
                'product_id': variant.id, 'compute_price': 'fixed', 'fixed_price': price, 'min_quantity': 0})
            return []
        return [_('Variants have different prices in Shopify. Choose a pricelist for variant prices on the store to '
                  'copy them.')]
