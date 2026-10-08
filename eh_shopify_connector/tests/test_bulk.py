# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import base64
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models import eh_shopify_bulk, eh_shopify_product
from .common import ShopifyCase
from .test_catalogue import shopify_product
from .test_media import PNG_B64

BULK_JOBS = ('bulk.start', 'bulk.poll', 'bulk.read', 'bulk.chunk', 'product.import')


def export_rows(products):
    rows = []
    for product in products:
        root = {k: v for k, v in product.items() if k != '_variants'}
        root['variantsCount'] = {'count': len(product['_variants'])}
        rows.append(root)
        for variant in product['_variants']:
            rows.append(dict(variant, __parentId=product['id']))
    return rows


@tagged('post_install', '-at_install', 'eh_shopify')
class TestBulkImport(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['product.product'].create({'name': 'Bulk gear', 'default_code': 'BULK-A'})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.store.products_enabled = True
        self.Job = self.env['eh.shopify.job']
        self.Bulk = self.env['eh.shopify.bulk']
        self.Product = self.env['eh.shopify.product']

    def _drain(self, limit=200):
        for _attempt in range(limit):
            job = self.Job.search([('store_id', '=', self.store.id), ('state', '=', 'pending'),
                                   ('job_type', 'in', BULK_JOBS)], order='id', limit=1)
            if not job:
                return
            job._run_inline()
            self.assertEqual(job.state, 'done', '%s: %s' % (job.job_type, job.last_error))
        self.fail('export jobs did not finish')

    def _start(self):
        self.store.action_import_all_products()
        start = self.Job.search([('store_id', '=', self.store.id), ('job_type', '=', 'bulk.start')])
        start._run_inline()
        self.assertEqual(start.state, 'done', start.last_error)
        bulk = self.Bulk.search([('store_id', '=', self.store.id)])
        self.assertEqual((len(bulk), bulk.state), (1, 'running'))
        self.assertIn('products', self.shop.bulk_queries[-1])
        return bulk

    def test_whole_catalogue_is_read_in_passes_and_chunks(self):
        products = [shopify_product(201, [('BULK-A', None)]),
                    shopify_product(202, [('BULK-B', None), ('BULK-C', None)]),
                    shopify_product(203, [('BULK-D', None)])]
        bulk = self._start()
        self.shop.finish_bulk(bulk.operation_gid, export_rows(products))
        with patch.object(eh_shopify_bulk, 'PASS_BUDGET', 0.0), patch.object(eh_shopify_bulk, 'CHUNK_ROOTS', 1), \
                patch.object(eh_shopify_bulk, 'OPEN_ROWS_LIMIT', 3):
            self._drain()
        self.assertEqual(bulk.state, 'done', bulk.note)
        self.assertGreater(bulk.pass_count, 1, 'the file was read in several passes')
        self.assertEqual(bulk.chunks_enqueued, bulk.chunks_done)
        bindings = self.Product.search([('store_id', '=', self.store.id)])
        self.assertEqual(len(bindings), 3)
        self.assertEqual(bindings.filtered(lambda b: b.shopify_gid.endswith('/201')).state, 'linked')
        self.assertEqual(len(bindings.mapped('variant_ids')), 4)

    def test_variant_arriving_after_its_product_is_imported_through_the_api(self):
        late = shopify_product(211, [('LATE-1', None)])
        other = [shopify_product(212 + i, [('OTHER-%s' % i, None)]) for i in range(3)]
        self.shop.add_product(late)
        rows = export_rows([late] + other)
        variant_row = rows.pop(1)
        rows.append(variant_row)
        bulk = self._start()
        self.shop.finish_bulk(bulk.operation_gid, rows)
        with patch.object(eh_shopify_bulk, 'CHUNK_ROOTS', 1), patch.object(eh_shopify_bulk, 'OPEN_ROWS_LIMIT', 1):
            self._drain()
        binding = self.Product.search([('store_id', '=', self.store.id), ('shopify_gid', '=', late['id'])])
        self.assertEqual(len(binding.variant_ids), 1, 'the late variant is not lost')
        self.assertEqual(bulk.state, 'done')

    def test_partial_export_imports_what_arrived_and_says_so(self):
        bulk = self._start()
        self.shop.finish_bulk(bulk.operation_gid, export_rows([shopify_product(221, [('PART-1', None)])]),
                              status='FAILED', partial=True, error_code='TIMEOUT')
        self._drain()
        self.assertTrue(bulk.partial)
        self.assertIn('timeout', bulk.note)
        self.assertEqual(len(self.Product.search([('store_id', '=', self.store.id)])), 1)

    def test_failed_export_without_a_file_is_reported(self):
        bulk = self._start()
        self.shop.finish_bulk(bulk.operation_gid, [], status='FAILED', error_code='ACCESS_DENIED')
        self._drain()
        self.assertEqual(bulk.state, 'failed')
        self.assertIn('access denied', bulk.note)

    def test_finish_webhook_checks_the_export_at_once(self):
        bulk = self._start()
        event = self.env['eh.shopify.event'].new({'store_id': self.store.id, 'topic': 'bulk_operations/finish',
                                                  'webhook_id': 'w-bulk', 'payload': {
                                                      'admin_graphql_api_id': bulk.operation_gid}})
        self.assertEqual(event._on_bulk_finish(), {'polling': bulk.operation_gid})
        polls = self.Job.search([('job_type', '=', 'bulk.poll'), ('state', '=', 'pending')], order='id desc')
        self.assertLessEqual(polls[0].next_run_at, fields.Datetime.now())

    def test_import_needs_product_sync_switched_on(self):
        self.store.products_enabled = False
        with self.assertRaises(UserError):
            self.store.action_import_all_products()

    def _product_binding(self, number):
        product = shopify_product(number, [('SWEEP-%s' % number, None)])
        self.shop.add_product(product)
        job = self.Product._enqueue_product_import(self.store, product['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        return self.Product.search([('store_id', '=', self.store.id), ('shopify_gid', '=', product['id'])])

    def _sweep(self):
        start = self.Bulk._start_sweep(self.store)
        self.assertTrue(start)
        start._run_inline()
        self.assertEqual(start.state, 'done', start.last_error)
        return self.Bulk.search([('store_id', '=', self.store.id), ('kind', '=', 'product_ids')])

    def test_nightly_check_marks_only_products_missing_from_a_complete_list(self):
        kept = self._product_binding(231)
        gone = self._product_binding(232)
        bulk = self._sweep()
        newer = self._product_binding(233)
        self.shop.finish_bulk(bulk.operation_gid, [{'id': kept.shopify_gid}])
        with patch.object(eh_shopify_bulk, 'PASS_BUDGET', 0.0):
            self._drain()
        self.assertEqual(bulk.state, 'done', bulk.note)
        self.assertEqual(gone.state, 'removed')
        self.assertNotEqual(kept.state, 'removed')
        self.assertNotEqual(newer.state, 'removed', 'a product created during the export is not removed')
        self.assertTrue(self.store.products_swept_at)

    def test_incomplete_product_list_removes_nothing(self):
        kept = self._product_binding(241)
        self._product_binding(242)
        bulk = self._sweep()
        self.shop.finish_bulk(bulk.operation_gid, [{'id': kept.shopify_gid}], status='FAILED', partial=True,
                              error_code='TIMEOUT')
        self._drain()
        self.assertFalse(self.Product.search([('store_id', '=', self.store.id), ('state', '=', 'removed')]))
        self.assertIn('incomplete', bulk.note)

    def test_no_second_export_while_one_is_running(self):
        self._start()
        self.assertFalse(self.Bulk._start_sweep(self.store))

    # ------------------------------------------------------------------ review round
    def _bindings(self):
        return self.Product.search([('store_id', '=', self.store.id)])

    def test_older_export_rows_never_overwrite_newer_imports_or_revive_deletions(self):
        fresh = shopify_product(251, [('BULK-A', None)], title='Renamed after the export', updated='2026-09-16T10:00:00Z')
        self.shop.add_product(fresh)
        self.Product._enqueue_product_import(self.store, fresh['id'])._run_inline()
        deleted = self._product_binding(252)
        bulk = self._start()
        deleted._mark_removed()
        stale = dict(fresh, title='Title before the rename', updatedAt='2026-09-16T08:00:00Z')
        self.shop.finish_bulk(bulk.operation_gid, export_rows([stale, shopify_product(252, [('SWEEP-252', None)])]))
        self._drain()
        self.assertEqual(bulk.state, 'done', bulk.note)
        binding = self._bindings().filtered(lambda record: record.shopify_gid == fresh['id'])
        self.assertEqual(binding.title, 'Renamed after the export')
        self.assertEqual(deleted.state, 'removed', 'a product deleted during the export stays removed')

    def test_short_download_is_fetched_again_before_anything_is_removed(self):
        kept = self._product_binding(261)
        gone = self._product_binding(262)
        bulk = self._sweep()
        url = self.shop.finish_bulk(bulk.operation_gid, [{'id': kept.shopify_gid}, {'id': 'gid://shopify/Product/9261'},
                                                         {'id': 'gid://shopify/Product/9262'}])
        self.shop.bulk_cuts[url] = 1
        self._drain()
        self.assertEqual(bulk.state, 'done', bulk.note)
        self.assertEqual(bulk.download_attempts, 1)
        self.assertNotEqual(kept.state, 'removed')
        self.assertEqual(gone.state, 'removed')

    def test_download_that_keeps_ending_early_fails_and_removes_nothing(self):
        kept = self._product_binding(263)
        self._product_binding(264)
        bulk = self._sweep()
        url = self.shop.finish_bulk(bulk.operation_gid, [{'id': kept.shopify_gid}, {'id': 'gid://shopify/Product/9263'}])
        self.shop.bulk_cuts[url] = 10
        self._drain()
        self.assertEqual(bulk.state, 'failed')
        self.assertIn('ended after', bulk.note)
        self.assertFalse(self._bindings().filtered(lambda record: record.state == 'removed'))

    def test_download_resumes_across_passes(self):
        bulk = self._start()
        self.shop.finish_bulk(bulk.operation_gid, export_rows([shopify_product(271 + i, [('RESUME-%s' % i, None)])
                                                              for i in range(3)]))
        self.shop.bulk_download_limit = 300
        self._drain()
        self.assertEqual(bulk.state, 'done', bulk.note)
        offsets = [request['download_from'] for request in self.fake.requests if 'download_from' in request]
        self.assertGreater(len(offsets), 2, 'the file arrived over several passes')
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(len(self._bindings()), 3)

    def test_unreadable_export_line_fails_the_export(self):
        bulk = self._start()
        rows = export_rows([shopify_product(281, [('BAD-1', None)])]) + ['{"id": "gid://shopify/Product/282", ']
        self.shop.finish_bulk(bulk.operation_gid, rows)
        self._drain()
        self.assertEqual(bulk.state, 'failed')
        self.assertIn('could not be read', bulk.note)

    def test_stalled_export_is_closed_and_a_manager_can_stop_one(self):
        bulk = self._start()
        self.Job.search(bulk._live_job_domain()).write({'state': 'dead'})
        self.Bulk._finish_idle_imports(self.store)
        self.assertEqual(bulk.state, 'running', 'a quiet export is given time')
        self.env.cr.execute("UPDATE eh_shopify_bulk SET write_date = (now() at time zone 'UTC') - interval '3 hours' "
                            "WHERE id = %s", (bulk.id,))
        bulk.invalidate_recordset(['write_date'])
        self.Bulk._finish_idle_imports(self.store)
        self.assertEqual(bulk.state, 'failed')
        other = self.Bulk.create({'store_id': self.store.id, 'kind': 'products', 'state': 'reading'})
        read = other._enqueue_pass()
        other.action_stop()
        self.assertEqual(other.state, 'failed')
        self.assertEqual(read.state, 'cancelled')

    def test_chunks_are_sized_by_rows_and_continue_when_time_runs_out(self):
        products = [shopify_product(291, [('ROWS-%s' % i, None) for i in range(3)])]
        products += [shopify_product(292 + i, [('ROW1-%s' % i, None)]) for i in range(3)]
        bulk = self._start()
        self.shop.finish_bulk(bulk.operation_gid, export_rows(products))
        with patch.object(eh_shopify_bulk, 'CHUNK_ROWS', 4), patch.object(eh_shopify_bulk, 'CHUNK_BUDGET', 0.0):
            self._drain()
        self.assertEqual(bulk.state, 'done', bulk.note)
        chunks = self.Job.search(bulk._chunk_domain())
        first = chunks.filtered(lambda job: job.dedup_key == 'bulk:%s:0' % bulk.id)
        self.assertEqual(len(first.payload['roots']), 1, 'a product with three variants fills a chunk of four rows')
        self.assertTrue(chunks.filtered(lambda job: '.' in job.dedup_key), 'a chunk out of time hands the rest on')
        self.assertEqual(len(self._bindings()), 4)

    def test_product_with_too_many_variants_is_imported_through_an_export(self):
        big = shopify_product(301, [('BIG-%s' % i, None) for i in range(3)])
        self.shop.add_product(big)
        with patch.object(eh_shopify_product, 'API_VARIANT_LIMIT', 2):
            job = self.Product._enqueue_product_import(self.store, big['id'])
            job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        self.assertIn('exporting', job.result)
        start = self.Job.search([('job_type', '=', 'bulk.start'), ('dedup_key', '=', 'bulk.start:product:%s' % big['id'])])
        start._run_inline()
        self.assertEqual(start.state, 'done', start.last_error)
        self.assertIn('products(query: "id:301")', self.shop.bulk_queries[-1])
        bulk = self.Bulk.search([('kind', '=', 'product'), ('product_gid', '=', big['id'])])
        self.shop.finish_bulk(bulk.operation_gid, export_rows([big]))
        self._drain()
        binding = self._bindings().filtered(lambda record: record.shopify_gid == big['id'])
        self.assertEqual(len(binding.variant_ids), 3)

    def test_images_found_by_an_export_are_copied_by_their_own_job(self):
        self.store.write({'master_content': 'shopify', 'product_create_missing': True})
        product = shopify_product(311, [('PICTURE-1', None)])
        url = 'https://cdn.shopify.com/s/files/1/export.png'
        product['featuredMedia'] = {'id': 'gid://shopify/MediaImage/311', 'image': {'url': url}}
        self.shop.downloads[url] = base64.b64decode(PNG_B64)
        bulk = self._start()
        self.shop.finish_bulk(bulk.operation_gid, export_rows([product]))
        self._drain()
        self.assertFalse([request for request in self.fake.requests if request.get('download')],
                         'the import chunk downloads nothing')
        image = self.Job.search([('job_type', '=', 'product.image'), ('state', '=', 'pending')])
        self.assertEqual(len(image), 1)
        image._run_inline()
        self.assertEqual(image.state, 'done', image.last_error)
        binding = self._bindings().filtered(lambda record: record.shopify_gid == product['id'])
        self.assertTrue(binding.product_tmpl_id.image_1920)
