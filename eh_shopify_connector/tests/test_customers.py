# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from .common import ShopifyCase


def address(**kw):
    base = {'firstName': 'Ada', 'lastName': 'Lovelace', 'name': 'Ada Lovelace', 'company': None,
            'address1': '1 Engine St', 'address2': None, 'city': 'Melbourne', 'zip': '3000', 'province': 'Victoria',
            'provinceCode': 'VIC', 'countryCodeV2': 'AU', 'phone': None}
    base.update(kw)
    return base


def company_address(**kw):
    base = {'firstName': None, 'lastName': None, 'recipient': 'Accounts payable',
            'companyName': 'Analytical Engines Pty Ltd', 'address1': '9 Babbage Rd', 'address2': None, 'city': 'Sydney',
            'zip': '2000', 'zoneCode': 'NSW', 'countryCode': 'AU', 'phone': None}
    base.update(kw)
    return base


def order(**kw):
    base = {'name': '#1001', 'email': 'Ada@Example.com', 'phone': None, 'taxExempt': False,
            'customer': {'id': 'gid://shopify/Customer/1', 'displayName': 'Ada Lovelace', 'firstName': 'Ada',
                         'lastName': 'Lovelace', 'taxExempt': False, 'tags': ['vip'], 'locale': 'en',
                         'defaultEmailAddress': {'emailAddress': 'Ada@Example.com', 'marketingState': 'SUBSCRIBED'},
                         'defaultPhoneNumber': None},
            'billingAddress': address(), 'shippingAddress': address(), 'purchasingEntity': None}
    base.update(kw)
    return base


@tagged('post_install', '-at_install', 'eh_shopify')
class TestCustomerResolution(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.Customer = self.env['eh.shopify.customer']

    def test_new_customer_created_once_and_linked(self):
        first = self.Customer._resolve(self.store, order())
        partner = first['partner']
        self.assertEqual(partner.name, 'Ada Lovelace')
        self.assertEqual(partner.email, 'ada@example.com')
        self.assertEqual(partner.country_id.code, 'AU')
        self.assertEqual(partner.state_id.code, 'VIC')
        self.assertEqual(first['invoice'], partner)
        self.assertEqual(first['shipping'], partner)
        self.assertEqual(first['binding'].email_marketing_state, 'SUBSCRIBED')
        self.assertEqual(first['match_method'], 'created')
        second = self.Customer._resolve(self.store, order(name='#1002'))
        self.assertEqual(second['partner'], partner)
        self.assertEqual(second['match_method'], 'binding')

    def test_existing_contact_matched_by_email_not_duplicated(self):
        existing = self.env['res.partner'].create({'name': 'Ada L.', 'email': 'ada@example.com'})
        result = self.Customer._resolve(self.store, order())
        self.assertEqual(result['partner'], existing)
        self.assertEqual(result['match_method'], 'email')
        self.assertEqual(existing.name, 'Ada L.')

    def test_new_shipping_address_becomes_reused_delivery_contact(self):
        self.Customer._resolve(self.store, order())
        other = address(address1='99 Harbour Rd', city='Sydney', provinceCode='NSW', zip='2000')
        result = self.Customer._resolve(self.store, order(shippingAddress=other))
        delivery = result['shipping']
        self.assertNotEqual(delivery, result['partner'])
        self.assertEqual(delivery.parent_id, result['partner'])
        self.assertEqual(delivery.type, 'delivery')
        self.assertEqual(delivery.state_id.code, 'NSW')
        again = self.Customer._resolve(self.store, order(shippingAddress=other))
        self.assertEqual(again['shipping'], delivery)

    def test_guest_checkout_without_names(self):
        guest = order(customer=None, email=None, phone='+61 400 000 000',
                      billingAddress=None, shippingAddress=None, name='#1050')
        result = self.Customer._resolve(self.store, guest)
        self.assertEqual(result['partner'].name, '+61 400 000 000')
        self.assertFalse(result['binding'])

    def test_unknown_country_is_left_empty(self):
        result = self.Customer._resolve(self.store, order(
            billingAddress=address(countryCodeV2='ZZ', provinceCode='XX'), shippingAddress=None))
        self.assertFalse(result['partner'].country_id)
        self.assertEqual(result['partner'].city, 'Melbourne')

    def test_b2b_company_and_contact(self):
        purchasing = {'__typename': 'PurchasingCompany',
                      'company': {'id': 'gid://shopify/Company/7', 'name': 'Analytical Engines Pty Ltd',
                                  'externalId': 'AE-7'},
                      'location': {'id': 'gid://shopify/CompanyLocation/8', 'name': 'Head office',
                                   'externalId': None,
                                   'taxSettings': {'taxRegistrationId': '51824753556', 'taxExempt': False},
                                   'billingAddress': company_address(), 'shippingAddress': company_address()},
                      'contact': {'id': 'gid://shopify/CompanyContact/9'}}
        result = self.Customer._resolve(self.store, order(purchasingEntity=purchasing))
        company = result['partner']
        self.assertTrue(company.is_company)
        self.assertEqual(company.name, 'Analytical Engines Pty Ltd')
        self.assertEqual(company.vat, '51824753556')
        self.assertEqual(result['contact'].parent_id, company)
        again = self.Customer._resolve(self.store, order(purchasingEntity=purchasing, name='#1003'))
        self.assertEqual(again['partner'], company)

    def _purchasing(self, number, **location):
        base = {'id': 'gid://shopify/CompanyLocation/%s' % number, 'name': 'Branch', 'externalId': None,
                'taxSettings': {'taxRegistrationId': None, 'taxExempt': False},
                'billingAddress': company_address(), 'shippingAddress': company_address(city='Perth', zip='6000',
                                                                                         zoneCode='WA')}
        base.update(location)
        return {'__typename': 'PurchasingCompany',
                'company': {'id': 'gid://shopify/Company/%s' % number, 'name': 'Difference Engines %s' % number,
                            'externalId': None},
                'location': base, 'contact': {'id': 'gid://shopify/CompanyContact/%s' % number}}

    def test_b2b_orders_invoice_the_company_location_and_ship_where_the_order_says(self):
        result = self.Customer._resolve(self.store, order(purchasingEntity=self._purchasing(21)))
        company = result['partner']
        self.assertEqual(result['invoice'].city, 'Sydney', 'the company location invoices')
        self.assertIn(result['invoice'], company | company.child_ids)
        self.assertEqual(result['shipping'].city, 'Melbourne', 'the order keeps its own delivery address')
        no_shipping = self.Customer._resolve(self.store, order(purchasingEntity=self._purchasing(21),
                                                               shippingAddress=None, name='#1004'))
        self.assertEqual(no_shipping['shipping'].city, 'Perth', 'without one, the location delivery address is used')

    def test_b2b_tax_details_come_from_the_location_tax_settings(self):
        position = self.env['account.fiscal.position'].create({'name': 'B2B exempt'})
        self.store.tax_exempt_fiscal_position_id = position
        exempt = self._purchasing(22, taxSettings={'taxRegistrationId': '12345678901', 'taxExempt': True})
        result = self.Customer._resolve(self.store, order(purchasingEntity=exempt))
        self.assertEqual(result['fiscal_position'], position)
        self.assertEqual(result['partner'].vat, '12345678901')
        legacy = self._purchasing(23, taxSettings=None, taxRegistrationId='98765432109')
        self.assertEqual(self.Customer._resolve(self.store, order(purchasingEntity=legacy))['partner'].vat,
                         '98765432109', 'a payload from an older query still fills the tax id')

    def test_tax_exempt_customer_gets_fiscal_position(self):
        position = self.env['account.fiscal.position'].create({'name': 'Tax exempt'})
        self.store.tax_exempt_fiscal_position_id = position
        exempt = order()
        exempt['customer']['taxExempt'] = True
        self.assertEqual(self.Customer._resolve(self.store, exempt)['fiscal_position'], position)


@tagged('post_install', '-at_install', 'eh_shopify')
class TestVariantResolution(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.Variant = self.env['eh.shopify.variant']
        self.product = self.env['product.product'].create({'name': 'Brass gear', 'default_code': 'GEAR-1',
                                                           'barcode': '9300000000017'})

    def _line(self, sku='GEAR-1', barcode=None, variant='gid://shopify/ProductVariant/11'):
        return {'name': 'Brass gear', 'sku': sku,
                'variant': {'id': variant, 'sku': sku, 'barcode': barcode,
                            'inventoryItem': {'id': 'gid://shopify/InventoryItem/21'}} if variant else None,
                'product': {'id': 'gid://shopify/Product/31'}}

    def test_match_by_sku_then_link(self):
        product, reason = self.Variant._resolve_line(self.store, self._line())
        self.assertEqual(product, self.product)
        self.assertIsNone(reason)
        binding = self.Variant.search([('store_id', '=', self.store.id)])
        self.assertEqual((binding.match_method, binding.inventory_item_gid), ('sku', 'gid://shopify/InventoryItem/21'))
        self.product.default_code = 'RENAMED'
        product, reason = self.Variant._resolve_line(self.store, self._line())
        self.assertEqual(product, self.product)

    def test_match_by_barcode(self):
        product, reason = self.Variant._resolve_line(self.store, self._line(sku='', barcode='9300000000017'))
        self.assertEqual(product, self.product)

    def test_ambiguous_sku_is_explained(self):
        self.env['product.product'].create({'name': 'Brass gear copy', 'default_code': 'GEAR-1'})
        product, reason = self.Variant._resolve_line(self.store, self._line())
        self.assertFalse(product)
        self.assertIn('More than one', reason)

    def test_unknown_and_custom_items_are_explained(self):
        product, reason = self.Variant._resolve_line(self.store, self._line(sku='NOPE'))
        self.assertFalse(product)
        self.assertIn('NOPE', reason)
        product, reason = self.Variant._resolve_line(self.store, self._line(variant=None))
        self.assertIn('custom item', reason)

    def test_gateway_and_carrier_mappings(self):
        Gateway = self.env['eh.shopify.gateway']
        self.assertEqual(Gateway._for(self.store, 'gift_card').tender_type, 'gift_card')
        self.assertEqual(Gateway._for(self.store, 'Cash on Delivery (COD)', manual=True).tender_type, 'manual')
        self.assertEqual(Gateway._for(self.store, 'shopify_payments'), Gateway._for(self.store, 'shopify_payments'))
        Carrier = self.env['eh.shopify.carrier']
        first = Carrier._for(self.store, {'title': 'Express', 'code': 'EXP'})
        self.assertEqual(Carrier._for(self.store, {'title': 'express', 'code': 'EXP'}), first)
