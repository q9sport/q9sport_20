# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Offline throughput benchmark, skipped unless EH_SHOPIFY_BENCHMARK is set.

The real import code runs against the offline fake shop, so the figures are
Odoo side processing only: no network time and no Shopify cost limit waits.
Run it with tools/test_runner_shopify.py --benchmark.

Orders are imported with the cache cleared between them, because production
runs one order per job and per transaction. Set EH_SHOPIFY_BENCH_CLEAR_EVERY to
0 to keep one shared cache for the whole run, which is what a single long
transaction costs.
"""
import json
import os
import re
import time
from collections import Counter

from odoo.tests import tagged

from .common import ShopifyCase
from .test_bulk import BULK_JOBS, export_rows
from .test_catalogue import shopify_product
from .test_order_import import make_order
from .test_order_sale import ensure_chart


OPERATION = re.compile(r'(query|mutation)\s+(\w+)')


def _band(traces, start, end):
    """Median time and query count of the orders between two shares of the run."""
    part = traces[int(len(traces) * start):max(1, int(len(traces) * end))]
    if not part:
        return None
    times = sorted(elapsed for elapsed, _queries in part)
    queries = sorted(queries for _elapsed, queries in part)
    return {'ms': round(times[len(times) // 2] * 1000, 1), 'queries': queries[len(queries) // 2]}


def _percentile(values, share):
    ordered = sorted(values)
    return round(ordered[int(share * (len(ordered) - 1))] * 1000, 1) if ordered else None


def _size(name, default):
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


@tagged('post_install', '-at_install', 'eh_shopify_benchmark')
class TestThroughput(ShopifyCase):

    def setUp(self):
        if not os.environ.get('EH_SHOPIFY_BENCHMARK'):
            self.skipTest('set EH_SHOPIFY_BENCHMARK to run the throughput benchmark')
        super().setUp()
        self.store._check_connection()
        self.Job = self.env['eh.shopify.job']

    def _record(self, name, count, seconds, **extra):
        row = dict(extra, benchmark=name, count=count, seconds=round(seconds, 2),
                   per_second=round(count / seconds, 1) if seconds else None)
        path = os.environ.get('EH_SHOPIFY_BENCH_OUT')
        if path:
            with open(path, 'a') as handle:
                handle.write(json.dumps(row) + '\n')
        return row

    def _drain(self, job_types):
        ran = 0
        while True:
            jobs = self.Job.search([('store_id', '=', self.store.id), ('state', '=', 'pending'),
                                    ('job_type', 'in', list(job_types))], order='id', limit=200)
            if not jobs:
                return ran
            for job in jobs:
                job._run_inline()
                self.assertEqual(job.state, 'done', '%s: %s' % (job.job_type, job.last_error))
                ran += 1

    def test_catalogue_bulk_link(self):
        count = _size('EH_SHOPIFY_BENCH_PRODUCTS', 2000)
        self.store.products_enabled = True
        self.env['product.product'].create([{'name': 'Bench %s' % n, 'default_code': 'BENCH-%s' % n}
                                            for n in range(count)])
        products = [shopify_product(100000 + n, [('BENCH-%s' % n, None)]) for n in range(count)]
        self.store.action_import_all_products()
        self._drain(('bulk.start',))
        bulk = self.env['eh.shopify.bulk'].search([('store_id', '=', self.store.id)], order='id desc', limit=1)
        self.shop.finish_bulk(bulk.operation_gid, export_rows(products))
        started = time.perf_counter()
        jobs = self._drain(BULK_JOBS)
        seconds = time.perf_counter() - started
        self.assertEqual(bulk.state, 'done', bulk.note)
        linked = self.env['eh.shopify.product'].search_count([('store_id', '=', self.store.id),
                                                             ('state', '=', 'linked')])
        self.assertEqual(linked, count)
        self._record('catalogue_bulk_link', count, seconds, jobs=jobs)

    def _graphql_calls(self, start):
        return [OPERATION.search(request['payload'].get('query') or '') for request in self.fake.requests[start:]
                if 'payload' in request]

    def _database_bytes(self):
        self.env.cr.execute('SELECT pg_database_size(current_database())')
        return self.env.cr.fetchone()[0]

    def test_catalogue_bulk_read_with_variants(self):
        count = _size('EH_SHOPIFY_BENCH_VARIANT_PRODUCTS', 500)
        variants = _size('EH_SHOPIFY_BENCH_VARIANTS', 8)
        self.store.products_enabled = True
        products = [shopify_product(300000 + n, [('MV-%s-%s' % (n, v), None) for v in range(variants)])
                    for n in range(count)]
        self.store.action_import_all_products()
        self._drain(('bulk.start',))
        bulk = self.env['eh.shopify.bulk'].search([('store_id', '=', self.store.id)], order='id desc', limit=1)
        rows = export_rows(products)
        self.shop.finish_bulk(bulk.operation_gid, rows)
        started = time.perf_counter()
        jobs = self._drain(BULK_JOBS)
        seconds = time.perf_counter() - started
        self.assertEqual(bulk.state, 'done', bulk.note)
        bindings = self.env['eh.shopify.product'].search([('store_id', '=', self.store.id)])
        self.assertEqual(len(bindings), count)
        self.assertEqual(len(bindings.mapped('variant_ids')), count * variants)
        self._record('catalogue_bulk_read_variants', len(rows), seconds, jobs=jobs, products=count,
                     variants_per_product=variants, unit='rows')

    def test_order_import_with_invoice_and_payment(self):
        count = _size('EH_SHOPIFY_BENCH_ORDERS', 500)
        company = self.env.company
        if not company.country_id:
            company.country_id = self.env.ref('base.us')
        ensure_chart(self.env, company)
        bank = self.env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
        self.env['eh.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments',
                                               'journal_id': bank.id})
        self.env['product.product'].create([{'name': 'Item %s' % i, 'default_code': 'SKU-%s' % i} for i in range(3)])
        self.store.invoice_policy = 'paid'
        Order = self.env['eh.shopify.order']
        Move = self.env['account.move']
        invoices_before = Move.search_count([('move_type', '=', 'out_invoice'), ('state', '=', 'posted')])
        gids = []
        for number in range(count):
            order = make_order(number=200000 + number, lines=3)
            self.shop.add_order(order)
            gids.append(order['id'])
        follow_up = [key for key in self.Job._handlers() if key.split('.')[0] in ('order', 'invoice', 'payment')]
        durations, calls, operations, traces = [], [], Counter(), []
        # Production imports one order per job, so each order starts with an empty cache. The benchmark holds one
        # transaction for the whole run, so it clears the cache between orders unless asked not to.
        clear_every = int(os.environ.get('EH_SHOPIFY_BENCH_CLEAR_EVERY') or 1)
        self.env.flush_all()
        size_before = self._database_bytes()
        started = time.perf_counter()
        for index, gid in enumerate(gids):
            if clear_every and index and not index % clear_every:
                self.env.invalidate_all()
            first_request = len(self.fake.requests)
            queries_before = self.env.cr.sql_log_count
            began = time.perf_counter()
            job = Order._enqueue_import(self.store, gid)
            job._run_inline()
            elapsed = time.perf_counter() - began
            durations.append(elapsed)
            traces.append((elapsed, self.env.cr.sql_log_count - queries_before))
            self.assertEqual(job.state, 'done', job.last_error)
            made = self._graphql_calls(first_request)
            calls.append(len(made))
            operations.update(match.group(2) for match in made if match)
        jobs = count + self._drain(follow_up)
        seconds = time.perf_counter() - started
        self.env.flush_all()
        growth = self._database_bytes() - size_before
        self.assertEqual(Order.search_count([('store_id', '=', self.store.id)]), count)
        invoices = Move.search_count([('move_type', '=', 'out_invoice'), ('state', '=', 'posted')]) - invoices_before
        self._record('order_import_invoice_payment', count, seconds, jobs=jobs, lines_per_order=3, invoices=invoices,
                     p50_ms=_percentile(durations, 0.5), p95_ms=_percentile(durations, 0.95),
                     first_tenth=_band(traces, 0.0, 0.1), last_tenth=_band(traces, 0.9, 1.0),
                     cache_cleared_every=clear_every or None,
                     shopify_calls_per_order=round(sum(calls) / len(calls), 2), max_calls=max(calls),
                     operations=dict(operations), database_mb_per_100k=round(growth / count * 100000 / 1048576, 0))
