# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import datetime
from unittest.mock import patch

from odoo import fields

from .. import compat
from odoo.tests import tagged

from ..models import eh_shopify_product
from .common import ShopifyCase


def shopify_product(number, variants, title='Brass gear', updated='2026-09-16T08:00:00Z'):
    gid = 'gid://shopify/Product/%s' % number
    nodes = []
    for index, (sku, barcode) in enumerate(variants):
        nodes.append({
            'id': 'gid://shopify/ProductVariant/%s%03d' % (number, index), 'legacyResourceId': '%s%03d' % (number, index),
            'title': 'Size %s' % index, 'sku': sku, 'barcode': barcode, 'price': '%.2f' % (10 + index),
            'compareAtPrice': None, 'position': index + 1, 'taxable': True, 'inventoryPolicy': 'DENY',
            'updatedAt': updated, 'selectedOptions': [{'name': 'Size', 'value': str(index)}],
            'inventoryItem': {'id': 'gid://shopify/InventoryItem/%s%03d' % (number, index), 'tracked': True,
                              'requiresShipping': True, 'harmonizedSystemCode': '848340',
                              'countryCodeOfOrigin': 'AU', 'unitCost': {'amount': '4.00', 'currencyCode': 'USD'},
                              'measurement': {'weight': {'unit': 'GRAMS', 'value': 250.0}}}})
    return {'id': gid, 'legacyResourceId': str(number), 'title': title, 'handle': 'brass-gear-%s' % number,
            'status': 'ACTIVE', 'descriptionHtml': '<p>Solid brass</p>', 'vendor': 'Heritage Works',
            'productType': 'Gears', 'tags': ['brass', 'gear'], 'templateSuffix': None,
            'createdAt': updated, 'updatedAt': updated, 'hasOnlyDefaultVariant': len(variants) == 1,
            'isGiftCard': False, 'category': {'id': 'gid://shopify/TaxonomyCategory/1', 'fullName': 'Hardware'},
            'seo': {'title': None, 'description': None},
            'options': [{'id': 'gid://shopify/ProductOption/%s' % number, 'name': 'Size', 'position': 1,
                         'optionValues': []}],
            '_variants': nodes}


@tagged('post_install', '-at_install', 'eh_shopify')
class TestCatalogue(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Product = cls.env['product.product']
        cls.by_sku = Product.create({'name': 'Gear by reference', 'default_code': 'CAT-A'})
        cls.by_barcode = Product.create({'name': 'Gear by barcode', 'barcode': '9300000000017'})
        Product.create([{'name': 'Duplicate %s' % i, 'default_code': 'CAT-DUP'} for i in range(2)])

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.products_enabled = True
        self.Product = self.env['eh.shopify.product']
        self.Variant = self.env['eh.shopify.variant']
        self.Job = self.env['eh.shopify.job']

    def _import(self, product):
        self.shop.add_product(product)
        job = self.Product._enqueue_product_import(self.store, product['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Product.search([('store_id', '=', self.store.id), ('shopify_gid', '=', product['id'])])

    def test_variants_link_by_reference_and_barcode_and_the_rest_is_listed(self):
        binding = self._import(shopify_product(1, [('CAT-A', None), (None, '9300000000017'), ('CAT-Z', None)]))
        self.assertEqual(binding.state, 'unmatched')
        self.assertIn('CAT-Z', binding.reason)
        variants = binding.variant_ids.sorted('position')
        self.assertEqual(variants.mapped('product_id'), self.by_sku | self.by_barcode)
        self.assertEqual(variants.mapped('match_method'), ['sku', 'barcode', False])
        first = variants[0]
        self.assertEqual((first.price, first.hs_code, first.weight_unit, first.inventory_policy),
                         (10.0, '848340', 'GRAMS', 'DENY'))
        self.assertEqual((binding.vendor, binding.tags, binding.category_name), ('Heritage Works', 'brass, gear', 'Hardware'))

    def test_ambiguous_reference_is_never_guessed(self):
        binding = self._import(shopify_product(2, [('CAT-DUP', None)]))
        self.assertEqual(binding.state, 'ambiguous')
        self.assertFalse(binding.variant_ids.product_id)

    def test_existing_link_wins_and_mirror_fields_refresh(self):
        product = shopify_product(3, [('CAT-A', None)])
        binding = self._import(product)
        self.assertEqual(binding.state, 'linked')
        self.assertEqual(binding.product_tmpl_id, self.by_sku.product_tmpl_id)
        product['_variants'][0].update({'sku': 'RENAMED', 'price': '99.00'})
        product['updatedAt'] = '2026-09-16T09:00:00Z'
        binding = self._import(product)
        self.assertEqual(binding.variant_ids.product_id, self.by_sku)
        self.assertEqual((binding.variant_ids.sku, binding.variant_ids.price), ('RENAMED', 99.0))

    def test_all_variants_are_read_across_pages(self):
        binding = self._import(shopify_product(4, [('BULK-%s' % i, None) for i in range(120)]))
        self.assertEqual(len(binding.variant_ids), 120)
        self.assertEqual(binding.variant_count, 120)

    def test_deleted_product_is_marked_removed_and_stops_stock(self):
        binding = self._import(shopify_product(5, [('CAT-A', None)]))
        event = self.env['eh.shopify.event'].new({'store_id': self.store.id, 'topic': 'products/delete',
                                                  'webhook_id': 'w-p5', 'payload': {'id': 5}})
        self.assertEqual(event._on_product_delete(), {'removed': binding.shopify_gid})
        self.assertEqual(binding.state, 'removed')
        self.assertFalse(binding.variant_ids.active_in_shopify)
        self.assertEqual(self.by_sku.active, True, 'Odoo products are kept')

    def test_poller_queues_changed_products_and_moves_the_watermark(self):
        now = fields.Datetime.now().replace(microsecond=0)
        earlier, later = now - datetime.timedelta(hours=1), now - datetime.timedelta(minutes=30)
        stamp = '%Y-%m-%dT%H:%M:%SZ'
        self.shop.add_product(shopify_product(6, [('CAT-A', None)], updated=earlier.strftime(stamp)))
        self.shop.add_product(shopify_product(7, [('CAT-Z', None)], updated=later.strftime(stamp)))
        poll = self.Job._enqueue(self.store, 'products.poll', dedup_key='products.poll')
        poll._run_inline()
        self.assertEqual(poll.state, 'done', poll.last_error)
        self.assertEqual(poll.result['queued'], 2)
        self.assertEqual(self.Job.search_count([('job_type', '=', 'product.import'), ('store_id', '=', self.store.id)]), 2)
        self.assertEqual(self.store.products_polled_until, later, 'the watermark follows Shopify, never past now')

    def test_product_webhooks_follow_the_switch(self):
        self.assertIn('PRODUCTS_UPDATE', self.store._webhook_topics())
        event = self.env['eh.shopify.event'].new({'store_id': self.store.id, 'topic': 'products/update',
                                                  'webhook_id': 'w-p8', 'payload': {'id': 8}})
        self.assertEqual(event._on_product_change(), {'queued': 'gid://shopify/Product/8'})
        self.store.products_enabled = False
        self.assertNotIn('PRODUCTS_UPDATE', self.store._webhook_topics())
        self.assertIn('ignored', event._on_product_change())

    # ------------------------------------------------------------------ creation
    def test_creation_is_off_by_default(self):
        binding = self._import(shopify_product(10, [('NEW-OFF', None)]))
        self.assertEqual(binding.state, 'unmatched')
        self.assertFalse(binding.product_tmpl_id)

    def test_missing_product_is_created_with_its_real_variants(self):
        self.store.product_create_missing = True
        binding = self._import(shopify_product(11, [('NEW-0', None), ('NEW-1', None), ('NEW-2', None)]))
        self.assertEqual(binding.state, 'linked', binding.reason)
        template = binding.product_tmpl_id
        self.assertEqual(template.name, 'Brass gear')
        self.assertEqual(len(template.product_variant_ids), 3)
        self.assertEqual(sorted(template.product_variant_ids.mapped('default_code')), ['NEW-0', 'NEW-1', 'NEW-2'])
        self.assertEqual(template.list_price, 10.0)
        prices = {v.default_code: v.lst_price for v in template.product_variant_ids}
        self.assertEqual(prices, {'NEW-0': 10.0, 'NEW-1': 11.0, 'NEW-2': 12.0})
        self.assertTrue(compat.is_storable(template.product_variant_ids[:1]))
        self.assertAlmostEqual(template.product_variant_ids[:1].weight, 0.25, places=3)
        self.assertEqual(set(binding.variant_ids.mapped('match_method')), {'created'})
        self.assertTrue(binding.variant_ids.product_id.product_tmpl_id == template)

    def test_only_combinations_shopify_has_are_created(self):
        self.store.product_create_missing = True
        product = shopify_product(12, [('MIX-0', None), ('MIX-1', None), ('MIX-2', None)])
        product['options'] = [{'id': 'o1', 'name': 'Size', 'position': 1, 'optionValues': []},
                              {'id': 'o2', 'name': 'Colour', 'position': 2, 'optionValues': []}]
        combos = [('S', 'Red'), ('S', 'Blue'), ('M', 'Red')]
        for node, (size, colour) in zip(product['_variants'], combos):
            node['selectedOptions'] = [{'name': 'Size', 'value': size}, {'name': 'Colour', 'value': colour}]
        binding = self._import(product)
        template = binding.product_tmpl_id
        self.assertEqual(len(template.product_variant_ids), 3, 'M Blue does not exist in Shopify')
        self.assertEqual(len(template.attribute_line_ids), 2)
        self.assertIn('pricelist', binding.reason)

    def test_barcode_already_used_is_left_empty_and_explained(self):
        self.store.product_create_missing = True
        self.env['product.product'].create({'name': 'Old gear', 'barcode': '9300000000024', 'active': False})
        binding = self._import(shopify_product(13, [('CLASH-1', '9300000000024')]))
        self.assertEqual(binding.state, 'linked')
        self.assertFalse(binding.variant_ids.product_id.barcode)
        self.assertIn('9300000000024', binding.reason)

    def test_partly_linked_product_is_never_extended(self):
        self.store.product_create_missing = True
        binding = self._import(shopify_product(14, [('CAT-A', None), ('PART-1', None)]))
        self.assertEqual(binding.state, 'unmatched')
        self.assertEqual(binding.variant_ids.product_id, self.by_sku)

    # ------------------------------------------------------------------ two way sync
    def _push_job(self, binding):
        return self.Job.search([('job_type', '=', 'product.push'), ('dedup_key', '=', 'product.push:%s' % binding.id)],
                               order='id desc')

    def test_price_edited_in_odoo_is_sent_when_odoo_owns_prices(self):
        self.store.master_price = 'odoo'
        binding = self._import(shopify_product(301, [('CAT-A', None)]))
        self.assertFalse(self._push_job(binding), 'importing never queues a push')
        self.by_sku.product_tmpl_id.list_price = 42.0
        job = self._push_job(binding)
        self.assertEqual(len(job), 1)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        sent = self.shop.variant_updates[-1]['variants'][0]
        self.assertEqual(sent['price'], '42.00')
        self.assertNotIn('inventoryItem', sent, 'details stay with Shopify')
        self.assertEqual(self.shop.products[binding.shopify_gid]['_variants'][0]['price'], '42.00')

    def test_details_edited_in_odoo_are_sent_when_odoo_owns_them(self):
        self.store.master_content = 'odoo'
        binding = self._import(shopify_product(302, [('CAT-A', None)]))
        self.by_sku.barcode = '9300000000031'
        job = self._push_job(binding)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        sent = self.shop.variant_updates[-1]['variants'][0]
        self.assertEqual(sent['barcode'], '9300000000031')
        self.assertNotIn('price', sent, 'prices stay with Shopify')
        self.assertEqual(self.shop.product_updates[-1]['title'], self.by_sku.product_tmpl_id.name)
        self.assertEqual(self.Product._barcode_input('2026-10', '123'), {'barcodes': [{'value': '123'}]})
        self.assertEqual(self.Product._barcode_input('2026-07', '123'), {'barcode': '123'})

    def test_nothing_is_sent_when_the_store_keeps_products_separate(self):
        binding = self._import(shopify_product(303, [('CAT-A', None)]))
        self.by_sku.product_tmpl_id.list_price = 55.0
        self.assertFalse(self._push_job(binding))

    def test_archiving_in_odoo_archives_in_shopify(self):
        self.store.master_content = 'odoo'
        binding = self._import(shopify_product(304, [('CAT-A', None)]))
        self.by_sku.product_tmpl_id.active = False
        job = self._push_job(binding)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(self.shop.product_updates[-1]['status'], 'ARCHIVED')

    def test_refused_variant_is_noted_and_the_others_are_sent(self):
        self.store.master_price = 'odoo'
        product = shopify_product(305, [('CAT-A', None), (None, '9300000000017')])
        binding = self._import(product)
        refused_gid = product['_variants'][1]['id']
        self.shop.variant_update_refusals = {refused_gid: 'Price is too high'}
        (self.by_sku | self.by_barcode).mapped('product_tmpl_id').write({'list_price': 77.0})
        job = self._push_job(binding)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        refused = binding.variant_ids.filtered(lambda variant: variant.shopify_gid == refused_gid)
        self.assertEqual(refused.sync_note, 'Price is too high')
        self.assertEqual((binding.variant_ids - refused).price, 77.0)

    def test_shopify_owns_details_without_echoing_back(self):
        self.store.write({'master_content': 'shopify', 'master_price': 'odoo'})
        product = shopify_product(306, [('CAT-A', '9300000000055')])
        product['title'] = 'Heritage brass gear'
        self._import(product)
        self.assertEqual(self.by_sku.product_tmpl_id.name, 'Heritage brass gear')
        self.assertEqual(self.by_sku.barcode, '9300000000055')
        self.assertAlmostEqual(self.by_sku.weight, 0.25, places=3)
        self.assertFalse(self.Job.search([('job_type', '=', 'product.push')]), 'writes from Shopify are not sent back')

    def test_shopify_owns_prices(self):
        self.store.master_price = 'shopify'
        self._import(shopify_product(307, [('CAT-A', None)]))
        self.assertEqual(self.by_sku.product_tmpl_id.list_price, 10.0)

    # ------------------------------------------------------------------ review round
    def test_poller_reads_only_recent_changes_in_slices(self):
        now = fields.Datetime.now().replace(microsecond=0)
        stamp = '%Y-%m-%dT%H:%M:%SZ'
        self.store.products_polled_until = now - datetime.timedelta(minutes=20)
        self.shop.add_product(shopify_product(8, [('CAT-A', None)],
                                              updated=(now - datetime.timedelta(hours=3)).strftime(stamp)))
        for number, minutes in ((9, 15), (10, 12), (11, 5)):
            self.shop.add_product(shopify_product(number, [('CAT-Z%s' % number, None)],
                                                  updated=(now - datetime.timedelta(minutes=minutes)).strftime(stamp)))
        with patch.object(eh_shopify_product, 'POLL_SLICE', 1):
            poll = self.Job._enqueue(self.store, 'products.poll', dedup_key='products.poll')
            poll._run_inline()
            self.assertEqual(poll.result, {'queued': 1, 'more': True})
            for _round in range(5):
                follow = self.Job.search([('job_type', '=', 'products.poll'), ('state', '=', 'pending')])
                if not follow:
                    break
                follow._run_inline()
                self.assertEqual(follow.state, 'done', follow.last_error)
        imports = self.Job.search([('job_type', '=', 'product.import'), ('store_id', '=', self.store.id)])
        self.assertEqual(sorted(imports.mapped('dedup_key')),
                         sorted('product:gid://shopify/Product/%s' % number for number in (9, 10, 11)))
        self.assertEqual(self.store.products_polled_until, now - datetime.timedelta(minutes=5))

    def test_failed_poll_is_tried_again_after_a_pause(self):
        pause = datetime.timedelta(hours=1)
        failed = self.Job._enqueue(self.store, 'products.poll', dedup_key='products.poll')
        failed.write({'state': 'dead', 'finished_at': fields.Datetime.now()})
        self.assertTrue(self.Job._blocked_by_failure(self.store, 'products.poll', retry_after=pause))
        failed.write({'finished_at': fields.Datetime.now() - datetime.timedelta(hours=2)})
        self.assertFalse(self.Job._blocked_by_failure(self.store, 'products.poll', retry_after=pause))
        self.assertTrue(self.Job._blocked_by_failure(self.store, 'products.poll'), 'other work stays blocked')

    def test_partly_linked_product_never_renames_the_odoo_product(self):
        self.store.master_content = 'odoo'
        binding = self._import(shopify_product(308, [('CAT-A', None), ('CAT-NOPE', None)]))
        self.assertEqual(binding.state, 'unmatched')
        self.assertFalse(binding.product_tmpl_id)
        self.by_sku.product_tmpl_id.name = 'Only one of two variants'
        job = self._push_job(binding)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertFalse(self.shop.product_updates, 'the Shopify title stays')

    def test_change_during_a_running_push_is_sent_after_it(self):
        self.store.master_price = 'odoo'
        binding = self._import(shopify_product(309, [('CAT-A', None)]))
        self.by_sku.product_tmpl_id.list_price = 42.0
        self._push_job(binding).state = 'running'
        self.by_sku.product_tmpl_id.list_price = 43.0
        follow = self.Job.search([('dedup_key', '=', 'product.push:%s:next' % binding.id), ('state', '=', 'pending')])
        self.assertEqual(len(follow), 1)

    def test_prices_are_not_copied_between_currencies(self):
        other = 'EUR' if self.env.company.currency_id.name != 'EUR' else 'USD'
        self.store.write({'master_price': 'shopify', 'currency_code': other})
        before = self.by_sku.product_tmpl_id.list_price
        binding = self._import(shopify_product(310, [('CAT-A', None)]))
        self.assertEqual(self.by_sku.product_tmpl_id.list_price, before)
        self.assertIn('not copied', binding.reason)

    def _sales_tax(self, include):
        company = self.env.company
        if not (company.account_fiscal_country_id or company.country_id):
            company.country_id = self.env.ref('base.au')
        tax = compat.create_sale_tax(self.env, company, 'Catalogue tax %s' % ('incl' if include else 'excl'), 10.0,
                                     include)
        self.assertTrue(tax)
        return tax

    def test_prices_follow_the_tax_basis_of_each_side(self):
        template = self.by_sku.product_tmpl_id
        template.write({'taxes_id': [(6, 0, self._sales_tax(include=False).ids)], 'list_price': 100.0})
        self.store.write({'currency_code': self.env.company.currency_id.name, 'taxes_included': True})
        self.assertAlmostEqual(self.store._shopify_price(self.by_sku), 110.0, places=2)
        self.store.taxes_included = False
        self.assertAlmostEqual(self.store._shopify_price(self.by_sku), 100.0, places=2)
        self.store.write({'master_price': 'shopify', 'taxes_included': True})
        product = shopify_product(311, [('CAT-A', None)])
        product['_variants'][0]['price'] = '55.00'
        self._import(product)
        self.assertAlmostEqual(template.list_price, 50.0, places=2, msg='a tax included Shopify price loses its tax')
        template.taxes_id = [(6, 0, self._sales_tax(include=True).ids)]
        self.assertAlmostEqual(self.store._odoo_price(self.by_sku, 55.0), 55.0, places=2)
        self.store.taxes_included = False
        self.assertAlmostEqual(self.store._odoo_price(self.by_sku, 50.0), 55.0, places=2)

    def test_a_change_from_one_store_is_not_sent_back_to_it(self):
        self.store.master_price = 'odoo'
        binding = self._import(shopify_product(312, [('CAT-A', None)]))
        self.Product.with_context(eh_shopify_from_shopify=self.store.id)._queue_push_for(self.by_sku)
        self.assertFalse(self._push_job(binding))
        self.Product.with_context(eh_shopify_from_shopify=self.store.id + 1000)._queue_push_for(self.by_sku)
        self.assertTrue(self._push_job(binding), 'a change that came from another store still reaches this one')

    def test_a_storewide_price_rule_rechecks_prices(self):
        pricelist = self.env['product.pricelist'].create({'name': 'Shopify prices',
                                                          'currency_id': self.env.company.currency_id.id})
        self.store.write({'product_pricelist_id': pricelist.id, 'master_price': 'odoo',
                          'currency_code': self.env.company.currency_id.name})
        binding = self._import(shopify_product(313, [('CAT-A', None)]))
        self.Job.search([('job_type', '=', 'products.price_check')]).write({'state': 'cancelled'})
        self.env['product.pricelist.item'].create({'pricelist_id': pricelist.id, 'applied_on': '3_global',
                                                   'compute_price': 'fixed', 'fixed_price': 25.0})
        check = self.Job.search([('job_type', '=', 'products.price_check'), ('state', '=', 'pending')])
        self.assertEqual(len(check), 1)
        check._run_inline()
        self.assertEqual(check.state, 'done', check.last_error)
        self.assertEqual(check.result['queued'], 1)
        self.assertTrue(self._push_job(binding))

    def test_names_follow_the_language_of_the_store_company(self):
        self.env['res.lang']._activate_lang('fr_FR')
        self.env.company.partner_id.lang = 'fr_FR'
        self.store.master_content = 'odoo'
        binding = self._import(shopify_product(314, [('CAT-A', None)]))
        self.by_sku.product_tmpl_id.with_context(lang='fr_FR').name = 'Engrenage en laiton'
        job = self._push_job(binding)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(self.shop.product_updates[-1]['title'], 'Engrenage en laiton')
