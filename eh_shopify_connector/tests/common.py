# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import TransactionCase

from .. import compat
from ..lib import testing


class ShopifyCase(TransactionCase):
    """Store wired to an offline fake Shopify. Every test gets a fresh fake."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, eh_shopify_inline=True, tracking_disable=True,
                                       mail_create_nolog=True, mail_notrack=True))
        cls.env['ir.config_parameter'].sudo().set_str('web.base.url', 'https://erp.example.com')
        cls.domain = 'eh-test.myshopify.com'
        cls.store = cls.env['eh.shopify.store'].create({
            'name': 'Test store',
            'shop_domain': cls.domain,
            'auth_mode': 'client_credentials',
            'client_id': 'eh-client-id',
        })
        cls.store._credential()._set_secrets(client_secret='eh-client-secret')

    def setUp(self):
        super().setUp()
        self.fake = testing.FakeTransport(testing.FakeShop(domain=self.domain))
        self.shop = self.fake.shop
        testing.TRANSPORT_OVERRIDES[self.domain] = self.fake
        self.addCleanup(testing.TRANSPORT_OVERRIDES.pop, self.domain, None)

    def _user(self, login, groups, company=None):
        company = company or self.env.company
        vals = {
            'name': login,
            'login': login,
            'email': '%s@example.com' % login,
            'company_id': company.id,
            'company_ids': [(6, 0, company.ids)],
            compat.user_groups_field(): [(6, 0, [self.env.ref(g).id for g in groups])],
        }
        return self.env['res.users'].with_context(no_reset_password=True).create(vals)
