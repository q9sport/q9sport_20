from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestPosUserRestrict(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cashier = new_test_user(cls.env, 'tamadoo_cashier', groups='base.group_user,point_of_sale.group_pos_user')
        cls.other = new_test_user(cls.env, 'tamadoo_cashier2', groups='base.group_user,point_of_sale.group_pos_user')
        cls.manager = new_test_user(cls.env, 'tamadoo_pos_manager', groups='base.group_user,point_of_sale.group_pos_manager')
        Config = cls.env['pos.config']
        cls.open_shop = Config.create({'name': 'Open shop'})
        cls.my_shop = Config.create({'name': 'My shop', 'allowed_user_ids': [(6, 0, cls.cashier.ids)]})
        cls.other_shop = Config.create({'name': 'Other shop', 'allowed_user_ids': [(6, 0, cls.other.ids)]})
        cls.shops = cls.open_shop | cls.my_shop | cls.other_shop

    def _visible(self, user):
        return self.env['pos.config'].with_user(user).search([('id', 'in', self.shops.ids)])

    def test_cashier_sees_allowed_and_open_shops(self):
        self.assertEqual(self._visible(self.cashier), self.open_shop | self.my_shop)
        self.assertEqual(self._visible(self.other), self.open_shop | self.other_shop)

    def test_cashier_cannot_read_other_shop(self):
        with self.assertRaises(AccessError):
            self.other_shop.with_user(self.cashier).read(['name'])
        self.assertEqual(self.my_shop.with_user(self.cashier).read(['name'])[0]['name'], 'My shop')

    def test_manager_sees_every_shop(self):
        self.assertEqual(self._visible(self.manager), self.shops)

    def test_settings_field(self):
        settings = self.env['res.config.settings'].create({'pos_config_id': self.open_shop.id})
        settings.pos_allowed_user_ids = self.other
        settings.execute()
        self.assertEqual(self.open_shop.allowed_user_ids, self.other)
        self.assertEqual(self._visible(self.cashier), self.my_shop)


@tagged('post_install', '-at_install')
class TestPosUserRestrictLink(HttpCase):

    def test_direct_link_to_other_shop(self):
        new_test_user(self.env, 'tamadoo_link_cashier', password='tamadoo_link_cashier', groups='base.group_user,point_of_sale.group_pos_user')
        other = new_test_user(self.env, 'tamadoo_link_other', groups='base.group_user,point_of_sale.group_pos_user')
        shop = self.env['pos.config'].create({'name': 'Forbidden shop', 'allowed_user_ids': [(6, 0, other.ids)]})
        self.authenticate('tamadoo_link_cashier', 'tamadoo_link_cashier')
        for url in ('/pos/ui/%s' % shop.id, '/pos/ui?config_id=%s' % shop.id):
            response = self.url_open(url, allow_redirects=False)
            self.assertEqual(response.status_code, 303, url)
            self.assertIn('action_client_pos_menu', response.headers['Location'], url)
        self.assertFalse(shop.session_ids)
