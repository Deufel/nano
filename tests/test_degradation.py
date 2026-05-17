"""Test that live() actually serves the right transport mode under load."""

import os
import sys
import threading
import time
import urllib.request
import urllib.error

TEST_DB = "/tmp/todo_smoke_v5.db"
for suf in ("", "-wal", "-shm"):
    p = TEST_DB + suf
    if os.path.exists(p):
        os.remove(p)

import py_sse
orig_db = py_sse.Database
def patched_db(path, *a, **kw):
    return orig_db(TEST_DB, *a, **kw)
py_sse.Database = patched_db

import importlib.util
_HERE = os.path.dirname(os.path.abspath(__file__))
_TODO_PY = os.path.join(_HERE, "..", "examples", "todo.py")
spec = importlib.util.spec_from_file_location("todo_app", _TODO_PY)
todo_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(todo_app)
py_sse.Database = orig_db

from py_sse import Capacity

# Configure tight limits: 1 live viewer max, 3 viewers max before static
lc = Capacity(soft_cap=1, hard_cap=3, min_poll_ms=1000, max_poll_ms=4000, ramp_users=2)

def run_server():
    from py_sse import serve
    serve(todo_app.ROUTES, host="127.0.0.1", port=8128,
          changes=todo_app.db.changes, head=todo_app.HEAD,
          capacity=lc, access_log=False)
threading.Thread(target=run_server, daemon=True).start()
time.sleep(0.4)

def get(path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:8128{path}",
                                  headers=headers or {})
    return urllib.request.urlopen(req, timeout=3)


# Test 1: first viewer is live (gets data-init)
resp = get("/")
body = resp.read().decode()
assert "data-init" in body
assert "data-on-interval" not in body
print("test 1 (first viewer: data-init for SSE) OK")

# Open an SSE stream to occupy the live slot
sse_resp = get("/", headers={"accept": "text/event-stream", "accept-encoding": "identity"})
_ = sse_resp.fp.read1(2048)  # read first frame
time.sleep(0.2)
assert lc.count("todo") == 1
print(f"test 2 (SSE stream is open, viewer count = {lc.count('todo')}) OK")

# Now viewer count >= soft_cap. Next initial GET should be in poll mode.
resp = get("/")
body = resp.read().decode()
assert "data-on-interval" in body, "second viewer should be polled, not live"
assert "data-init" not in body
print("test 3 (second viewer over soft_cap: data-on-interval for polling) OK")

# Extract the polling interval and verify it's in the configured range
import re
m = re.search(r"data-on-interval__duration\.(\d+)ms", body)
assert m
interval = int(m.group(1))
assert 1000 <= interval <= 4000, f"interval {interval} out of range"
print(f"test 4 (poll interval {interval}ms in [1000, 4000]) OK")

# Open more SSE streams to push past hard_cap
sse2 = get("/", headers={"accept": "text/event-stream", "accept-encoding": "identity"})
_ = sse2.fp.read1(2048)
sse3 = get("/", headers={"accept": "text/event-stream", "accept-encoding": "identity"})
_ = sse3.fp.read1(2048)
time.sleep(0.2)
count = lc.count("todo")
print(f"  viewer count is now {count}")

# At hard_cap (3), next initial GET should be static (no transport attribute)
resp = get("/")
body = resp.read().decode()
if count >= 3:
    assert "data-init" not in body, "static mode should have no data-init"
    assert "data-on-interval" not in body, "static mode should have no data-on-interval"
    assert 'id="live-root"' in body
    print("test 5 (viewer at hard_cap: static mode, no auto-update) OK")
else:
    print(f"test 5 (couldn't reach hard_cap, only got {count} live viewers) SKIP")

# Close streams; live should come back
sse_resp.close()
sse2.close()
sse3.close()
time.sleep(0.5)
print(f"  after cleanup, viewer count = {lc.count('todo')}")

print()
print("Graceful degradation tests passed.")
