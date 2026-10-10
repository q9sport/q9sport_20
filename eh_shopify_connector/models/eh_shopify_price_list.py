# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Odoo pricelists sent to Shopify market and B2B price lists.

A store reads its Shopify price lists and maps each one to an Odoo pricelist in
the same currency, with the tax basis that price list uses. For every linked
variant the job computes the price the Odoo pricelist gives, reads the fixed
prices the Shopify list already holds, and sends only the prices that differ,
in batches, a slice of variants per job. Prices are sent when a mapping or a
rule of a mapped pricelist changes, when a product price changes, once a day
because rules can depend on dates, and on request. A pricelist in another
currency is refused, never converted silently.

A price list can also send a compare at price, the price Shopify shows struck
through, taken from the product sales price or from a second Odoo pricelist in
the same currency, and only when it is above the price sent. With quantity
price breaks on, quantities that cost less in the Odoo pricelist are sent as
Shopify quantity price breaks; switched off, breaks already in Shopify are left
alone.
"""
import datetime

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .. import compat
from ..lib import documents, errors, order_math

BATCH = 100
BREAK_BATCH = 25
BREAKS = 5
SLICE = 2000
PUSH_DELAY = datetime.timedelta(seconds=30)
DAILY = datetime.timedelta(hours=24)
PRICE_FIELDS = {'list_price', 'standard_price', 'lst_price', 'taxes_id'}


def _same_money(left, right):
    if left is None or right is None:
        return left is None and right is None
    return abs(left - right) < 0.005


def _same_breaks(left, right):
    return set(left) == set(right) and all(abs(left[quantity] - right[quantity]) < 0.005 for quantity in left)


class ShopifyPriceList(models.Model):
    _name = 'eh.shopify.price.list'
    _description = 'Shopify Price List'
    _order = 'store_id, name'
    _check_company_auto = True

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    name = fields.Char(readonly=True)
    currency_code = fields.Char('Currency', readonly=True)
    catalog_title = fields.Char('Catalog', readonly=True)
    catalog_status = fields.Char('Catalog status', readonly=True)
    fixed_prices_count = fields.Integer('Fixed prices', readonly=True)
    pricelist_id = fields.Many2one('product.pricelist', 'Odoo pricelist', check_company=True,
                                   help='Prices of this Odoo pricelist are sent to the Shopify price list.')
    compare_at_source = fields.Selection([
        ('none', 'Do not send'),
        ('sales_price', 'The product sales price'),
        ('pricelist', 'Another Odoo pricelist'),
    ], string='Compare at price', default='none', required=True,
        help='Shopify shows this price struck through beside the price of this list. It is sent only where it is above '
             'the price sent.')
    compare_at_pricelist_id = fields.Many2one('product.pricelist', 'Compare at pricelist', check_company=True,
                                              domain="[('currency_id.name', '=', currency_code)]")
    send_quantity_breaks = fields.Boolean(
        'Send quantity price breaks', default=False,
        help='Quantities that cost less in the Odoo pricelist are sent as Shopify quantity price breaks. Switched off, '
             'breaks already in Shopify are left alone.')
    taxes_included = fields.Boolean('Prices include tax',
                                    help='Send prices with tax included, as the markets of this price list show them.')
    active = fields.Boolean(default=True)
    removed_in_shopify = fields.Boolean(readonly=True)
    sent_at = fields.Datetime('Prices last sent', readonly=True)
    note = fields.Char(readonly=True)

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_price_list_gid_uniq "
                            "ON eh_shopify_price_list (store_id, shopify_gid)")

    @api.constrains('pricelist_id', 'compare_at_pricelist_id', 'currency_code')
    def _check_currency(self):
        for price_list in self:
            for pricelist in (price_list.pricelist_id, price_list.compare_at_pricelist_id):
                odoo = pricelist.currency_id.name
                if pricelist and price_list.currency_code and odoo != price_list.currency_code:
                    raise ValidationError(_(
                        'The Shopify price list %(list)s is in %(shopify)s, so it needs an Odoo pricelist in '
                        '%(shopify)s, not %(odoo)s.') % {'list': price_list.name,
                                                         'shopify': price_list.currency_code, 'odoo': odoo})

    def write(self, vals):
        result = super().write(vals)
        if {'pricelist_id', 'taxes_included', 'active', 'compare_at_source', 'compare_at_pricelist_id',
                'send_quantity_breaks'} & set(vals):
            self._enqueue_push()
        return result

    # ------------------------------------------------------------------ queueing
    def _enqueue_push(self):
        Job = self.env['eh.shopify.job'].sudo()
        for price_list in self.sudo().filtered(lambda record: record.active and record.pricelist_id
                                               and not record.removed_in_shopify):
            Job._enqueue_after_running(
                price_list.store_id, 'price_list.push', 'price_list:%s' % price_list.id,
                name=_('Send prices of %s to Shopify') % price_list.name, payload={'price_list_id': price_list.id},
                resource_key=price_list.shopify_gid, priority=8, run_at=fields.Datetime.now() + PUSH_DELAY)

    @api.model
    def _enqueue_for_products(self, products):
        if not products:
            return
        stores = self.env['eh.shopify.variant'].sudo().search([('product_id', 'in', products.ids)]).mapped('store_id')
        if stores:
            self.sudo().search([('store_id', 'in', stores.ids), ('pricelist_id', '!=', False)])._enqueue_push()

    def action_send_now(self):
        self.env['eh.shopify.job']._require_manager()
        self._enqueue_push()
        return True

    # ------------------------------------------------------------------ prices
    def _taxed(self, price, product):
        self.ensure_one()
        currency = self.pricelist_id.currency_id
        taxes = self.store_id._sale_taxes(product)
        if not taxes:
            return currency.round(price)
        result = taxes.compute_all(price, currency, 1.0, product=product)
        return currency.round(result['total_included'] if self.taxes_included else result['total_excluded'])

    def _price_for(self, product, quantity=1.0):
        """Price of ``product`` at ``quantity`` on this price list's tax basis."""
        self.ensure_one()
        return self._taxed(self.pricelist_id._get_product_price(product, quantity), product)

    def _compare_at_for(self, product, price):
        """The price Shopify strikes through, or None when there is nothing to show."""
        self.ensure_one()
        if self.compare_at_source == 'sales_price':
            before = self._taxed(product.lst_price, product)
        elif self.compare_at_source == 'pricelist' and self.compare_at_pricelist_id:
            before = self._taxed(self.compare_at_pricelist_id._get_product_price(product, 1.0), product)
        else:
            return None
        return before if before - price > 0.005 else None

    def _break_quantities(self):
        """Quantities the Odoo pricelist prices differently, largest lists first."""
        self.ensure_one()
        quantities = {int(item.min_quantity) for item in self.pricelist_id.item_ids
                      if item.min_quantity and item.min_quantity > 1}
        return sorted(quantities)[:BREAKS]

    def _breaks_for(self, product, price, quantities):
        """Quantity breaks worth sending: each one cheaper than the one before."""
        self.ensure_one()
        breaks = {}
        previous = price
        for quantity in quantities:
            tier = self._price_for(product, quantity)
            if previous - tier > 0.005:
                breaks[quantity] = tier
                previous = tier
        return breaks

    def _current_fixed_prices(self, client):
        self.ensure_one()
        store = self.store_id
        document = documents.PRICE_LIST_FIXED_PRICES
        prices = {}
        for nodes, _cursor, _more in client.iter_pages(store._doc_text(document), {'id': self.shopify_gid},
                                                        document.connection, page_size=document.page_size):
            for node in nodes:
                variant = (node.get('variant') or {}).get('id')
                if not variant:
                    continue
                compare = ((node.get('compareAtPrice') or {}).get('amount'))
                breaks = {}
                for item in ((node.get('quantityPriceBreaks') or {}).get('nodes')) or []:
                    quantity = int(item.get('minimumQuantity') or 0)
                    if quantity:
                        breaks[quantity] = float(order_math.dec((item.get('price') or {}).get('amount')))
                prices[variant] = {'price': float(order_math.dec((node.get('price') or {}).get('amount'))),
                                   'compare_at': float(order_math.dec(compare)) if compare else None,
                                   'breaks': breaks}
        return prices

    def _job_price_list_push(self, job):
        payload = job.payload or {}
        price_list = self.browse(payload.get('price_list_id')).exists()
        if not price_list or not price_list.active or not price_list.pricelist_id or price_list.removed_in_shopify:
            return {'skipped': 'the price list is not mapped'}
        store = price_list.store_id
        if price_list.pricelist_id.currency_id.name != price_list.currency_code:
            price_list.note = _('The Odoo pricelist is in another currency, so nothing was sent.')
            return {'skipped': 'the currencies differ'}
        client = store._client()
        try:
            current = price_list._current_fixed_prices(client)
        except errors.NotFound:
            price_list.write({'removed_in_shopify': True, 'note': _('Shopify no longer has this price list.')})
            return {'removed': price_list.shopify_gid}
        after = int(payload.get('after_id') or 0)
        variants = self.env['eh.shopify.variant'].sudo().search([
            ('store_id', '=', store.id), ('product_id', '!=', False), ('active_in_shopify', '=', True),
            ('id', '>', after)], order='id', limit=SLICE + 1)
        more = len(variants) > SLICE
        variants = variants[:SLICE]
        quantities = price_list._break_quantities() if price_list.send_quantity_breaks else []
        currency_code = price_list.currency_code
        changes, break_plan = [], []
        for variant in variants:
            if variant.product_binding_id.state == 'removed':
                continue
            known = current.get(variant.shopify_gid) or {}
            price = price_list._price_for(variant.product_id)
            compare_at = price_list._compare_at_for(variant.product_id, price)
            unchanged = known and abs(known['price'] - price) < 0.005 and _same_money(known.get('compare_at'), compare_at)
            if not unchanged:
                entry = {'variantId': variant.shopify_gid,
                         'price': {'amount': '%.2f' % price, 'currencyCode': currency_code}}
                if compare_at is not None:
                    entry['compareAtPrice'] = {'amount': '%.2f' % compare_at, 'currencyCode': currency_code}
                changes.append(entry)
            if price_list.send_quantity_breaks:
                # With breaks on, Odoo owns them: no tier left in Odoo means the Shopify breaks go.
                breaks = price_list._breaks_for(variant.product_id, price, quantities) if quantities else {}
                if not _same_breaks(known.get('breaks') or {}, breaks):
                    break_plan.append((variant.shopify_gid, breaks))
        sent, refused = 0, []
        document = documents.PRICE_LIST_FIXED_PRICES_UPDATE
        for start in range(0, len(changes), BATCH):
            batch = changes[start:start + BATCH]
            try:
                client.mutate(store._doc_text(document), {'priceListId': price_list.shopify_gid, 'pricesToAdd': batch,
                                                          'variantIdsToDelete': []}, document.payload_key)
                sent += len(batch)
            except errors.UserErrors as error:
                refused.append(error.message)
        breaks_sent = 0
        document = documents.QUANTITY_PRICING_UPDATE
        for start in range(0, len(break_plan), BREAK_BATCH):
            batch = break_plan[start:start + BREAK_BATCH]
            # Shopify keeps the breaks it holds, so the ones of these variants go first. Every list of the input
            # has to be present, even empty.
            data = {'quantityPriceBreaksToDeleteByVariantId': [gid for gid, _breaks in batch],
                    'quantityPriceBreaksToAdd': [
                        {'variantId': gid, 'minimumQuantity': quantity,
                         'price': {'amount': '%.2f' % tier, 'currencyCode': currency_code}}
                        for gid, breaks in batch for quantity, tier in sorted(breaks.items())],
                    'quantityPriceBreaksToDelete': [], 'quantityRulesToAdd': [],
                    'quantityRulesToDeleteByVariantId': [], 'pricesToAdd': [], 'pricesToDeleteByVariantId': []}
            try:
                client.mutate(store._doc_text(document), {'priceListId': price_list.shopify_gid, 'input': data},
                              document.payload_key)
                breaks_sent += len(batch)
            except errors.UserErrors as error:
                refused.append(error.message)
        if more:
            self.env['eh.shopify.job'].sudo()._enqueue(
                store, 'price_list.push', name=job.name, payload={'price_list_id': price_list.id,
                                                                  'after_id': variants[-1].id},
                dedup_key='price_list:%s:%s' % (price_list.id, variants[-1].id), resource_key=price_list.shopify_gid,
                priority=8)
        vals = {'note': _('Shopify refused some prices: %s') % ' '.join(dict.fromkeys(refused)) if refused else False}
        if not more:
            vals['sent_at'] = fields.Datetime.now()
        super(ShopifyPriceList, price_list).write(vals)
        return {'sent': sent, 'breaks': breaks_sent, 'unchanged': len(variants) - len(changes),
                'refused': len(refused), 'more': more}


class ShopifyStorePriceLists(models.Model):
    _inherit = 'eh.shopify.store'

    price_list_ids = fields.One2many('eh.shopify.price.list', 'store_id', string='Shopify price lists')
    price_lists_synced_at = fields.Datetime('Price lists last read', readonly=True, copy=False)

    def action_sync_price_lists(self):
        self.ensure_one()
        compat.check_access(self, 'write')
        self.env['eh.shopify.job'].sudo()._enqueue(self, 'price_lists.sync', name=_('Read Shopify price lists'),
                                                   dedup_key='price_lists.sync', priority=6)
        return self._notify(_('Reading price lists'), _('Map each Shopify price list to an Odoo pricelist in the same '
                                                        'currency once they appear.'))

    def action_send_price_lists(self):
        self.ensure_one()
        compat.check_access(self, 'write')
        self.price_list_ids._enqueue_push()
        return self._notify(_('Prices queued'), _('Only prices that differ in Shopify are sent.'))

    def _job_price_lists_sync(self, job):
        store = job.store_id
        document = documents.PRICE_LISTS
        PriceList = self.env['eh.shopify.price.list'].sudo().with_context(active_test=False)
        seen = []
        for nodes, _cursor, _more in store._client().iter_pages(store._doc_text(document), {}, document.connection,
                                                                 page_size=document.page_size):
            for node in nodes:
                catalog = node.get('catalog') or {}
                vals = {'store_id': store.id, 'shopify_gid': node['id'], 'name': node.get('name'),
                        'currency_code': node.get('currency'), 'catalog_title': catalog.get('title') or False,
                        'catalog_status': catalog.get('status') or False,
                        'fixed_prices_count': int(node.get('fixedPricesCount') or 0), 'removed_in_shopify': False}
                record = PriceList.search([('store_id', '=', store.id), ('shopify_gid', '=', node['id'])], limit=1)
                if record.pricelist_id and record.pricelist_id.currency_id.name != node.get('currency'):
                    vals.update(pricelist_id=False, note=_('The price list changed currency in Shopify, so its Odoo '
                                                            'pricelist was unmapped.'))
                if record:
                    super(ShopifyPriceList, record).write(vals)
                else:
                    PriceList.create(vals)
                seen.append(node['id'])
        gone = PriceList.search([('store_id', '=', store.id), ('shopify_gid', 'not in', seen)])
        if gone:
            super(ShopifyPriceList, gone).write({'removed_in_shopify': True,
                                                 'note': _('Shopify no longer has this price list.')})
        store.sudo().price_lists_synced_at = fields.Datetime.now()
        return {'price_lists': len(seen), 'removed': len(gone)}


class PricelistItemPriceLists(models.Model):
    _inherit = 'product.pricelist.item'

    def _eh_shopify_price_lists(self):
        pricelists = self.sudo().mapped('pricelist_id')
        if not pricelists:
            return self.env['eh.shopify.price.list']
        return self.env['eh.shopify.price.list'].sudo().search([('pricelist_id', 'in', pricelists.ids)])

    @api.model_create_multi
    def create(self, vals_list):
        items = super().create(vals_list)
        items._eh_shopify_price_lists()._enqueue_push()
        return items

    def write(self, vals):
        before = self._eh_shopify_price_lists()
        result = super().write(vals)
        (before | self._eh_shopify_price_lists())._enqueue_push()
        return result

    def unlink(self):
        price_lists = self._eh_shopify_price_lists()
        result = super().unlink()
        price_lists._enqueue_push()
        return result


class ProductTemplatePriceLists(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        result = super().write(vals)
        if PRICE_FIELDS & set(vals):
            self.env['eh.shopify.price.list']._enqueue_for_products(
                self.with_context(active_test=False).mapped('product_variant_ids'))
        return result


class ProductProductPriceLists(models.Model):
    _inherit = 'product.product'

    def write(self, vals):
        result = super().write(vals)
        if PRICE_FIELDS & set(vals):
            self.env['eh.shopify.price.list']._enqueue_for_products(self)
        return result
