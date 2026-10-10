# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Amount parity on hand computed orders shaped exactly like Shopify payloads."""
from decimal import Decimal

from odoo.tests import BaseCase, tagged

from ..lib import documents, errors, order_math


def bag(shop, presentment=None, shop_currency='USD', presentment_currency=None):
    return {'shopMoney': {'amount': str(shop), 'currencyCode': shop_currency},
            'presentmentMoney': {'amount': str(presentment if presentment is not None else shop),
                                 'currencyCode': presentment_currency or shop_currency}}


def tax(title, rate, amount, presentment=None, currency='USD', presentment_currency=None):
    return {'title': title, 'rate': rate, 'ratePercentage': rate * 100, 'channelLiable': False, 'source': 'Shopify',
            'priceSet': bag(amount, presentment, currency, presentment_currency)}


def allocation(amount, index=0, presentment=None, currency='USD', presentment_currency=None):
    return {'allocatedAmountSet': bag(amount, presentment, currency, presentment_currency),
            'discountApplication': {'index': index}}


def line(gid, quantity, unit, allocations=(), taxes=(), current=None, gift_card=False, duties=(), currency='USD',
         presentment_unit=None, presentment_currency=None, taxable=True):
    return {'id': gid, 'sku': 'SKU-%s' % gid, 'name': 'Item %s' % gid, 'quantity': quantity,
            'currentQuantity': quantity if current is None else current, 'isGiftCard': gift_card, 'taxable': taxable,
            'requiresShipping': True, 'variant': {'id': 'gid://shopify/ProductVariant/%s' % gid, 'sku': 'SKU-%s' % gid},
            'product': {'id': 'gid://shopify/Product/%s' % gid},
            'originalUnitPriceSet': bag(unit, presentment_unit, currency, presentment_currency),
            'discountAllocations': list(allocations), 'taxLines': list(taxes), 'duties': list(duties)}


def order(lines, total, shipping=(), taxes_included=False, total_price=None, refunded='0.00', tip='0.00', fees=None,
          duties_included=False, currency='USD', presentment_total=None, presentment_currency=None, tip_presentment=None):
    data = {'name': '#1001', 'currencyCode': currency, 'presentmentCurrencyCode': presentment_currency or currency,
            'taxesIncluded': taxes_included, 'dutiesIncluded': duties_included,
            'lineItems': {'nodes': list(lines), 'pageInfo': {'hasNextPage': False, 'endCursor': None}},
            'shippingLines': {'nodes': list(shipping), 'pageInfo': {'hasNextPage': False, 'endCursor': None}},
            'currentTotalPriceSet': bag(total, presentment_total, currency, presentment_currency),
            'totalRefundedSet': bag(refunded, refunded, currency, presentment_currency),
            'totalTipReceivedSet': bag(tip, tip_presentment, currency, presentment_currency)}
    if total_price is not None:
        data['totalPriceSet'] = bag(total_price, total_price, currency, presentment_currency)
    if fees is not None:
        data['currentTotalAdditionalFeesSet'] = bag(fees, fees, currency, presentment_currency)
    return data


@tagged('post_install', '-at_install', 'eh_shopify')
class TestOrderAmounts(BaseCase):

    def _us_order(self, total='29.77'):
        # 2 x 10.00 and 1 x 5.00 with a 10 percent code discount allocated per line, 8.25 percent tax,
        # 7.00 shipping with a 2.00 shipping discount, tax on shipping.
        lines = [
            line('1', 2, '10.00', [allocation('2.00')], [tax('State tax', 0.0825, '1.49')]),
            line('2', 1, '5.00', [allocation('0.50')], [tax('State tax', 0.0825, '0.37')]),
        ]
        shipping = [{'id': 'gid://shopify/ShippingLine/1', 'title': 'Express', 'code': 'EXP', 'isRemoved': False,
                     'originalPriceSet': bag('7.00'), 'discountAllocations': [allocation('2.00', 1)],
                     'taxLines': [tax('State tax', 0.0825, '0.41')]}]
        return order(lines, total, shipping=shipping)

    def test_tax_excluded_with_line_and_shipping_discounts(self):
        result = order_math.OrderAmounts(self._us_order()).compute()
        self.assertEqual(result['basis'], 'current')
        self.assertEqual(result['net_total'], Decimal('27.50'))
        self.assertEqual(result['tax_total'], Decimal('2.27'))
        self.assertEqual(result['computed_total'], Decimal('29.77'))
        self.assertEqual(result['residual'], Decimal('0.00'))
        self.assertTrue(result['within_tolerance'])
        first = result['lines'][0]
        self.assertEqual((first['gross'], first['discount'], first['net']),
                         (Decimal('20.00'), Decimal('2.00'), Decimal('18.00')))
        self.assertEqual(first['discount_percent'], Decimal('10'))
        self.assertEqual(result['shipping'][0]['net'], Decimal('5.00'))

    def test_mismatch_is_reported_not_hidden(self):
        result = order_math.OrderAmounts(self._us_order(total='30.77')).compute()
        self.assertEqual(result['residual'], Decimal('1.00'))
        self.assertEqual(result['tolerance'], Decimal('0.05'))
        self.assertFalse(result['within_tolerance'])

    def test_tax_included_prices(self):
        lines = [line('1', 3, '3.33', [allocation('1.00')], [tax('GST', 0.10, '0.82')], currency='AUD')]
        shipping = [{'id': 's', 'title': 'Standard', 'isRemoved': False, 'originalPriceSet': bag('10.00', shop_currency='AUD'),
                     'discountAllocations': [], 'taxLines': [tax('GST', 0.10, '0.91', currency='AUD')]}]
        result = order_math.OrderAmounts(order(lines, '18.99', shipping=shipping, taxes_included=True,
                                               currency='AUD')).compute()
        self.assertTrue(result['taxes_included'])
        self.assertEqual(result['tax_total'], Decimal('1.73'))
        self.assertEqual(result['computed_total'], Decimal('18.99'))
        self.assertTrue(result['within_tolerance'])
        self.assertEqual(result['currency'], 'AUD')

    def test_presentment_currency_tip_and_duties(self):
        duty = {'id': 'gid://shopify/Duty/1', 'harmonizedSystemCode': '6109', 'countryCodeOfOrigin': 'CN',
                'price': bag('5.00', '4.60', 'USD', 'EUR'), 'taxLines': []}
        lines = [line('1', 1, '100.00', duties=[duty], presentment_unit='92.00', presentment_currency='EUR',
                      taxable=False)]
        data = order(lines, '115.00', presentment_total='105.80', presentment_currency='EUR', tip='10.00',
                     tip_presentment='9.20')
        shop = order_math.OrderAmounts(data).compute()
        self.assertEqual(shop['duty_total'], Decimal('5.00'))
        self.assertTrue(shop['tip_in_total'])
        self.assertEqual(shop['residual'], Decimal('0.00'))
        self.assertEqual(shop['currency'], 'USD')
        presentment = order_math.OrderAmounts(data, currency='presentment').compute()
        self.assertEqual(presentment['computed_total'], Decimal('105.80'))
        self.assertEqual(presentment['currency'], 'EUR')
        self.assertTrue(presentment['within_tolerance'])

    def test_additional_fees_detected(self):
        lines = [line('1', 1, '40.00')]
        result = order_math.OrderAmounts(order(lines, '42.00', fees='2.00')).compute()
        self.assertTrue(result['fees_in_total'])
        self.assertEqual(result['residual'], Decimal('0.00'))

    def test_refunded_units_import_as_sold(self):
        # 3 units sold, 2 refunded; allocations and taxes are reported for all 3 units.
        lines = [line('1', 3, '20.00', [allocation('6.00')], [tax('VAT', 0.10, '5.40')], current=1)]
        data = order(lines, '19.80', total_price='59.40', refunded='39.60')
        with self.assertRaises(errors.IncompleteData):
            order_math.OrderAmounts(data).compute()
        known = order_math.OrderAmounts(data, refunded_quantities={'1': 2}).compute()
        self.assertEqual(known['basis'], 'before_refunds')
        self.assertEqual(known['lines'][0]['quantity'], 3)
        self.assertEqual(known['target_total'], Decimal('59.40'))
        self.assertTrue(known['within_tolerance'])
        self.assertFalse(known['warnings'])

    def test_units_removed_by_edit_are_prorated(self):
        lines = [line('1', 3, '20.00', [allocation('6.00')], [tax('VAT', 0.10, '5.40')], current=2)]
        result = order_math.OrderAmounts(order(lines, '39.60')).compute()
        item = result['lines'][0]
        self.assertEqual((item['quantity'], item['discount'], item['taxes'][0]['amount']),
                         (2, Decimal('4.00'), Decimal('3.60')))
        self.assertEqual(result['computed_total'], Decimal('39.60'))

    def test_removed_lines_and_gift_cards(self):
        lines = [line('1', 1, '50.00', gift_card=True, taxable=False), line('2', 2, '9.00', current=0)]
        result = order_math.OrderAmounts(order(lines, '50.00')).compute()
        self.assertEqual([l['kind'] for l in result['lines']], ['gift_card'])
        self.assertTrue(result['within_tolerance'])

    def test_tip_line_item_is_a_tip_not_a_product(self):
        tip = {'id': 'gid://shopify/LineItem/99', 'title': 'Tip', 'name': 'Tip', 'sku': None, 'vendor': None,
               'product': None, 'variant': None, 'requiresShipping': False, 'quantity': 1, 'currentQuantity': 1,
               'isGiftCard': False, 'taxable': False, 'originalUnitPriceSet': bag('5.00'),
               'discountAllocations': [], 'taxLines': [], 'duties': []}
        product_named_tip = line('7', 1, '12.00')
        product_named_tip['title'] = 'Tip'
        data = order([line('1', 1, '20.00'), tip, product_named_tip], '37.00', tip='5.00')
        result = order_math.OrderAmounts(data).compute()
        self.assertEqual([l['id'] for l in result['lines']], ['1', '7'])
        self.assertEqual(result['tip_lines'][0]['amount'], Decimal('5.00'))
        self.assertTrue(result['tip_in_total'])
        self.assertEqual(result['residual'], Decimal('0.00'))

    def test_additional_fees_with_taxes_are_exact(self):
        data = order([line('1', 1, '40.00')], '44.20')
        data['additionalFees'] = [{'id': 'gid://shopify/AdditionalFee/1', 'name': 'Bottle deposit',
                                   'price': bag('4.00'), 'taxLines': [tax('GST', 0.05, '0.20')]}]
        result = order_math.OrderAmounts(data).compute()
        self.assertEqual(result['fee_lines'][0]['net'], Decimal('4.00'))
        self.assertEqual(result['tax_total'], Decimal('0.20'))
        self.assertEqual(result['residual'], Decimal('0.00'))

    def test_cash_rounding_is_not_part_of_the_price(self):
        data = order([line('1', 1, '9.98')], '9.98')
        data['totalCashRoundingAdjustment'] = {'paymentSet': bag('0.02'), 'refundSet': bag('0.00')}
        result = order_math.OrderAmounts(data).compute()
        self.assertEqual(result['residual'], Decimal('0.00'))
        self.assertEqual(result['cash_rounding'], Decimal('0.02'))

    def test_odoo_discount_rounding_residual(self):
        self.assertEqual(order_math.odoo_line_discount(Decimal('20.00'), Decimal('2.00')),
                         (Decimal('10.00'), Decimal('0.00')))
        percent, residual = order_math.odoo_line_discount(Decimal('1000.00'), Decimal('333.33'))
        self.assertEqual(percent, Decimal('33.33'))
        self.assertEqual(residual, Decimal('0.03'))


@tagged('post_install', '-at_install', 'eh_shopify')
class TestScopeGates(BaseCase):

    def test_optional_field_removed_without_scope(self):
        full = documents.ORDER.text_for('2026-07')
        self.assertIn('paymentTerms', full)
        stripped = documents.ORDER.text_for('2026-07', scopes={'read_orders'})
        self.assertNotIn('paymentTerms', stripped)
        self.assertNotIn('@requires', stripped)
        kept = documents.ORDER.text_for('2026-07', scopes={'read_orders', 'write_payment_terms'})
        self.assertIn('paymentTerms', kept)
        self.assertEqual(documents.ORDER.gated_scopes(), {'read_payment_terms'})
