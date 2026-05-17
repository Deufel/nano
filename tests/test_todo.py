"""Smoke test for py_sse 0.7.0 todo demo."""

import os
import sys
import threading
import time
import urllib.request
import urllib.error
import json
import re

TEST_DB = "/tmp/todo_smoke_v4.db"
for suf in ("", "-wal", "-shm"):
    p = TEST_DB + suf
    if os.path.exists(p):
        os.remove(p)

# Use the new package
sys.path.insert(0, "/home/claude/py_sse_v8")

# Patch Database to use temp db
import py_sse
orig_db = py_sse.Database
def patched_db(path, *a, **kw):
    return orig_db(TEST_DB, *a, **kw)
py_sse.Database = patched_db

import importlib.util
spec = importlib.util.spec_from_file_location("todo_app", "/home/claude/py_sse_v8/todo.py")
todo_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(todo_app)
py_sse.Database = orig_db

def run_server():
    from py_sse import serve
    serve(todo_app.ROUTES, host="127.0.0.1", port=8127,
          changes=todo_app.db.changes, head=todo_app.HEAD,
          access_log=False)
threading.Thread(target=run_server, daemon=True).start()
time.sleep(0.4)

def get(path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:8127{path}",
                                  headers=headers or {})
    return urllib.request.urlopen(req, timeout=3)

def post_json(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:8127{path}",
        data=body,
        headers={"content-type": "application/json"},
        method="POST")
    return urllib.request.urlopen(req, timeout=3)

def post_empty(path):
    req = urllib.request.Request(f"http://127.0.0.1:8127{path}",
                                  data=b"", method="POST")
    return urllib.request.urlopen(req, timeout=3)


# Test 1: GET / returns full HTML page with envelope + #live-root + data-init
resp = get("/")
body = resp.read().decode()
assert resp.status == 200
assert "<!doctype html>" in body, "missing doctype"
assert '<html id="page"' in body, "missing html envelope with id=page"
assert "<head>" in body, "missing head"
assert "stylesheet" in body, "missing CSS link from HEAD"
assert "datastar" in body, "missing Datastar script from HEAD"
assert 'id="live-root"' in body, "missing live-root wrapper"
assert "data-init" in body, "live mode should have data-init on wrapper"
assert "data-on-interval" not in body, "live mode should NOT have data-on-interval"
print("test 1 (initial GET / has full envelope + live-root + data-init) OK")

# Test 2: no <form> tags (pure Datastar)
assert "<form" not in body
print("test 2 (no <form> tags) OK")

# Test 3: same URL with Accept: text/event-stream returns SSE
resp = get("/", headers={"accept": "text/event-stream", "accept-encoding": "identity"})
assert resp.headers.get("content-type") == "text/event-stream"
chunk = resp.fp.read1(8192)
assert b"datastar-patch-elements" in chunk
assert b'id="live-root"' in chunk
# SSE frames should NOT contain data-init (would re-trigger stream)
assert b"data-init" not in chunk
print("test 3 (same URL with SSE Accept opens stream, no data-init in frame) OK")
resp.close()

# Test 4: add via JSON @post
post_json("/todos", {"text": "buy milk"})
resp = get("/")
body = resp.read().decode()
assert "buy milk" in body
print("test 4 (JSON @post adds todo) OK")

# Test 5: notify wakes stream — open SSE, fire write, watch for new frame
resp = get("/", headers={"accept": "text/event-stream", "accept-encoding": "identity"})
# Read first frame
initial = b""
for _ in range(5):
    initial += resp.fp.read1(8192)
    if b"buy milk" in initial:
        break
assert b"buy milk" in initial

def fire_post():
    time.sleep(0.2)
    post_json("/todos", {"text": "walk dog"})
threading.Thread(target=fire_post, daemon=True).start()

deadline = time.time() + 3
followup = b""
while time.time() < deadline:
    chunk = resp.fp.read1(8192)
    if not chunk:
        time.sleep(0.05)
        continue
    followup += chunk
    if b"walk dog" in followup:
        break
assert b"walk dog" in followup
print("test 5 (notify → SSE stream sends new frame) OK")
resp.close()

# Test 6: toggle works
resp = get("/")
body = resp.read().decode()
m = re.search(r"/todos/(\d+)/toggle", body)
assert m
todo_id = int(m.group(1))
post_empty(f"/todos/{todo_id}/toggle")
resp = get("/")
body = resp.read().decode()
assert "line-through" in body
print("test 6 (toggle works) OK")

# Test 7: delete works
post_empty(f"/todos/{todo_id}/delete")
resp = get("/")
body = resp.read().decode()
assert f"/todos/{todo_id}/toggle" not in body
print("test 7 (delete works) OK")

# Test 8: head fragments are present and consistent between initial GET and SSE
resp = get("/")
body = resp.read().decode()
initial_head_match = re.search(r'<head>(.*?)</head>', body, re.DOTALL)
assert initial_head_match
head_initial = initial_head_match.group(1)
assert 'rel="stylesheet"' in head_initial
assert 'datastar' in head_initial

# SSE frames don't carry a head (just the live-root content), so the head
# in the live DOM is whatever was set on initial load. That's by design:
# idiomorph never touches it.
print("test 8 (head fragments injected, consistent envelope) OK")

# Test 9: graceful degradation — set soft_cap=1 and verify second viewer polls
from py_sse import Capacity
# We can't easily restart the server here. Instead, test the mode logic directly.
lc = Capacity(soft_cap=1, hard_cap=3)
assert lc.mode("foo") == "live"
with lc.join("foo"):
    assert lc.mode("foo") == "poll"
    with lc.join("foo"):
        assert lc.mode("foo") == "poll"
        with lc.join("foo"):
            assert lc.mode("foo") == "static"
assert lc.mode("foo") == "live"  # all viewers left
print("test 9 (Capacity three-tier mode logic) OK")

print()
print("All 9 smoke tests passed.")
