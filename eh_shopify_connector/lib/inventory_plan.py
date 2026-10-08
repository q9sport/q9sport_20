# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Plan and send inventory updates safely.

* One change per inventory item and location (the latest wins).
* No request when the target already equals the last quantity we saw.
* Every quantity carries ``changeFromQuantity`` (compare and set), so a sale
  in Shopify between our read and our write is detected instead of being
  overwritten. A change with no known previous quantity is sent to be read
  first.
* Batches that Shopify partly rejects are split: items named by an error are
  isolated, and errors that do not name an item are narrowed down by halving
  the batch, so one bad item never blocks the others.
"""
import re

_INDEX_RE = re.compile(r"^\d+$")


def prepare(changes, batch_size=100):
    """Return (batches, needs_read, skipped).

    ``changes`` are dicts with inventory_item_gid, location_gid, quantity and
    last_known (int or None).
    """
    latest = {}
    for change in changes or []:
        key = (change["inventory_item_gid"], change["location_gid"])
        latest[key] = change
    ready = []
    needs_read = []
    skipped = []
    for key in sorted(latest):
        change = latest[key]
        target = max(0, int(change.get("quantity") or 0))
        known = change.get("last_known")
        if known is None:
            needs_read.append(change)
            continue
        if int(known) == target:
            skipped.append(change)
            continue
        ready.append({"inventoryItemId": key[0], "locationId": key[1], "quantity": target,
                      "changeFromQuantity": int(known)})
    batches = [ready[i:i + batch_size] for i in range(0, len(ready), batch_size)]
    return batches, needs_read, skipped


def error_index(error):
    """Index into ``input.quantities`` named by a user error, if any."""
    field = error.get("field") or []
    for position, part in enumerate(field):
        if part == "quantities" and position + 1 < len(field) and _INDEX_RE.match(str(field[position + 1])):
            return int(field[position + 1])
    return None


def send_all(batches, send, max_calls=1000):
    """Send batches with isolation. ``send(items)`` returns a list of userErrors.

    Returns {"applied": [items], "failed": [(item, error)], "calls": n}.
    """
    applied = []
    failed = []
    calls = [0]

    def run(items):
        if not items:
            return
        if calls[0] >= max_calls:
            failed.extend((item, {"message": "call budget exhausted", "code": "BUDGET"}) for item in items)
            return
        calls[0] += 1
        errors = send(items) or []
        if not errors:
            applied.extend(items)
            return
        indexed = {}
        unindexed = []
        for error in errors:
            index = error_index(error)
            if index is not None and 0 <= index < len(items):
                indexed.setdefault(index, error)
            else:
                unindexed.append(error)
        if indexed:
            for index, error in indexed.items():
                failed.append((items[index], error))
            rest = [item for position, item in enumerate(items) if position not in indexed]
            run(rest)
            return
        if len(items) == 1:
            failed.append((items[0], unindexed[0]))
            return
        middle = len(items) // 2
        run(items[:middle])
        run(items[middle:])

    for batch in batches:
        run(list(batch))
    return {"applied": applied, "failed": failed, "calls": calls[0]}
