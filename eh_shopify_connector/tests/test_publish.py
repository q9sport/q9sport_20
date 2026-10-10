# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged

from .. import compat
from ..lib import errors
from ..models import eh_shopify_product_publish
from .common import ShopifyCase
from .test_catalogue import shopify_product
from .test_media import PNG_B64


@tagged('post_install', '-at_install', 'eh_shopify')
class TestPublish(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vals = {'name': 'Heritage lamp', 'default_code': 'LAMP-1', 'barcode': '9300000000062', 'weight': 1.2,
                'list_price': 99.0}
        vals.update(compat.product_type_vals(cls.env, 'storable'))
        cls.lamp = cls.env['product.template'].create(vals)
        size = cls.env['product.attribute'].create({'name': 'Shirt size', 'create_variant': 'always'})
        values = cls.env['product.attribute.value'].create([{'attribute_id': size.id, 'name': name}
                                                            for name in ('S', 'M', 'L')])
        cls.shirt = cls.env['product.template'].create({
            'name': 'Heritage shirt', 'list_price': 40.0,
            'attribute_line_ids': [(0, 0, {'attribute_id': size.id, 'value_ids': [(6, 0, values.ids)]})]})
        for variant in cls.shirt.product_variant_ids:
            variant.default_code = 'SHIRT-%s' % variant.product_template_attribute_value_ids.name

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.products_enabled = True
        self.Job = self.env['eh.shopify.job']
        self.Variant = self.env['eh.shopify.variant']

    def _publish(self, template, status='DRAFT'):
        wizard = self.env['eh.shopify.publish.wizard'].with_context(
            active_model='product.template', active_ids=template.ids).create({'store_id': self.store.id,
                                                                              'status': status})
        wizard.action_publish()
        job = self.Job.search([('job_type', '=', 'product.publish'), ('res_model', '=', 'product.template'),
                               ('res_id', '=', template.id)], order='id desc', limit=1)
        job._run_inline()
        return job

    def test_single_product_is_created_as_a_draft_and_linked(self):
        job = self._publish(self.lamp)
        self.assertEqual(job.state, 'done', job.last_error)
        sent = self.shop.product_sets[-1]
        self.assertEqual(sent['status'], 'DRAFT')
        self.assertEqual(sent['productOptions'], [{'name': 'Title', 'values': [{'name': 'Default Title'}]}])
        variant = sent['variants'][0]
        self.assertEqual((variant['price'], variant['inventoryItem']['sku'], variant['barcode']),
                         ('99.00', 'LAMP-1', '9300000000062'))
        self.assertTrue(variant['inventoryItem']['tracked'])
        self.assertAlmostEqual(variant['inventoryItem']['measurement']['weight']['value'], 1.2, places=3)
        link = self.Variant.search([('store_id', '=', self.store.id), ('product_id', '=', self.lamp.product_variant_id.id)])
        self.assertEqual(link.match_method, 'published')
        self.assertTrue(self.Job.search([('job_type', '=', 'product.import'), ('dedup_key', '=', 'product:%s' % link.product_gid)]))
        self.assertTrue(self.env['eh.shopify.inventory.dirty'].search([('product_id', '=', self.lamp.product_variant_id.id)]))

    def test_variants_are_created_from_attributes(self):
        job = self._publish(self.shirt, status='ACTIVE')
        self.assertEqual(job.state, 'done', job.last_error)
        sent = self.shop.product_sets[-1]
        self.assertEqual(sent['status'], 'ACTIVE')
        self.assertEqual(sent['productOptions'][0]['name'], 'Shirt size')
        self.assertEqual(sorted(value['name'] for value in sent['productOptions'][0]['values']), ['L', 'M', 'S'])
        self.assertEqual(len(sent['variants']), 3)
        links = self.Variant.search([('store_id', '=', self.store.id), ('product_id', 'in', self.shirt.product_variant_ids.ids)])
        self.assertEqual(len(links), 3)
        self.assertEqual(set(links.mapped('sku')), {'SHIRT-S', 'SHIRT-M', 'SHIRT-L'})

    def test_a_product_shopify_already_has_is_linked_not_duplicated(self):
        self.shop.add_product(shopify_product(501, [('LAMP-1', None)]))
        job = self._publish(self.lamp)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(job.result.get('linked_existing'), 'gid://shopify/Product/501')
        self.assertFalse(self.shop.product_sets)

    def test_a_linked_product_is_not_published_twice(self):
        self._publish(self.lamp)
        job = self._publish(self.lamp)
        self.assertIn('skipped', job.result)
        self.assertEqual(len(self.shop.product_sets), 1)

    def test_only_shopify_managers_publish(self):
        user = self._user('eh_catalogue_clerk', ['eh_shopify_connector.group_shopify_user', 'base.group_user'])
        with self.assertRaises(AccessError):
            self.env['eh.shopify.publish.wizard'].with_user(user).create({'store_id': self.store.id,
                                                                          'product_tmpl_ids': [(6, 0, self.lamp.ids)]})

    # ------------------------------------------------------------------ review round
    def test_existing_product_is_linked_by_its_options_before_import(self):
        existing = shopify_product(502, [(None, None), (None, None), (None, None)])
        existing['handle'] = eh_shopify_product_publish._handle(self.shirt.name, self.shirt.id)
        for variant, size in zip(existing['_variants'], ('S', 'M', 'L')):
            variant['selectedOptions'] = [{'name': 'Shirt size', 'value': size}]
        self.shop.add_product(existing)
        job = self._publish(self.shirt)
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertEqual(job.result['variants'], 3)
        links = self.Variant.search([('store_id', '=', self.store.id),
                                     ('product_id', 'in', self.shirt.product_variant_ids.ids)])
        self.assertEqual(len(links), 3)
        self.assertEqual(set(links.mapped('match_method')), {'published'})
        self.assertFalse(self.shop.product_sets)

    def test_image_is_sent_by_its_own_job_so_links_survive_a_slow_upload(self):
        self.lamp.image_1920 = PNG_B64
        job = self._publish(self.lamp)
        self.assertEqual(job.state, 'done', job.last_error)
        link = self.Variant.search([('store_id', '=', self.store.id), ('product_id', '=', self.lamp.product_variant_id.id)])
        self.assertTrue(link)
        image = self.Job.search([('job_type', '=', 'product.image'), ('state', '=', 'pending')])
        self.assertEqual(len(image), 1)
        self.assertFalse(self.shop.uploads, 'publishing does not wait on the upload')
        with patch.object(type(self.env['eh.shopify.product']), '_push_image',
                          side_effect=errors.TransientError('Image upload timed out')):
            image._run_inline()
        self.assertNotEqual(image.state, 'done')
        self.assertTrue(link.exists(), 'the links stay')
        image.state = 'pending'
        image._run_inline()
        self.assertEqual(image.state, 'done', image.last_error)
        self.assertEqual(len(self.shop.uploads), 1)

    def test_a_store_in_another_currency_needs_a_pricelist(self):
        self.store.currency_code = 'EUR' if self.env.company.currency_id.name != 'EUR' else 'USD'
        job = self._publish(self.lamp)
        self.assertNotEqual(job.state, 'done')
        self.assertIn('no price in the store currency', job.last_error)
        self.assertFalse(self.shop.product_sets)

    # ------------------------------------------------------------------ background creation
    def _cap(self):
        colour = self.env['product.attribute'].create({'name': 'Cap colour', 'create_variant': 'always'})
        values = self.env['product.attribute.value'].create([{'attribute_id': colour.id, 'name': name}
                                                             for name in ('Red', 'Blue')])
        return self.env['product.template'].create({
            'name': 'Heritage cap', 'list_price': 25.0,
            'attribute_line_ids': [(0, 0, {'attribute_id': colour.id, 'value_ids': [(6, 0, values.ids)]})]})

    def _checks(self):
        return self.Job.search([('job_type', '=', 'product.publish.check'), ('state', '=', 'pending')])

    def test_product_with_many_variants_is_created_in_the_background(self):
        cap = self._cap()
        with patch.object(eh_shopify_product_publish, 'MAX_SYNC_VARIANTS', 1):
            job = self._publish(cap)
        self.assertEqual(job.state, 'done', job.last_error)
        operation_gid = job.result['operation']
        self.assertEqual(len(self.shop.product_sets), 1)
        check = self._checks()
        check._run_inline()
        self.assertEqual(check.state, 'done', check.last_error)
        self.assertEqual(check.result, {'status': 'CREATED'})
        self.shop.product_operations[operation_gid]['status'] = 'COMPLETE'
        again = self._checks()
        self.assertEqual(len(again), 1)
        again._run_inline()
        self.assertEqual(again.state, 'done', again.last_error)
        imports = self.Job.search([('job_type', '=', 'product.import'), ('state', '=', 'pending')])
        self.assertEqual(len(imports), 1)
        imports._run_inline()
        self.assertEqual(imports.state, 'done', imports.last_error)
        links = self.Variant.search([('store_id', '=', self.store.id), ('product_id', 'in', cap.product_variant_ids.ids)])
        self.assertEqual(len(links), 2)
        self.assertEqual(set(links.mapped('match_method')), {'options'})
        self.assertTrue(self.env['eh.shopify.inventory.dirty'].search([('product_id', 'in', cap.product_variant_ids.ids)]))

    def test_background_creation_refused_by_shopify_is_reported(self):
        cap = self._cap()
        with patch.object(eh_shopify_product_publish, 'MAX_SYNC_VARIANTS', 1):
            job = self._publish(cap)
        self.shop.product_operations[job.result['operation']].update({'status': 'COMPLETE', 'userErrors': [
            {'field': ['input', 'variants'], 'message': 'Too many option values', 'code': 'INVALID'}]})
        check = self._checks()
        check._run_inline()
        self.assertNotEqual(check.state, 'done')
        self.assertIn('Too many option values', check.last_error)
        self.assertFalse(self.Variant.search([('product_id', 'in', cap.product_variant_ids.ids)]))

    def test_publishing_again_while_shopify_creates_the_product_sends_nothing(self):
        cap = self._cap()
        with patch.object(eh_shopify_product_publish, 'MAX_SYNC_VARIANTS', 1):
            self._publish(cap)
            second = self._publish(cap)
            self.assertEqual(second.state, 'pending', 'it waits behind the check of the first request')
            self._checks()._run_inline()
            second._run_inline()
        self.assertEqual(second.state, 'done', second.last_error)
        self.assertIn('skipped', second.result)
        self.assertEqual(len(self.shop.product_sets), 1)

    def test_products_above_the_shopify_variant_limit_are_refused(self):
        with patch.object(eh_shopify_product_publish, 'MAX_VARIANTS', 2):
            job = self._publish(self.shirt)
        self.assertNotEqual(job.state, 'done')
        self.assertIn('more than the 2 Shopify allows', job.last_error)
        self.assertFalse(self.shop.product_sets)
