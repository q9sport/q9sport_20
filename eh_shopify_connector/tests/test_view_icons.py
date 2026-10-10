# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from lxml import etree

from odoo.addons.web.icons import ICONS
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'eh_shopify')
class TestViewIcons(TransactionCase):
    """Button icons name glyphs of the web client's icon font.

    The web client draws a button icon as a font ligature of its name, so a
    name outside the shipped set (an old Font Awesome class such as fa-refresh)
    prints as literal text on the button instead of failing anywhere.
    """

    def test_every_button_icon_is_in_the_icon_set(self):
        views = self.env['ir.model.data'].search([
            ('module', '=', 'eh_shopify_connector'), ('model', '=', 'ir.ui.view')])
        self.assertTrue(views)
        checked = 0
        for view in self.env['ir.ui.view'].browse(views.mapped('res_id')):
            for node in etree.fromstring(view.arch_db.encode()).iter():
                icon = node.get('icon')
                if not icon or node.tag not in ('button', 'a'):
                    continue
                checked += 1
                with self.subTest(view=view.xml_id, icon=icon):
                    self.assertTrue(icon in ICONS or icon.startswith('oi_'),
                                    '%s uses %s, which the web client cannot draw' % (view.xml_id, icon))
        self.assertGreaterEqual(checked, 17, 'the store, order, job, bulk and catalogue check buttons are covered')
