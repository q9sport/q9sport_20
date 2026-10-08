# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Decide which Shopify fulfillment order lines a shipment fulfils.

Shopify ships through fulfillment orders, one per assigned location. A
shipment from Odoo is a quantity per order line item; this module turns it
into ``lineItemsByFulfillmentOrder`` inputs without ever fulfilling more than a
fulfillment order still owes, and without silently shipping from the wrong
location. Anything it cannot place is returned with the reason, so the job
engine can move a fulfillment order, wait for a hold to be released, or raise
an exception.
"""

FULFILLABLE = ("OPEN", "IN_PROGRESS")
WAITING = {"SCHEDULED": "scheduled", "ON_HOLD": "on_hold"}
FINISHED = ("CLOSED", "CANCELLED", "INCOMPLETE")


def _nodes(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return value.get("nodes") or [edge["node"] for edge in value.get("edges") or []]


def _location(fulfillment_order):
    return ((fulfillment_order.get("assignedLocation") or {}).get("location") or {}).get("id")


def plan(fulfillment_orders, shipped, location_gid=None):
    """Allocate ``shipped`` ({order line item gid: quantity}) to fulfillment orders.

    Returns a dict with:
      requests: [{"fulfillmentOrderId", "fulfillmentOrderLineItems": [{"id", "quantity"}]}]
      moves: [{"fulfillmentOrderId", "fromLocationId", "toLocationId", "lineItems": {gid: qty}}]
      blocked: [{"fulfillmentOrderId", "reason", "detail"}]
      unallocated: {order line item gid: quantity}
    """
    needed = {gid: int(qty) for gid, qty in (shipped or {}).items() if int(qty or 0) > 0}
    # ``pending`` is only reduced by fulfilment requests; ``needed`` is also
    # reduced by planned moves so a second fulfillment order owing the same
    # units elsewhere is not moved as well.
    pending = dict(needed)
    orders = sorted(fulfillment_orders or [], key=lambda fo: (0 if location_gid and _location(fo) == location_gid
                                                               else 1, fo.get("id") or ""))
    requests = []
    moves = []
    blocked = []
    for fulfillment_order in orders:
        if not needed:
            break
        status = (fulfillment_order.get("status") or "").upper()
        if status in FINISHED:
            continue
        lines = _nodes(fulfillment_order.get("lineItems"))
        owed = {}
        for line in lines:
            gid = (line.get("lineItem") or {}).get("id")
            remaining = int(line.get("remainingQuantity") or 0)
            if gid in needed and remaining > 0:
                owed[line["id"]] = (gid, remaining)
        if not owed:
            continue
        if status in WAITING:
            holds = fulfillment_order.get("fulfillmentHolds") or []
            detail = ", ".join(h.get("reason") or "" for h in holds) or None
            blocked.append({"fulfillmentOrderId": fulfillment_order["id"], "reason": WAITING[status],
                            "detail": detail})
            continue
        if status not in FULFILLABLE:
            blocked.append({"fulfillmentOrderId": fulfillment_order["id"], "reason": "status",
                            "detail": status})
            continue
        at_other_location = bool(location_gid) and _location(fulfillment_order) not in (None, location_gid)
        taken = []
        for fo_line_id, (gid, remaining) in sorted(owed.items()):
            quantity = min(remaining, needed.get(gid, 0))
            if quantity <= 0:
                continue
            taken.append((fo_line_id, gid, quantity))
        if not taken:
            continue
        if at_other_location:
            move_lines = {}
            for _fo_line_id, gid, quantity in taken:
                move_lines[gid] = move_lines.get(gid, 0) + quantity
            moves.append({"fulfillmentOrderId": fulfillment_order["id"],
                          "fromLocationId": _location(fulfillment_order), "toLocationId": location_gid,
                          "lineItems": move_lines})
            for gid, quantity in move_lines.items():
                needed[gid] -= quantity
                if needed[gid] <= 0:
                    del needed[gid]
            continue
        request_lines = []
        for fo_line_id, gid, quantity in taken:
            request_lines.append({"id": fo_line_id, "quantity": quantity})
            needed[gid] -= quantity
            pending[gid] = pending.get(gid, 0) - quantity
            if needed[gid] <= 0:
                del needed[gid]
        requests.append({"fulfillmentOrderId": fulfillment_order["id"], "fulfillmentOrderLineItems": request_lines})
    return {"requests": requests, "moves": moves, "blocked": blocked,
            "unallocated": {gid: quantity for gid, quantity in pending.items() if quantity > 0}}
