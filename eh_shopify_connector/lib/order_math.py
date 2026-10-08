# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Reproduce Shopify order amounts exactly, with Decimal arithmetic.

Shopify semantics this module relies on (shopify.dev, September 2026):

* ``LineItem.quantity`` includes refunded and removed units;
  ``currentQuantity`` excludes both.
* ``discountAllocations``, ``totalDiscountSet`` and ``taxLines`` on a line are
  reported for the full ordered quantity, including refunded and removed
  units, so they are prorated to the quantity we import.
* ``Order.currentTotalPriceSet`` is the total after returns and includes taxes
  and discounts; ``totalPriceSet`` is the same before returns.
* ``taxesIncluded`` and ``dutiesIncluded`` say whether taxes and duties are
  already inside the item prices.

Shopify does not document whether tips and additional fees are inside the
order total, so ``compute`` tries both and keeps the reading that explains the
total, and says which one it used. Nothing here guesses silently.

The import basis is "as sold, before refunds": refunds become credit notes in
Odoo, so the sales order must hold the units that were sold, not the units
left after refunds.
"""
from decimal import ROUND_HALF_UP, Decimal

from .errors import IncompleteData

ZERO = Decimal("0")
HUNDRED = Decimal("100")


def dec(value):
    if value in (None, "", False):
        return ZERO
    return Decimal(str(value))


def quantize(value, digits=2):
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def tolerance(line_count, per_line=Decimal("0.01"), floor=Decimal("0.05")):
    """Rounding noise we accept between Shopify and Odoo totals."""
    return max(floor, per_line * max(1, line_count))


class OrderAmounts(object):
    def __init__(self, order, currency="shop", refunded_quantities=None, digits=2):
        self.order = order or {}
        self.currency = currency
        self.key = "presentmentMoney" if currency == "presentment" else "shopMoney"
        self.refunded_quantities = refunded_quantities
        self.digits = digits
        self.warnings = []

    # ------------------------------------------------------------------ helpers
    def money(self, bag):
        return dec(((bag or {}).get(self.key) or {}).get("amount"))

    def currency_code(self):
        bag = self.order.get("currentTotalPriceSet") or {}
        code = ((bag.get(self.key) or {}).get("currencyCode"))
        if code:
            return code
        return self.order.get("presentmentCurrencyCode" if self.currency == "presentment" else "currencyCode")

    def _q(self, value):
        return quantize(value, self.digits)

    def _nodes(self, key):
        value = self.order.get(key)
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return value.get("nodes") or [edge["node"] for edge in value.get("edges") or []]

    def _taxes(self, tax_lines, factor):
        out = []
        for tax in tax_lines or []:
            out.append({
                "title": tax.get("title"),
                "rate": dec(tax.get("rate")),
                "rate_percentage": dec(tax.get("ratePercentage")),
                "amount": self._q(self.money(tax.get("priceSet")) * factor),
                "channel_liable": bool(tax.get("channelLiable")),
                "source": tax.get("source"),
            })
        return out

    @staticmethod
    def is_tip_line(line):
        """Shopify returns a checkout tip as a line item titled Tip with no
        product, SKU or vendor that needs no shipping."""
        return (not line.get("product") and not line.get("sku") and not line.get("vendor")
                and not line.get("requiresShipping")
                and (line.get("title") or line.get("name") or "").strip().lower() == "tip")

    def _allocations(self, allocations, factor):
        return self._q(sum((self.money(a.get("allocatedAmountSet")) for a in allocations or []), ZERO) * factor)

    # ------------------------------------------------------------------ basis
    def _has_refunds(self):
        return self.money(self.order.get("totalRefundedSet")) > 0

    def _sold_quantity(self, line):
        ordered = int(line.get("quantity") or 0)
        current = int(line.get("currentQuantity") if line.get("currentQuantity") is not None else ordered)
        if self.refunded_quantities is not None:
            return min(ordered, current + int(self.refunded_quantities.get(line.get("id"), 0))), "before_refunds"
        if self._has_refunds():
            raise IncompleteData("Order %s has refunds; refunded quantities are required to import it as sold."
                                 % (self.order.get("name") or ""))
        return current, "current"

    # ------------------------------------------------------------------ compute
    def compute(self):
        order = self.order
        taxes_included = bool(order.get("taxesIncluded"))
        duties_included = bool(order.get("dutiesIncluded"))
        lines = []
        tip_lines = []
        basis = "before_refunds" if self.refunded_quantities is not None else "current"
        for line in self._nodes("lineItems"):
            ordered = int(line.get("quantity") or 0)
            sold, basis = self._sold_quantity(line)
            if sold <= 0:
                continue
            if self.is_tip_line(line):
                tip_lines.append({"kind": "tip", "id": line.get("id"), "name": "Tip",
                                  "amount": self._q(self.money(line.get("originalUnitPriceSet")) * sold)})
                continue
            factor = Decimal(sold) / Decimal(ordered) if ordered else ZERO
            unit = self.money(line.get("originalUnitPriceSet"))
            gross = self._q(unit * sold)
            discount = self._allocations(line.get("discountAllocations"), factor)
            duties = []
            for duty in line.get("duties") or []:
                duties.append({
                    "id": duty.get("id"),
                    "hs_code": duty.get("harmonizedSystemCode"),
                    "country": duty.get("countryCodeOfOrigin"),
                    "amount": self._q(self.money(duty.get("price")) * factor),
                    "taxes": self._taxes(duty.get("taxLines"), factor),
                })
            lines.append({
                "kind": "gift_card" if line.get("isGiftCard") else "product",
                "id": line.get("id"),
                "sku": line.get("sku") or ((line.get("variant") or {}).get("sku")),
                "variant_id": (line.get("variant") or {}).get("id"),
                "product_id": (line.get("product") or {}).get("id"),
                "name": line.get("name") or line.get("title"),
                "quantity": sold,
                "ordered_quantity": ordered,
                "unit_price": unit,
                "gross": gross,
                "discount": discount,
                "discount_percent": (discount / gross * HUNDRED) if gross else ZERO,
                "net": gross - discount,
                "taxes": self._taxes(line.get("taxLines"), factor),
                "duties": duties,
                "taxable": bool(line.get("taxable", True)),
                "requires_shipping": bool(line.get("requiresShipping")),
            })
        shipping = []
        for ship in self._nodes("shippingLines"):
            if ship.get("isRemoved"):
                continue
            gross = self.money(ship.get("originalPriceSet"))
            discount = self._allocations(ship.get("discountAllocations"), Decimal(1))
            shipping.append({
                "kind": "shipping",
                "id": ship.get("id"),
                "title": ship.get("title"),
                "code": ship.get("code"),
                "carrier": ship.get("carrierIdentifier"),
                "source": ship.get("source"),
                "gross": gross,
                "discount": discount,
                "discount_percent": (discount / gross * HUNDRED) if gross else ZERO,
                "net": gross - discount,
                "taxes": self._taxes(ship.get("taxLines"), Decimal(1)),
            })
        tip_from_lines = sum((t["amount"] for t in tip_lines), ZERO)
        tip = tip_from_lines or self.money(order.get("totalTipReceivedSet"))
        fee_lines = []
        for fee in order.get("additionalFees") or []:
            fee_lines.append({"kind": "fee", "id": fee.get("id"), "name": fee.get("name"),
                              "gross": self.money(fee.get("price")), "discount": ZERO,
                              "net": self.money(fee.get("price")), "taxes": self._taxes(fee.get("taxLines"), Decimal(1))})
        fees_known = "additionalFees" in order
        fees = sum((f["net"] for f in fee_lines), ZERO) if fees_known else \
            self.money(order.get("currentTotalAdditionalFeesSet"))
        # Cash rounding belongs to the payment, not to the price of the order.
        rounding = self.money((order.get("totalCashRoundingAdjustment") or {}).get("paymentSet"))

        tax_total = ZERO
        net_total = ZERO
        duty_total = ZERO
        for item in lines + shipping + fee_lines:
            net_total += item["net"]
            tax_total += sum((t["amount"] for t in item["taxes"]), ZERO)
            for duty in item.get("duties") or []:
                duty_total += duty["amount"]
                tax_total += sum((t["amount"] for t in duty["taxes"]), ZERO)
        base = net_total
        if not taxes_included:
            base += tax_total
        if not duties_included:
            base += duty_total

        if basis == "before_refunds" and order.get("totalPriceSet"):
            target = self.money(order.get("totalPriceSet"))
        else:
            target = self.money(order.get("currentTotalPriceSet"))

        if tip_lines:
            base += tip_from_lines
        fees_in_total = fees_known
        readings = [(base, bool(tip_lines), fees_in_total)]
        if tip and not tip_lines:
            readings.append((base + tip, True, fees_in_total))
        if fees and not fees_known:
            readings.append((base + fees, False, True))
            if tip and not tip_lines:
                readings.append((base + tip + fees, True, True))
        computed, tip_in_total, fees_in_total = min(readings, key=lambda r: (abs(target - r[0]), r[1], r[2]))
        residual = target - computed
        allowed = tolerance(len(lines) + len(shipping) + len(fee_lines))
        return {
            "currency": self.currency_code(),
            "basis": basis,
            "taxes_included": taxes_included,
            "duties_included": duties_included,
            "lines": lines,
            "shipping": shipping,
            "tip_lines": tip_lines,
            "fee_lines": fee_lines,
            "tip": tip,
            "tip_in_total": tip_in_total,
            "fees": fees,
            "fees_in_total": fees_in_total,
            "cash_rounding": rounding,
            "tax_total": tax_total,
            "duty_total": duty_total,
            "net_total": net_total,
            "target_total": target,
            "computed_total": computed,
            "residual": residual,
            "tolerance": allowed,
            "within_tolerance": abs(residual) <= allowed,
            "warnings": list(self.warnings),
        }


def odoo_line_discount(gross, discount, discount_digits=2, currency_digits=2):
    """Discount percentage for an Odoo line and the money it loses to rounding.

    Odoo stores the discount as a percentage with limited precision, so the
    rounded percentage rarely reproduces the exact discount amount. The
    returned residual is added to the order's rounding line.
    """
    if not gross:
        return ZERO, discount
    percent = quantize(discount / gross * HUNDRED, discount_digits)
    applied = quantize(gross * percent / HUNDRED, currency_digits)
    return percent, discount - applied
