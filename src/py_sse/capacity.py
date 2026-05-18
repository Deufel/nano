"""Per-topic + server-wide SSE connection count, with mode dispatch.

Mode rule:
  count < soft_cap   → "live"   (open SSE stream, hold a slot)
  count ≥ soft_cap   → "poll"   (one-shot HTML with data-on-interval)
  count ≥ hard_cap   → "static" (one-shot HTML, no auto-update)

Poll interval ramps from min_poll_ms to max_poll_ms as overage grows.
The ramp uses 1 - exp(-k*overage), reaching ~95% of max at ramp_users.

State: just two integers per topic (count, and via the lock, the
running total). Nothing else lives here.
"""
import math
import threading
from contextlib import contextmanager

class Capacity:
    __slots__ = ("soft_cap","hard_cap","min_poll_ms","max_poll_ms",
                 "ramp_users","_lock","_counts","_k")

    def __init__(s,soft_cap=8,hard_cap=64,min_poll_ms=1000,
                 max_poll_ms=8000,ramp_users=8):
        s.soft_cap,s.hard_cap = soft_cap,hard_cap
        s.min_poll_ms,s.max_poll_ms,s.ramp_users = min_poll_ms,max_poll_ms,ramp_users
        s._lock = threading.Lock()
        s._counts = {}
        # k chosen so 1 - exp(-k*ramp_users) ≈ 0.95
        s._k = -math.log(0.05) / max(ramp_users,1)

    def count(s,topic):
        with s._lock: return s._counts.get(topic,0)

    def total(s):
        "Sum across all topics — server-wide active SSE count."
        with s._lock: return sum(s._counts.values())

    def mode(s,topic):
        n = s.count(topic)
        if n < s.soft_cap: return "live"
        if n >= s.hard_cap: return "static"
        return "poll"

    def poll_interval_ms(s,topic):
        n = s.count(topic)
        if n <= s.soft_cap: return s.min_poll_ms
        over = n - s.soft_cap
        frac = 1.0 - math.exp(-s._k * over)
        return int(s.min_poll_ms + (s.max_poll_ms - s.min_poll_ms) * frac)

    @contextmanager
    def join(s,topic):
        "Hold a slot for the lifetime of the with-block."
        with s._lock: s._counts[topic] = s._counts.get(topic,0) + 1
        try:
            yield
        finally:
            with s._lock:
                n = s._counts.get(topic,0) - 1
                if n <= 0: s._counts.pop(topic,None)
                else:      s._counts[topic] = n
