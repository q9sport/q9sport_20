# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .common import ShopifyCase
from .test_bulk import export_rows
from .test_catalogue import shopify_product

CHECK_JOBS = ('bulk.start', 'bulk.poll', 'bulk.read', 'bulk.chunk')


@tagged('post_install', '-at_install', 'eh_shopify')
class TestCatalogueMatch(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Product = cls.env['product.product']
        cls.by_sku = Product.create({'name': 'Match by reference', 'default_code': 'MATCH-A'})
        cls.by_barcode = Product.create({'name': 'Match by barcode', 'barcode': '9300000000093'})
        Product.create([{'name': 'Match twin %s' % i, 'default_code': 'MATCH-DUP'} for i in range(2)])
        cls.odoo_only = Product.create({'name': 'Sold only in Odoo', 'default_code': 'MATCH-ODOO'})
        cls.not_for_sale = Product.create({'name': 'Internal part', 'default_code': 'MATCH-PART', 'sale_ok': False})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.Job = self.env['eh.shopify.job']
        self.Report = self.env['eh.shopify.match.report']

    def _drain(self):
        for _attempt in range(100):
            job = self.Job.search([('store_id', '=', self.store.id), ('state', '=', 'pending'),
                                   ('job_type', 'in', CHECK_JOBS)], order='id', limit=1)
            if not job:
                return
            job._run_inline()
            self.assertEqual(job.state, 'done', '%s: %s' % (job.job_type, job.last_error))
        self.fail('check jobs did not finish')

    def _start(self):
        action = self.store.action_check_catalogue_match()
        report = self.Report.browse(action['res_id'])
        start = self.Job.search([('store_id', '=', self.store.id), ('dedup_key', '=', 'bulk.start:match'),
                                 ('state', '=', 'pending')])
        start._run_inline()
        self.assertEqual(start.state, 'done', start.last_error)
        self.assertTrue(report.bulk_id)
        return report

    def _check(self, products):
        report = self._start()
        self.shop.finish_bulk(report.bulk_id.operation_gid, export_rows(products))
        self._drain()
        return report

    def _lines(self, report, kind):
        return report.line_ids.filtered(lambda line: line.kind == kind)

    def test_check_predicts_the_import_and_changes_nothing(self):
        report = self._check([
            shopify_product(701, [('MATCH-A', None), (None, '9300000000093')]),
            shopify_product(702, [('MATCH-DUP', None)]),
            shopify_product(703, [('NOWHERE-1', None)]),
            shopify_product(704, [('TWICE', None)]),
            shopify_product(705, [('TWICE', None)]),
        ])
        self.assertEqual(report.state, 'ready', report.note)
        self.assertEqual((report.product_count, report.variant_count), (5, 6))
        self.assertEqual((report.link_count, report.ambiguous_count, report.unmatched_count, report.create_count),
                         (2, 1, 3, 0))
        self.assertEqual(set(self._lines(report, 'link').mapped('product_id').ids), {self.by_sku.id, self.by_barcode.id})
        self.assertEqual(self._lines(report, 'duplicate').mapped('sku'), ['TWICE'])
        odoo_only = self._lines(report, 'odoo_only').mapped('product_id')
        self.assertIn(self.odoo_only, odoo_only)
        self.assertNotIn(self.not_for_sale, odoo_only, 'products not sold are left out')
        self.assertNotIn(self.by_sku, odoo_only)
        self.assertFalse(self.env['eh.shopify.product'].search([('store_id', '=', self.store.id)]),
                         'the check writes no links')
        self.assertFalse(self.env['eh.shopify.variant'].search([('store_id', '=', self.store.id)]))
        self.env.cr.execute("SELECT count(*) FROM eh_shopify_match_seen WHERE report_id = %s", (report.id,))
        self.assertEqual(self.env.cr.fetchone()[0], 0, 'working rows are cleared')
        lines = report.with_context(match_kind='ambiguous').action_open_lines()
        self.assertIn(('kind', '=', 'ambiguous'), lines['domain'])

    def test_missing_products_show_as_created_when_creation_is_on(self):
        self.store.product_create_missing = True
        report = self._check([shopify_product(711, [('NEW-1', None), ('NEW-2', None)]),
                              shopify_product(712, [('MATCH-A', None), ('NEW-3', None)])])
        self.assertEqual(report.state, 'ready', report.note)
        self.assertEqual(report.create_count, 2)
        self.assertEqual((report.link_count, report.unmatched_count), (1, 1), 'a partly linked product is never extended')

    def test_existing_links_are_reported_as_linked(self):
        product = shopify_product(721, [('MATCH-A', None)])
        self.shop.add_product(product)
        self.store.products_enabled = True
        job = self.env['eh.shopify.product']._enqueue_product_import(self.store, product['id'])
        job._run_inline()
        self.assertEqual(job.state, 'done', job.last_error)
        report = self._check([product])
        self.assertEqual((report.linked_count, report.link_count), (1, 0))

    def test_one_check_at_a_time_and_a_failed_export_is_reported(self):
        report = self._start()
        again = self.store.action_check_catalogue_match()
        self.assertEqual(again['res_id'], report.id, 'the running check is shown instead of a second one')
        self.shop.finish_bulk(report.bulk_id.operation_gid, [], status='FAILED', error_code='ACCESS_DENIED')
        self._drain()
        self.assertEqual(report.state, 'failed')
        self.assertIn('access denied', report.note)
