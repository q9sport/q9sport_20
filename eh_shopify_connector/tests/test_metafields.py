# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import ShopifyCase
from .test_catalogue import shopify_product


@tagged('post_install', '-at_install', 'eh_shopify')
class TestMetafields(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gear = cls.env['product.product'].create({'name': 'Metafield gear', 'default_code': 'META-1'})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.products_enabled = True
        self.Product = self.env['eh.shopify.product']
        self.Map = self.env['eh.shopify.metafield.map']
        self.Job = self.env['eh.shopify.job']

    def _map(self, owner, key, kind, model, field, direction='import'):
        return self.Map.create({'store_id': self.store.id, 'owner_type': owner, 'namespace': 'custom', 'key': key,
                                'metafield_type': kind, 'field_id': self.env['ir.model.fields']._get(model, field).id,
                                'direction': direction})

    def _import(self, product):
        if product['id'] not in self.shop.products:
            self.shop.add_product(product)
        job = self.Product._enqueue_product_import(self.store, product['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Product.search([('store_id', '=', self.store.id), ('shopify_gid', '=', product['id'])])

    def test_mapped_metafields_fill_odoo_fields_without_echoing_back(self):
        self._map('product', 'care', 'multi_line_text_field', 'product.template', 'description_sale')
        self._map('variant', 'weight_kg', 'number_decimal', 'product.product', 'weight')
        product = shopify_product(401, [('META-1', None)])
        self.shop.add_product(product)
        self.shop.set_metafield(self.shop.products[product['id']], 'custom', 'care', 'multi_line_text_field',
                                'Hand wash cold')
        self.shop.set_metafield(self.shop.products[product['id']]['_variants'][0], 'custom', 'weight_kg',
                                'number_decimal', '1.25')
        self._import(product)
        self.assertEqual(self.gear.product_tmpl_id.description_sale, 'Hand wash cold')
        self.assertAlmostEqual(self.gear.weight, 1.25, places=3)
        self.assertFalse(self.Job.search([('job_type', '=', 'product.push')]), 'imported values are not sent back')

    def test_a_mapped_field_edited_in_odoo_is_sent_with_its_digest_and_only_once(self):
        self._map('product', 'care', 'multi_line_text_field', 'product.template', 'description_sale', direction='both')
        product = shopify_product(402, [('META-1', None)])
        self.shop.add_product(product)
        stored = self.shop.products[product['id']]
        digest = self.shop.set_metafield(stored, 'custom', 'care', 'multi_line_text_field', 'Old text')['compareDigest']
        binding = self._import(product)
        self.assertEqual(self.gear.product_tmpl_id.description_sale, 'Old text')
        self.gear.product_tmpl_id.description_sale = 'New care text'
        job = self.Job.search([('job_type', '=', 'product.push'), ('dedup_key', '=', 'product.push:%s' % binding.id)])
        self.assertEqual(len(job), 1, 'editing a mapped field queues a push even when product details stay separate')
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        sent = self.shop.metafield_sets[-1]
        self.assertEqual((sent[0]['value'], sent[0]['compareDigest']), ('New care text', digest))
        self.assertEqual(stored['_metafields']['custom.care']['value'], 'New care text')
        again = self.Product._enqueue_push(binding)
        again._run_inline()
        self.assertEqual(again.state, 'done', again.last_error)
        self.assertEqual(len(self.shop.metafield_sets), 1, 'an unchanged value is not sent again')

    def test_a_metafield_changed_in_shopify_meanwhile_is_read_again(self):
        self._map('product', 'care', 'single_line_text_field', 'product.template', 'description_sale',
                  direction='export')
        product = shopify_product(403, [('META-1', None)])
        self.shop.add_product(product)
        stored = self.shop.products[product['id']]
        self.shop.set_metafield(stored, 'custom', 'care', 'single_line_text_field', 'Shopify text')
        binding = self._import(product)
        self.gear.product_tmpl_id.description_sale = 'Odoo text'
        job = self.Job.search([('job_type', '=', 'product.push'), ('dedup_key', '=', 'product.push:%s' % binding.id)])
        self.shop.metafield_force_stale = True
        job._run_inline()
        self.assertNotEqual(job.state, 'done')
        self.assertIn('read again', job.last_error)
        self.assertEqual(stored['_metafields']['custom.care']['value'], 'Shopify text', 'nothing is overwritten')

    def test_a_type_that_does_not_fit_the_field_is_refused(self):
        with self.assertRaises(ValidationError):
            self._map('product', 'launch', 'boolean', 'product.template', 'description_sale')
        with self.assertRaises(ValidationError):
            self._map('variant', 'care', 'multi_line_text_field', 'product.template', 'description_sale')
