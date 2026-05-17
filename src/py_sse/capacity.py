"""Per-resource viewer capacity tracking.

Three tiers of service based on per-resource viewer count:

  0 to soft_cap         → live SSE
  soft_cap to hard_cap  → polling (interval ramps exponentially)
  above hard_cap        → static (no automatic updates)

The poll interval ramps as exp(-k * overage), giving fast feedback
when load is just barely over the cap and degrading gracefully as
load climbs. The rate constant is set so the curve reaches roughly
95% of max_poll_ms at ramp_users overage — same intent as the prior
linear ramp, smoother in shape.
"""

import math
import threading
from contextlib import contextmanager


class Capacity:
    """Tracks how many viewers are watching each resource.

    Args:
        soft_cap:    below this many concurrent streams per resource,
                     viewers get live SSE. At or above, they degrade
                     to polling.
        hard_cap:    at or above this many viewers, polling is disabled
                     and viewers get a static response. None = polling
                     never gives up.
        min_poll_ms: poll interval at the soft cap.
        max_poll_ms: poll interval ceiling, approached as overage → ∞.
        ramp_users:  overage at which poll interval reaches ~95% of
                     max_poll_ms (sets the exponential rate constant).
    """

    def __init__(self, soft_cap=200, hard_cap=None,
                 min_poll_ms=1_000, max_poll_ms=8_000, ramp_users=50):
        self.soft_cap = soft_cap
        self.hard_cap = hard_cap
        self.min_poll_ms = min_poll_ms
        self.max_poll_ms = max_poll_ms
        self.ramp_users = ramp_users
        # k chosen so 1 - exp(-k * ramp_users) ≈ 0.95
        self._k = math.log(20) / max(1, ramp_users)
        self._counts = {}
        self._lock = threading.Lock()

    def count(self, resource_id):
        with self._lock:
            return self._counts.get(resource_id, 0)

    def total(self):
        """Sum of active connections across all resources (server-wide)."""
        with self._lock:
            return sum(self._counts.values())

    def mode(self, resource_id):
        """Current service mode: 'live', 'poll', or 'static'."""
        count = self.count(resource_id)
        if count < self.soft_cap:
            return "live"
        if self.hard_cap is not None and count >= self.hard_cap:
            return "static"
        return "poll"

    def should_be_live(self, resource_id):
        return self.mode(resource_id) == "live"

    def poll_interval_ms(self, resource_id):
        count = self.count(resource_id)
        if count <= self.soft_cap:
            return self.min_poll_ms
        overage = count - self.soft_cap
        frac = 1.0 - math.exp(-self._k * overage)
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
