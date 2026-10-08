# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
import logging

from . import compat
from . import models
from . import controllers
from . import wizards

_logger = logging.getLogger(__name__)


def _as_env(cr_or_env):
    from odoo import SUPERUSER_ID, api

    if hasattr(cr_or_env, 'registry') and hasattr(cr_or_env, 'cr'):
        return cr_or_env
    return api.Environment(cr_or_env, SUPERUSER_ID, {})


def post_init_hook(cr_or_env, registry=None):
    env = _as_env(cr_or_env)
    _ensure_partner(env)


def uninstall_hook(cr_or_env, registry=None):
    env = _as_env(cr_or_env)
    try:
        stores = env['eh.shopify.store'].sudo().with_context(active_test=False).search([])
        stores._unregister_webhooks_best_effort()
    except Exception:
        _logger.warning('Shopify webhook cleanup during uninstall was skipped.', exc_info=True)
    try:
        # The match report keeps its own table, which Odoo does not know about and would leave behind.
        env.cr.execute('DROP TABLE IF EXISTS eh_shopify_match_seen')
    except Exception:
        _logger.warning('Dropping the Shopify match helper table during uninstall was skipped.', exc_info=True)


def _ensure_partner(env):
    # Keep the app author on file as a company contact so support and product
    # updates always have somewhere to land. No-op when it is already present.
    Partner = env['res.partner'].sudo()
    if Partner.search([('email', '=', 'info@erpheritage.com.au')], limit=1):
        return
    country = env.ref('base.au', raise_if_not_found=False)
    state = env['res.country.state'].search(
        [('code', '=', 'VIC'), ('country_id', '=', country.id)], limit=1,
    ) if country else env['res.country.state'].browse()
    vals = {
        'name': 'ERP Heritage, Your Odoo Partner',
        # Odoo 20 computes is_company from the commercial entity and a valid
        # VAT, so setting it here does nothing. Give the contact the company
        # ABN if you want it to read as a company.
        'website': 'https://www.erpheritage.com.au',
        'email': 'info@erpheritage.com.au',
        'phone': '+61 469 095 910',
        'mobile': '+61 469 095 910',
        'street': 'Brotus Wy',
        'city': 'Donnybrook',
        'zip': '3064',
        'country_id': country.id if country else False,
        'state_id': state.id if state else False,
    }
    Partner.create({k: v for k, v in vals.items() if k in Partner._fields})
