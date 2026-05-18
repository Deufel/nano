"""Per-topic load counting with three-tier mode dispatch.

Mode rule (count = SSE streams + actively-polling distinct tabs):
  count < soft_cap   → "live"   (SSE; holds a slot for connection lifetime)
  soft_cap ≤ count   → "poll"   (one-shot HTML; data-on-interval refreshes)
  count ≥ hard_cap   → "static" (one-shot HTML; no auto-update)

Polled tabs are counted by stable tab identity (a cookie set by the
framework on the first poll response). Each poll updates last_seen for
that tab. A tab that hasn't polled within 2.5 × poll_interval is
considered gone. So one tab counts as one no matter how fast it polls.

Static tabs are not tracked. They received a snapshot, won't auto-refresh,
and consume no further server resources.

State:
  _streams[topic] = int                 — open SSE stream count
  _polls[topic]   = dict[tab_id, t]     — last_seen per tab (monotonic)
"""
import math
import threading
import time
from contextlib import contextmanager


class Capacity:
    __slots__ = ("soft_cap","hard_cap","min_poll_ms","max_poll_ms",
                 "ramp_users","_lock","_streams","_polls","_k")

    def __init__(s, soft_cap=8, hard_cap=64, min_poll_ms=1000,
                 max_poll_ms=8000, ramp_users=8):
        s.soft_cap, s.hard_cap = soft_cap, hard_cap
        s.min_poll_ms, s.max_poll_ms, s.ramp_users = min_poll_ms, max_poll_ms, ramp_users
        s._lock = threading.Lock()
        s._streams = {}
        s._polls   = {}
        s._k = -math.log(0.05) / max(ramp_users, 1)

    # ─── private helpers; caller must hold s._lock ───────────────────

    def _count_locked(s, topic):
        return s._streams.get(topic, 0) + len(s._polls.get(topic, {}))

    def _poll_interval_locked(s, n):
        if n <= s.soft_cap: return s.min_poll_ms
        over = n - s.soft_cap
        frac = 1.0 - math.exp(-s._k * over)
        return int(s.min_poll_ms + (s.max_poll_ms - s.min_poll_ms) * frac)

    def _purge_polls_locked(s, topic, now):
        m = s._polls.get(topic)
        if not m: return
        # Expiry = 2.5 * current poll interval. Use the count BEFORE the
        # purge for the interval calculation so a single still-active tab
        # doesn't push itself into a too-short window.
        window = (s._poll_interval_locked(s._count_locked(topic)) / 1000.0) * 2.5
        cutoff = now - window
        stale = [tid for tid, t in m.items() if t < cutoff]
        for tid in stale:
            del m[tid]
        if not m:
            s._polls.pop(topic, None)

    # ─── public read API ─────────────────────────────────────────────

    def streamers(s, topic):
        "Active SSE streams on this topic."
        with s._lock: return s._streams.get(topic, 0)

    def pollers(s, topic):
        "Distinct polled tabs recently active on this topic."
        with s._lock:
            s._purge_polls_locked(topic, time.monotonic())
            return len(s._polls.get(topic, ()))

    def count(s, topic):
        "All active subscribers (SSE + polled) on this topic."
        with s._lock:
            s._purge_polls_locked(topic, time.monotonic())
            return s._count_locked(topic)

    def total(s):
        "All active subscribers across all topics."
        with s._lock:
            now = time.monotonic()
            seen = set(s._streams) | set(s._polls)
            for t in seen: s._purge_polls_locked(t, now)
            return sum(s._streams.get(t, 0) + len(s._polls.get(t, ()))
                       for t in seen)

    def total_streamers(s):
        with s._lock: return sum(s._streams.values())

    def total_pollers(s):
        with s._lock:
            now = time.monotonic()
            for t in list(s._polls.keys()):
                s._purge_polls_locked(t, now)
            return sum(len(v) for v in s._polls.values())

    def mode(s, topic):
        n = s.count(topic)
        if n < s.soft_cap:  return "live"
        if n >= s.hard_cap: return "static"
        return "poll"

    def poll_interval_ms(s, topic):
        with s._lock:
            return s._poll_interval_locked(s._count_locked(topic))

    # ─── public write API ────────────────────────────────────────────

    @contextmanager
    def join(s, topic):
        "Hold an SSE slot for the lifetime of the with-block."
        with s._lock:
            s._streams[topic] = s._streams.get(topic, 0) + 1
        try:
            yield
        finally:
            with s._lock:
                n = s._streams.get(topic, 0) - 1
                if n <= 0: s._streams.pop(topic, None)
                else:      s._streams[topic] = n

    def touch_poll(s, topic, tab_id):
        """Record that a tab is polling. Keyed by tab_id so one tab is
        one count regardless of how fast it polls. tab_id is set by the
        framework via a session cookie."""
        with s._lock:
            now = time.monotonic()
            s._purge_polls_locked(topic, now)
            s._polls.setdefault(topic, {})[tab_id] = now
