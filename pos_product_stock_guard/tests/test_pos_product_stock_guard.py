# -*- coding: utf-8 -*-

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPosProductStockGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env['pos.config'].create({'name': 'Stock Guard Shop'})
        cls.stock_location = cls.config.picking_type_id.default_location_src_id
        cls.shelf = cls.env['stock.location'].create({
            'name': 'Shelf A', 'usage': 'internal', 'location_id': cls.stock_location.id,
        })
        cls.backroom = cls.env['stock.location'].create({
            'name': 'Back Room', 'usage': 'internal',
            'location_id': cls.stock_location.warehouse_id.view_location_id.id,
        })
        Product = cls.env['product.product']
        cls.desk = Product.create({'name': 'Guard Desk', 'type': 'consu', 'is_storable': True, 'available_in_pos': True})
        cls.lamp = Product.create({'name': 'Guard Lamp', 'type': 'consu', 'is_storable': True, 'available_in_pos': True})
        cls.bag = Product.create({'name': 'Guard Bag', 'type': 'consu', 'is_storable': False, 'available_in_pos': True})
        cls.fee = Product.create({'name': 'Guard Fee', 'type': 'service', 'available_in_pos': True})
        Quant = cls.env['stock.quant']
        Quant._update_available_quantity(cls.desk, cls.stock_location, 10)
        Quant._update_available_quantity(cls.desk, cls.shelf, 4)
        Quant._update_available_quantity(cls.desk, cls.backroom, 7)

    def _quantities(self, products=None):
        products = products or (self.desk | self.lamp | self.bag | self.fee)
        return self.config._get_stock_guard_quantities(products)

    def test_defaults(self):
        """A new point of sale shows on-hand stock, warns, and lets POS administrators override."""
        self.assertTrue(self.config.stock_guard_show)
        self.assertEqual(self.config.stock_guard_qty_type, 'on_hand')
        self.assertEqual(self.config.stock_guard_mode, 'warn')
        self.assertFalse(self.config.stock_guard_location_id)
        self.assertEqual(self.config._get_stock_guard_location(), self.stock_location)
        self.assertEqual(self.config.stock_guard_override_group_id, self.env.ref('point_of_sale.group_pos_manager'))

    def test_quantity_per_location(self):
        """The default location counts its sub-locations; another location replaces it."""
        self.assertEqual(self._quantities()[self.desk.id], 14)
        self.assertEqual(self._quantities()[self.lamp.id], 0, "Storable without stock is tracked at 0")
        self.config.stock_guard_location_id = self.backroom
        self.assertEqual(self._quantities()[self.desk.id], 7)
        self.config.stock_guard_location_id = self.shelf
        self.assertEqual(self._quantities()[self.desk.id], 4)

    def test_quantity_types(self):
        """Free to Use removes reserved units, Forecasted removes planned deliveries."""
        customer = self.env.ref('stock.stock_location_customers')
        move = self.env['stock.move'].create({
            'product_id': self.desk.id,
            'product_uom_qty': 3,
            'location_id': self.stock_location.id,
            'location_dest_id': customer.id,
        })
        move._action_confirm()
        move._action_assign()
        self.assertEqual(self._quantities()[self.desk.id], 14)
        self.config.stock_guard_qty_type = 'free'
        self.assertEqual(self._quantities()[self.desk.id], 11)
        self.config.stock_guard_qty_type = 'forecast'
        self.assertEqual(self._quantities()[self.desk.id], 11)
        move._do_unreserve()
        self.config.stock_guard_qty_type = 'free'
        self.assertEqual(self._quantities()[self.desk.id], 14)

    def test_non_storable_excluded(self):
        """Consumables and services are never tracked, so they are never blocked."""
        quantities = self._quantities()
        self.assertNotIn(self.bag.id, quantities)
        self.assertNotIn(self.fee.id, quantities)
        self.assertEqual(self._quantities(self.bag | self.fee), {})

    def test_register_data(self):
        """The register call returns string keys, the location and the override right."""
        data = self.config.get_stock_guard_data([self.desk.id, self.fee.id, 0])
        self.assertEqual(data['quantities'], {str(self.desk.id): 14})
        self.assertEqual(data['location_name'], self.stock_location.display_name)
        self.assertTrue(data['can_override'], "The admin is a POS administrator")
        self.config.write({'stock_guard_show': False, 'stock_guard_mode': 'none'})
        self.assertEqual(self.config.get_stock_guard_data([self.desk.id])['quantities'], {})

    def test_override_group(self):
        """Only users of the override group can sell above the stock in Block mode."""
        cashier = self.env['res.users'].create({
            'name': 'Guard Cashier', 'login': 'guard_cashier',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id, self.env.ref('point_of_sale.group_pos_user').id])],
        })
        config = self.config.with_user(cashier)
        self.assertFalse(config.get_stock_guard_data([self.desk.id])['can_override'])
        self.assertEqual(config.get_stock_guard_data([self.desk.id])['quantities'], {str(self.desk.id): 14})
        self.config.stock_guard_override_group_id = self.env.ref('point_of_sale.group_pos_user')
        self.assertTrue(config.get_stock_guard_data([self.desk.id])['can_override'])
        self.config.stock_guard_override_group_id = False
        self.assertFalse(self.config._stock_guard_user_can_override(), "No group: nobody overrides")

    def test_settings_loaded_in_register(self):
        """The settings travel to the register with the point of sale data."""
        self.config.stock_guard_mode = 'block'
        record = self.config._load_pos_data_read(self.config, self.config)[0]
        for field in ('stock_guard_show', 'stock_guard_qty_type', 'stock_guard_mode'):
            self.assertIn(field, record)
        self.assertEqual(record['stock_guard_mode'], 'block')
        settings = self.env['res.config.settings'].create({'pos_config_id': self.config.id})
        self.assertEqual(settings.pos_stock_guard_mode, 'block')
        settings.pos_stock_guard_qty_type = 'forecast'
        settings.execute()
        self.assertEqual(self.config.stock_guard_qty_type, 'forecast')
