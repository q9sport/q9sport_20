# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Cost aware pacing for the Shopify GraphQL Admin API.

Shopify meters GraphQL by query cost with a leaky bucket per app and shop:
``maximumAvailable`` points, refilled at ``restoreRate`` points per second.
Each response reports the bucket in ``extensions.cost.throttleStatus``. We
keep the last observation per shop, project the refill forward in time, and
wait before sending a query whose requested cost would overdraw the bucket.
That avoids THROTTLED errors instead of reacting to them.
"""
import random
import threading
import time

MAX_SINGLE_WAIT = 30.0


class CostBucket(object):
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.maximum = None
        self.available = None
        self.restore_rate = None
        self.observed_at = None
        self.last_requested = None
        self.last_actual = None

    def update(self, cost):
        """Record ``extensions.cost`` from a response."""
        if not cost:
            return
        status = cost.get("throttleStatus") or {}
        with self._lock:
            if status.get("maximumAvailable") is not None:
                self.maximum = float(status["maximumAvailable"])
            if status.get("currentlyAvailable") is not None:
                self.available = float(status["currentlyAvailable"])
            if status.get("restoreRate") is not None:
                self.restore_rate = float(status["restoreRate"])
            self.observed_at = self._clock()
            self.last_requested = cost.get("requestedQueryCost")
            self.last_actual = cost.get("actualQueryCost")

    def projected(self):
        """Points expected to be available now."""
        with self._lock:
            if self.available is None or self.restore_rate is None:
                return None
            elapsed = max(0.0, self._clock() - (self.observed_at or self._clock()))
            value = self.available + elapsed * self.restore_rate
            if self.maximum is not None:
                value = min(self.maximum, value)
            return value

    def wait_for(self, requested_cost):
        """Seconds to wait so that ``requested_cost`` points are available."""
        if not requested_cost:
            return 0.0
        projected = self.projected()
        if projected is None or self.restore_rate in (None, 0):
            return 0.0
        if self.maximum is not None:
            requested_cost = min(float(requested_cost), self.maximum)
        deficit = float(requested_cost) - projected
        if deficit <= 0:
            return 0.0
        return min(MAX_SINGLE_WAIT, deficit / self.restore_rate)

    def snapshot(self):
        return {
            "maximum": self.maximum,
            "available": self.projected(),
            "restore_rate": self.restore_rate,
            "last_requested": self.last_requested,
            "last_actual": self.last_actual,
        }


_BUCKETS = {}
_BUCKETS_LOCK = threading.Lock()


def bucket_for(shop_key, clock=time.monotonic):
    """Process wide bucket per shop so every client in a worker shares pacing."""
    with _BUCKETS_LOCK:
        bucket = _BUCKETS.get(shop_key)
        if bucket is None:
            bucket = _BUCKETS[shop_key] = CostBucket(clock=clock)
        return bucket


def jitter(seconds, spread=0.25, rng=random.random):
    """Add up to ``spread`` proportional jitter so workers do not stampede."""
    return seconds + seconds * spread * rng()


def backoff(attempt, base=1.0, cap=30.0, rng=random.random):
    """Exponential backoff with full jitter for network and 5xx retries."""
    return min(cap, base * (2 ** attempt)) * (0.5 + 0.5 * rng())
