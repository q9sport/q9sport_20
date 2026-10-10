# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import base64
import hashlib
import io

from odoo.tests import tagged

from .common import ShopifyCase
from .test_catalogue import shopify_product

PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='


@tagged('post_install', '-at_install', 'eh_shopify')
class TestProductMedia(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.picture = cls.env['product.product'].create({'name': 'Picture gear', 'default_code': 'PIC-1'})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.write({'products_enabled': True, 'master_content': 'odoo'})
        self.Product = self.env['eh.shopify.product']
        self.Job = self.env['eh.shopify.job']

    def _import(self, product):
        self.shop.add_product(product)
        job = self.Product._enqueue_product_import(self.store, product['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Product.search([('store_id', '=', self.store.id), ('shopify_gid', '=', product['id'])])

    def _run_push(self, binding):
        job = self.Job.search([('job_type', '=', 'product.push'), ('dedup_key', '=', 'product.push:%s' % binding.id),
                               ('state', '=', 'pending')])
        self.assertEqual(len(job), 1)
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return job

    def test_image_is_uploaded_once_and_attached(self):
        binding = self._import(shopify_product(601, [('PIC-1', None)]))
        template = self.picture.product_tmpl_id
        template.image_1920 = PNG_B64
        self._run_push(binding)
        self.assertEqual(len(self.shop.uploads), 1)
        self.assertEqual(self.shop.file_updates[-1]['files'][0]['referencesToAdd'], [binding.shopify_gid])
        stored = template.image_1920.content
        self.assertEqual(binding.image_hash, hashlib.sha256(stored).hexdigest())
        template.name = 'Picture gear, second edition'
        self._run_push(binding)
        self.assertEqual(len(self.shop.uploads), 1, 'an unchanged image is not sent again')

    def test_retry_reuses_the_file_already_in_shopify(self):
        binding = self._import(shopify_product(602, [('PIC-1', None)]))
        template = self.picture.product_tmpl_id
        template.image_1920 = PNG_B64
        stored = template.image_1920.content
        filename = 'odoo-%s.png' % hashlib.sha256(stored).hexdigest()[:40]
        self.shop.files['gid://shopify/MediaImage/77'] = {'id': 'gid://shopify/MediaImage/77', 'filename': filename,
                                                          'alt': None, 'fileStatus': 'READY', 'references': []}
        self._run_push(binding)
        self.assertFalse(self.shop.uploads, 'the existing file is reused')
        self.assertEqual(self.shop.file_updates[-1]['files'][0]['id'], 'gid://shopify/MediaImage/77')

    def test_image_from_shopify_fills_the_product_when_shopify_owns_details(self):
        self.store.master_content = 'shopify'
        product = shopify_product(603, [('PIC-1', None)])
        url = 'https://cdn.shopify.com/s/files/1/gear.png'
        product['featuredMedia'] = {'id': 'gid://shopify/MediaImage/603', 'image': {'url': url}}
        self.shop.downloads[url] = base64.b64decode(PNG_B64)
        binding = self._import(product)
        self.assertTrue(self.picture.product_tmpl_id.image_1920)
        self.assertEqual(binding.media_gid, 'gid://shopify/MediaImage/603')
        self.assertFalse(self.Job.search([('job_type', '=', 'product.push')]), 'the copied image is not sent back')

    def test_images_outside_the_shopify_cdn_are_ignored(self):
        self.store.master_content = 'shopify'
        product = shopify_product(604, [('PIC-1', None)])
        product['featuredMedia'] = {'id': 'gid://shopify/MediaImage/604',
                                    'image': {'url': 'https://files.example.net/gear.png'}}
        self._import(product)
        self.assertFalse(self.picture.product_tmpl_id.image_1920)
        self.assertFalse([request for request in self.fake.requests if request.get('download')])

    def test_image_resized_by_odoo_is_not_sent_back(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (2400, 20), (180, 120, 40)).save(buffer, format='PNG')
        self.store.master_content = 'shopify'
        product = shopify_product(605, [('PIC-1', None)])
        url = 'https://cdn.shopify.com/s/files/1/wide.png'
        product['featuredMedia'] = {'id': 'gid://shopify/MediaImage/605', 'image': {'url': url}}
        self.shop.downloads[url] = buffer.getvalue()
        binding = self._import(product)
        template = self.picture.product_tmpl_id
        stored = template.image_1920.content
        self.assertNotEqual(stored, buffer.getvalue(), 'Odoo resized the image')
        self.assertEqual(binding.image_hash, hashlib.sha256(stored).hexdigest())
        self.store.master_content = 'odoo'
        template.name = 'Wide picture gear'
        self._run_push(binding)
        self.assertFalse(self.shop.uploads, 'the copied image is not uploaded back')
