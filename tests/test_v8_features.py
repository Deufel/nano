"""Tests for v0.8 changes: multi-topic wait, topic format expansion,
on_event observability, live-mode restoration on poll refetch, and the
exponential poll-interval ramp shape."""

import os
import sys
import threading
import time
import urllib.request
import json
import math

sys.path.insert(0, "/home/claude/py_sse_v8")

from py_sse import Changes, Capacity, live, serve, html, Database, signals
from py_sse.server import _h_render
from html_tags import h


# ───────────────────────────── 1. Multi-topic wait ─────────────────────────

print("=== multi-topic wait ===")

ch = Changes()

# Single-pattern wait still works (sanity).
got = []
def wait_single():
    got.append(ch.wait("foo", timeout=1.0))
t = threading.Thread(target=wait_single)
t.start()
time.sleep(0.05)
ch.notify("foo")
t.join()
assert got == [True]
print("test A1 (single-pattern wait wakes on notify) OK")

# Multi-pattern wait wakes on any of the patterns.
got = []
def wait_multi():
    got.append(ch.wait(["account.*", "product.*", "sale.*"], timeout=1.0))

t = threading.Thread(target=wait_multi)
t.start()
time.sleep(0.05)
ch.notify("product.42")  # wakes "product.*"
t.join()
assert got == [True]
print("test A2 (multi-pattern wait wakes on any match) OK")

# Multi-pattern wait times out if nothing matches.
got = []
def wait_timeout():
    got.append(ch.wait(["alpha", "beta"], timeout=0.2))
t = threading.Thread(target=wait_timeout)
t.start()
t.join()
assert got == [False]
print("test A3 (multi-pattern wait returns False on timeout) OK")

# Multiple multi-pattern waiters all wake on a relevant notify.
got = []
lock = threading.Lock()
def wait_many(label, patterns):
    if ch.wait(patterns, timeout=1.0):
        with lock:
            got.append(label)
t1 = threading.Thread(target=wait_many, args=("a", ["x.*", "y.*"]))
t2 = threading.Thread(target=wait_many, args=("b", ["y.*", "z.*"]))
t3 = threading.Thread(target=wait_many, args=("c", ["only-this"]))
for t in (t1, t2, t3): t.start()
time.sleep(0.05)
ch.notify("y.42")  # should wake a and b but not c
for t in (t1, t2, t3): t.join(timeout=1.5)
assert set(got) == {"a", "b"}, f"expected a+b, got {got}"
print("test A4 (multi-pattern: only matching waiters wake) OK")

# Fanout cleanup: after all multi-waiters complete, no residue.
assert not ch._fanout, f"fanout should be empty, has: {ch._fanout}"
print("test A5 (fanout cleaned up after waiters return) OK")


# ───────────────────────────── 2. Topic format expansion ────────────────

print()
print("=== topic format expansion ===")

# Build a live handler with templated topic and exercise the wrapper
# logic directly via a fake request.

class FakeCap:
    def __init__(self): self.last_topic = None
    def mode(self, t): self.last_topic = t; return "live"
    def count(self, t): return 0
    def poll_interval_ms(self, t): return 1000
    def join(self, t):
        from contextlib import contextmanager
        @contextmanager
        def cm(): yield
        return cm()

cap = FakeCap()
wrapped = live(lambda req: h.span("hi"), topic="game.{id}.*")

req = {
    "_capacity": cap,
    "_changes": Changes(),
    "_head": [],
    "_ui_theme": "dark",
    "_on_event": None,
    "params": {"id": "42"},
    "headers": {},
    "path": "/games/42",
}
wrapped(req)
assert cap.last_topic == "game.42.*", f"got {cap.last_topic!r}"
print("test B1 (string topic with {id} expanded from params) OK")

# Plain string topic still works (no braces, no expansion).
cap.last_topic = None
wrapped2 = live(lambda req: h.span("hi"), topic="static-topic")
req["params"] = {}
wrapped2(req)
assert cap.last_topic == "static-topic"
print("test B2 (plain string topic passes through) OK")

# Callable topic still works.
cap.last_topic = None
wrapped3 = live(lambda req: h.span("hi"),
                topic=lambda r: f"dynamic.{r['params']['id']}")
req["params"] = {"id": "99"}
wrapped3(req)
assert cap.last_topic == "dynamic.99"
print("test B3 (callable topic still works) OK")


# ───────────────────────────── 3. Exponential poll ramp ─────────────────

print()
print("=== exponential poll ramp ===")

c = Capacity(soft_cap=10, min_poll_ms=1000, max_poll_ms=8000, ramp_users=50)

c._counts["x"] = 10  # exactly at cap
assert c.poll_interval_ms("x") == 1000
c._counts["x"] = 60  # ramp_users overage → ~95% of way to max
i_at_ramp = c.poll_interval_ms("x")
assert 7500 <= i_at_ramp <= 8000, f"at ramp_users, expected ~95% of max, got {i_at_ramp}"
print(f"test C1 (at overage=ramp_users, interval ≈ {i_at_ramp}ms ≈ 95% of max) OK")

# Curve should be monotonically increasing.
prev = 0
for over in range(0, 200, 5):
    c._counts["x"] = 10 + over
    cur = c.poll_interval_ms("x")
    assert cur >= prev, f"non-monotonic at overage={over}: {prev} -> {cur}"
    prev = cur
print("test C2 (interval is monotonic non-decreasing in overage) OK")

# As overage → ∞, approaches max_poll_ms.
c._counts["x"] = 10 + 10_000
final = c.poll_interval_ms("x")
assert 7990 <= final <= 8000, f"at huge overage, expected ~max_poll_ms, got {final}"
print(f"test C3 (asymptote: interval at huge overage = {final}ms ≈ max_poll_ms) OK")


# ───────────────────────────── 4. on_event observability ────────────────

print()
print("=== on_event observability ===")

events = []
def collect(e):
    events.append(e)

# Drive a live page render in 'live' mode.
cap = FakeCap()
wrapped = live(lambda req: h.span("hi"), topic="watch")
req = {
    "_capacity": cap,
    "_changes": Changes(),
    "_head": [], "_ui_theme": "dark",
    "_on_event": collect,
    "params": {}, "headers": {}, "path": "/",
}
wrapped(req)
assert any(e["type"] == "page_render" and e["mode"] == "live" for e in events), \
    f"expected page_render event with mode=live, got {events}"
print("test D1 (page_render event fired with mode) OK")

# Stream open/close fires for SSE requests.
events.clear()
req2 = dict(req)
req2["headers"] = {"accept": "text/event-stream"}
req2["_changes"] = Changes()  # fresh so it doesn't wake

# Real Capacity so join() actually runs and stream context works.
cap_real = Capacity(soft_cap=5)
req2["_capacity"] = cap_real

# Drain the generator briefly: yield the first frame, then close.
gen = wrapped(req2)
first_frame = next(gen)  # initial render
assert b"datastar-patch-elements" in first_frame.encode() or "datastar-patch-elements" in first_frame
gen.close()  # triggers finally → stream_close event

types = [e["type"] for e in events]
assert "stream_open" in types
assert "stream_close" in types
print(f"test D2 (stream_open + stream_close fired, events: {types}) OK")

# stream_close includes duration and frame count.
close_event = next(e for e in events if e["type"] == "stream_close")
assert "duration_s" in close_event
assert close_event["frames"] == 1, f"expected 1 frame, got {close_event['frames']}"
print(f"test D3 (stream_close has duration_s + frames={close_event['frames']}) OK")


# ───────────────────────────── 5. Live restore on poll refetch ──────────

print()
print("=== live restore on poll refetch ===")

# Use a stateful capacity that returns "poll" then "live"
class FlipCap:
    def __init__(self):
        self.calls = 0
        self.next_mode = "poll"
    def mode(self, t):
        self.calls += 1
        return self.next_mode
    def count(self, t): return 1
    def poll_interval_ms(self, t): return 1500
    def join(self, t):
        from contextlib import contextmanager
        @contextmanager
        def cm(): yield
        return cm()

flip = FlipCap()
wrapped = live(lambda req: h.span("hi"), topic="t")
req = {
    "_capacity": flip,
    "_changes": Changes(),
    "_head": [], "_ui_theme": "dark",
    "_on_event": None,
    "params": {}, "headers": {}, "path": "/",
}

# First request: poll mode → wrapper has data-on-interval.
status, headers, body = wrapped(req)
assert "data-on-interval" in body, "first request should be poll mode"
assert "data-init" not in body
print("test E1 (first refetch in poll mode: data-on-interval present) OK")

# Capacity drops; next refetch sees live mode → wrapper has data-init.
# When Datastar morphs in the new wrapper, data-init fires and opens SSE.
flip.next_mode = "live"
status, headers, body = wrapped(req)
assert "data-init" in body, "after capacity drops, refetch should be live mode"
assert "data-on-interval" not in body
print("test E2 (refetch after capacity drop: data-init restores SSE) OK")


print()
print("All v0.8 feature tests passed.")
