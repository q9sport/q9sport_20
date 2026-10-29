# -*- coding: utf-8 -*-

from odoo.tests import tagged
from odoo.addons.point_of_sale.tests.test_frontend import TestPointOfSaleHttpCommon


@tagged('post_install', '-at_install')
class TestPosStockGuardTour(TestPointOfSaleHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Template = cls.env['product.template']
        values = {'available_in_pos': True, 'list_price': 10.0, 'taxes_id': False}
        cls.guard_desk = Template.create({**values, 'name': 'Guard Desk', 'type': 'consu', 'is_storable': True})
        cls.guard_empty = Template.create({**values, 'name': 'Guard Empty', 'type': 'consu', 'is_storable': True})
        Template.create({**values, 'name': 'Guard Fee', 'type': 'service'})
        location = cls.main_pos_config._get_stock_guard_location()
        cls.env['stock.quant']._update_available_quantity(cls.guard_desk.product_variant_id, location, 2)

    def test_block_mode(self):
        """A cashier can't add or raise a storable product above its stock; services are free."""
        self.main_pos_config.write({'stock_guard_mode': 'block'})
        self.start_pos_tour('PosStockGuardBlockTour')

    def test_warn_mode(self):
        """Warn asks for confirmation and the badge goes below zero."""
        self.main_pos_config.write({'stock_guard_mode': 'warn'})
        self.start_pos_tour('PosStockGuardConfirmTour')

    def test_block_mode_override_group(self):
        """In Block mode a user of the override group gets the confirmation instead."""
        self.main_pos_config.write({
            'stock_guard_mode': 'block',
            'stock_guard_override_group_id': self.env.ref('point_of_sale.group_pos_user').id,
        })
        self.start_pos_tour('PosStockGuardConfirmTour')
