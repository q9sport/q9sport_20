# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .. import compat
from .common import ShopifyCase

ITEM = 'gid://shopify/InventoryItem/0'
PARTNER = 'gid://shopify/Location/9'
KEY = (ITEM, PARTNER)


@tagged('post_install', '-at_install', 'eh_shopify')
class TestStockCompare(ShopifyCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vals = {'name': 'Stocked gear', 'default_code': 'SKU-0'}
        vals.update(compat.product_type_vals(cls.env, 'storable'))
        cls.product = cls.env['product.template'].create(vals).product_variant_id
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.env.company.id)], limit=1)
        cls.shelf = cls.env['stock.location'].create({'name': 'Partner shelf', 'usage': 'internal',
                                                      'location_id': cls.warehouse.view_location_id.id})

    def setUp(self):
        super().setUp()
        self.store._check_connection()
        self.env['eh.shopify.variant'].create({
            'store_id': self.store.id, 'shopify_gid': 'gid://shopify/ProductVariant/0', 'inventory_item_gid': ITEM,
            'product_id': self.product.id, 'sku': 'SKU-0', 'match_method': 'sku'})
        self.location = self.env['eh.shopify.location'].create({
            'store_id': self.store.id, 'name': 'Logistics partner', 'shopify_gid': PARTNER, 'active_in_shopify': True,
            'delivery_mode': 'shopify', 'sync_stock': False, 'location_ids': [(6, 0, self.shelf.ids)]})
        self.shop.inventory[KEY] = 5
        self.shop.inventory_committed[KEY] = 2
        self.Wizard = self.env['eh.shopify.stock.compare.wizard']

    def _set_on_hand(self, quantity):
        self.env.flush_all()
        product = self.product.with_context(location=self.shelf.id)
        product.invalidate_recordset(['qty_available'])
        self.env['stock.quant']._update_available_quantity(self.product, self.shelf, quantity - product.qty_available)
        self.env.flush_all()

    def _on_hand(self):
        self.env.flush_all()
        product = self.product.with_context(location=self.shelf.id)
        product.invalidate_recordset(['qty_available'])
        return product.qty_available

    def _wizard(self, location=None):
        action = (location or self.location).action_compare_stock()
        return self.Wizard.browse(action['res_id'])

    def test_difference_is_previewed_then_applied(self):
        self._set_on_hand(4)
        wizard = self._wizard()
        line = wizard.line_ids
        self.assertEqual((len(line), line.odoo_quantity, line.shopify_quantity, line.apply), (1, 4.0, 7.0, True))
        self.assertEqual(self._on_hand(), 4.0, 'nothing changes before the user applies')
        wizard.action_apply()
        self.assertEqual(self._on_hand(), 7.0)

    def test_matching_stock_shows_no_lines(self):
        self._set_on_hand(7)
        wizard = self._wizard()
        self.assertFalse(wizard.line_ids)
        self.assertTrue(wizard.note)

    def test_unticked_lines_are_left_alone(self):
        self._set_on_hand(4)
        wizard = self._wizard()
        wizard.line_ids.apply = False
        wizard.action_apply()
        self.assertEqual(self._on_hand(), 4.0)

    def test_locations_odoo_ships_are_never_overwritten(self):
        self.location.delivery_mode = 'odoo'
        with self.assertRaises(UserError):
            self._wizard()

    def test_only_inventory_administrators_apply_counts(self):
        self._set_on_hand(4)
        user = self._user('eh_partner_clerk', ['eh_shopify_connector.group_shopify_manager', 'stock.group_stock_user'])
        wizard = self._wizard(self.location.with_user(user))
        with self.assertRaises(AccessError):
            wizard.with_user(user).action_apply()
        self.assertEqual(self._on_hand(), 4.0)

    def test_every_mapped_location_is_counted(self):
        second = self.env['stock.location'].create({'name': 'Partner shelf B', 'usage': 'internal',
                                                    'location_id': self.warehouse.view_location_id.id})
        self.location.location_ids = [(4, second.id)]
        self._set_on_hand(3)
        self.env['stock.quant']._update_available_quantity(self.product, second, 2)
        self.env.flush_all()
        wizard = self._wizard()
        self.assertEqual((wizard.line_ids.odoo_quantity, wizard.line_ids.shopify_quantity), (5.0, 7.0))
        wizard.action_apply()
        product = self.product.with_context(location=(self.shelf | second).ids)
        product.invalidate_recordset(['qty_available'])
        self.assertEqual(product.qty_available, 7.0)

    def test_a_tampered_dialog_is_refused(self):
        self._set_on_hand(4)
        wizard = self._wizard()
        wizard.stock_location_id = self.warehouse.lot_stock_id
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assertEqual(self._on_hand(), 4.0)
