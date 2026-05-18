"""Per-resource viewer counting with a soft cap.

A LiveCounter tracks how many viewers are currently holding an SSE
stream open for each resource. When below the soft cap, new viewers get
a live stream. Above the cap, new viewers get a snapshot plus a Datastar
polling instruction, with the poll interval scaling with load.

State is one counter per resource in a lock-protected dict. Viewers
self-register on stream open and self-unregister on stream close via
the generator's lifecycle — when a connection dies, its generator
exits, the context manager's __exit__ runs, and the count drops.

The mode (live vs poll) is NOT a Datastar signal. It's a property of
the HTML the server sends. When the backend's morph replaces the
wrapper, the behavior changes automatically.
"""

import threading
from contextlib import contextmanager


class LiveCounter:
    """Tracks how many viewers are streaming each resource.

    Parameters
    ----------
    soft_cap : int
        Below this many concurrent streams per resource, new viewers
        get live SSE. At or above, they get polling.
    min_poll_ms : int
        Polling interval at the soft cap.
    max_poll_ms : int
        Polling interval ceiling, reached at soft_cap + ramp_users.
    ramp_users : int
        How many users above the cap before polling reaches its max
        interval. Linear ramp.
    """

    def __init__(self, soft_cap=200, min_poll_ms=1_000, max_poll_ms=8_000,
                 ramp_users=50):
        self.soft_cap = soft_cap
        self.min_poll_ms = min_poll_ms
        self.max_poll_ms = max_poll_ms
        self.ramp_users = ramp_users
        self._counts = {}
        self._lock = threading.Lock()

    def count(self, resource_id):
        with self._lock:
            return self._counts.get(resource_id, 0)

    def should_be_live(self, resource_id):
        return self.count(resource_id) < self.soft_cap

    def poll_interval_ms(self, resource_id):
        count = self.count(resource_id)
        if count <= self.soft_cap:
            return self.min_poll_ms
        overage = count - self.soft_cap
        frac = min(1.0, overage / self.ramp_users)
        return int(self.min_poll_ms + frac * (self.max_poll_ms - self.min_poll_ms))

    @contextmanager
    def join(self, resource_id):
        with self._lock:
            self._counts[resource_id] = self._counts.get(resource_id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                new = self._counts.get(resource_id, 0) - 1
                if new <= 0:
                    self._counts.pop(resource_id, None)
                else:
                    self._counts[resource_id] = new

    def snapshot(self):
        with self._lock:
            return dict(self._counts)
