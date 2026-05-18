"""Per-topic capacity management via an ordered queue of active clients.

Each topic has a queue (insertion-ordered) of client_id → last_seen entries.
Position in the queue determines treatment:

  position < soft_cap     → "live"   (open SSE; held by socket lifetime)
  soft_cap ≤ position     → "poll"   (one-shot HTML; data-on-interval)
  position ≥ hard_cap     → "static" (one-shot HTML; not added to queue)

  unknown client          → assigned a position by inserting at the end,
                            then dispatched per the above rules.

Promotion happens implicitly. When a live client's socket closes, its
entry is removed from the queue. The next polled client's request will
find itself at a lower position, possibly below soft_cap, and the next
assign() returns "live" — so the framework upgrades them to an SSE
stream automatically.

Demotion never happens. First-in, first-served: once a client is in
live mode, they keep it until they leave.

Static clients are NOT added to the queue. They got a snapshot. If they
want updates, they refresh the page; their next request enters the
queue as a fresh arrival.

State:
  _queue[topic]   = OrderedDict[client_id, last_seen | None]
                    last_seen=None means SSE-held (socket is alive)
                    last_seen=monotonic means polled (expires on staleness)
  _streams[topic] = int — open SSE socket count for the topic
                    (semantically redundant with `count(None entries)`,
                    but cheap to maintain and useful for diagnostics)
"""
import math
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager


class Capacity:
    __slots__ = ("soft_cap","hard_cap","min_poll_ms","max_poll_ms",
                 "ramp_users","_lock","_queue","_streams","_k")

    def __init__(s, soft_cap=8, hard_cap=64, min_poll_ms=1000,
                 max_poll_ms=8000, ramp_users=8):
        s.soft_cap, s.hard_cap = soft_cap, hard_cap
        s.min_poll_ms, s.max_poll_ms, s.ramp_users = min_poll_ms, max_poll_ms, ramp_users
        s._lock = threading.Lock()
        s._queue   = {}    # topic -> OrderedDict[cid, last_seen | None]
        s._streams = {}    # topic -> int
        s._k = -math.log(0.05) / max(ramp_users, 1)

    # ─── private helpers (caller holds s._lock) ──────────────────────

    def _q(s, topic):
        q = s._queue.get(topic)
        if q is None:
            q = OrderedDict()
            s._queue[topic] = q
        return q

    def _purge_locked(s, topic, now):
        q = s._queue.get(topic)
        if not q: return
        window_s = (s._poll_interval_locked(len(q)) / 1000.0) * 2.5
        cutoff = now - window_s
        stale = [cid for cid, t in q.items() if t is not None and t < cutoff]
        for cid in stale:
            del q[cid]
        if not q: s._queue.pop(topic, None)

    def _position_locked(s, topic, client_id):
        q = s._queue.get(topic)
        if not q: return None
        for i, cid in enumerate(q):
            if cid == client_id: return i
        return None

    def _poll_interval_locked(s, n):
        if n <= s.soft_cap: return s.min_poll_ms
        over = n - s.soft_cap
        frac = 1.0 - math.exp(-s._k * over)
        return int(s.min_poll_ms + (s.max_poll_ms - s.min_poll_ms) * frac)

    def _mode_at_locked(s, pos):
        if pos is None:        return "static"
        if pos < s.soft_cap:   return "live"
        if pos < s.hard_cap:   return "poll"
        return "static"

    # ─── public read API ─────────────────────────────────────────────

    def streamers(s, topic):
        "Open SSE sockets on this topic."
        with s._lock: return s._streams.get(topic, 0)

    def pollers(s, topic):
        "Distinct polled clients (queue entries with a last_seen timestamp)."
        with s._lock:
            s._purge_locked(topic, time.monotonic())
            q = s._queue.get(topic)
            if not q: return 0
            return sum(1 for v in q.values() if v is not None)

    def queue_size(s, topic):
        "Total queue size on this topic = streamers + pollers."
        with s._lock:
            s._purge_locked(topic, time.monotonic())
            return len(s._queue.get(topic, ()))

    def total_streamers(s):
        with s._lock: return sum(s._streams.values())

    def total_pollers(s):
        with s._lock:
            now = time.monotonic()
            for t in list(s._queue.keys()):
                s._purge_locked(t, now)
            return sum(1 for q in s._queue.values()
                         for v in q.values() if v is not None)

    def total(s):
        "Total clients across all topics (streamers + pollers)."
        with s._lock:
            now = time.monotonic()
            for t in list(s._queue.keys()):
                s._purge_locked(t, now)
            return sum(len(q) for q in s._queue.values())

    def position(s, topic, client_id):
        """0-based position in the queue, or None if not enqueued."""
        with s._lock:
            s._purge_locked(topic, time.monotonic())
            return s._position_locked(topic, client_id)

    def poll_interval_ms(s, topic):
        """Poll interval for new dispatch on this topic, based on queue size."""
        with s._lock:
            return s._poll_interval_locked(len(s._queue.get(topic, ())))

    # ─── dispatch decision ───────────────────────────────────────────

    def assign(s, topic, client_id):
        """Decide a client's mode for this request.

        Atomic. If the client is already in the queue, returns their mode
        based on current position. If not, attempts to add them at the
        end; returns the resulting mode. Clients past hard_cap are
        returned as "static" and NOT added to the queue.

        Does NOT update last_seen — callers should call touch_poll
        separately for poll-mode responses. SSE responses use the
        join() context manager.
        """
        with s._lock:
            s._purge_locked(topic, time.monotonic())
            q = s._q(topic)
            pos = s._position_locked(topic, client_id)
            if pos is None:
                # New arrival: place at end.
                if len(q) >= s.hard_cap:
                    # No room — static, don't add.
                    return "static"
                # Provisional entry; caller will firm it up via
                # touch_poll() (poll) or join() (live). For now, mark as
                # polled-with-fresh-timestamp so the slot is held briefly
                # even if the caller doesn't follow up.
                q[client_id] = time.monotonic()
                pos = len(q) - 1
            return s._mode_at_locked(pos)

    # ─── public write API ────────────────────────────────────────────

    @contextmanager
    def join(s, topic, client_id):
        """Hold an SSE slot for the lifetime of the with-block. The
        client is marked as socket-held (last_seen=None) and removed
        on exit. Increments the SSE socket count.

        Must be paired with a prior assign() that returned "live".
        """
        with s._lock:
            q = s._q(topic)
            q[client_id] = None     # socket-held marker; preserves position
            s._streams[topic] = s._streams.get(topic, 0) + 1
        try:
            yield
        finally:
            with s._lock:
                q = s._queue.get(topic)
                if q is not None:
                    q.pop(client_id, None)
                    if not q: s._queue.pop(topic, None)
                n = s._streams.get(topic, 0) - 1
                if n <= 0: s._streams.pop(topic, None)
                else:      s._streams[topic] = n

    def touch_poll(s, topic, client_id):
        """Refresh last_seen for a polled client.

        Must be called after assign() returned "poll" — and only then.
        Updates the timestamp so the entry stays in the queue across
        polls. Safe to call repeatedly.
        """
        with s._lock:
            q = s._q(topic)
            if client_id in q:
                q[client_id] = time.monotonic()
            # If not in queue (e.g. evicted in race), do nothing —
            # the next assign() will re-add at the end.
