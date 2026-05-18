# /// script
# requires-python = ">=3.13"
# dependencies = ["py-sse","html-tags>=0.4.4"]
#
# [tool.uv.sources]
# py-sse = { path = "../", editable = true }
# ///
"""Queue-test app for py_sse 0.10.

Purpose: isolate and observe the queue/dispatch behavior without
any business logic. No counter, no buttons — just a live region that
shows each tab's identity and queue state.

How to test:
  1. Open http://localhost:8000 in 7 tabs side-by-side.
  2. Watch each tab's "tab id" — should stay stable across renders.
  3. Watch "queue position" — should increase as you open more tabs.
  4. Tabs 1-2 should be LIVE, tabs 3-4 POLL, tabs 5+ STATIC.
  5. Close tab 1. Refresh the LIVE→LIVE check in remaining tabs.
     A POLL tab should silently promote to LIVE on its next refresh.

Observe in browser DevTools:
  - Network tab: see /stream requests with ?datastar=... carrying the signal.
  - Application > Local Storage / DevTools > Elements: inspect the
    data-signals-psse_tab attribute.

Server logs print each request with topic, tab_id, position, and mode.
"""
import os
import threading
import time

from py_sse import Router, Capacity, Changes, live, page, serve
from html_tags import h, Safe


CAPACITY = Capacity(
    soft_cap=2,
    hard_cap=4,
    min_poll_ms=2000,
    max_poll_ms=5000,
    ramp_users=2,
)

# A standalone Changes object — no database needed for this test.
CHANGES = Changes()

# Background ticker so polled/live tabs have something to render against.
def ticker():
    while True:
        time.sleep(3)
        CHANGES.notify("queue")
threading.Thread(target=ticker, daemon=True).start()

# Per-tab render counter, purely diagnostic.
_lock = threading.Lock()
_renders = {}
def bump(tab_id):
    with _lock:
        _renders[tab_id] = _renders.get(tab_id, 0) + 1
        return _renders[tab_id]


# Event log for the server-side console.
def on_event(ev):
    print(f"  event: {ev}")


CSS = """
:root { color-scheme: dark; }
body { font: 14px/1.5 ui-sans-serif, system-ui, sans-serif;
       background: #0a0a0a; color: #e5e5e5;
       min-height: 100vh; margin: 0;
       display: grid; place-items: center; padding: 2rem; }
.box { width: 100%; max-width: 32rem;
       padding: 1.5rem; border: 1px solid #2a2a2a; border-radius: 0.5rem;
       background: #0f0f0f; }
h1 { margin: 0 0 1rem; font-size: 0.8rem; font-weight: 500;
     color: #888; letter-spacing: 0.15em; text-transform: uppercase; }
.grid { display: grid; grid-template-columns: max-content 1fr;
        gap: 0.4rem 1rem;
        font: 12px/1.5 ui-monospace, monospace; }
.k { color: #666; }
.v { color: #ddd; font-variant-numeric: tabular-nums; }
.tab-id { color: #6aa6ff; background: #6aa6ff18;
          padding: 0.05em 0.4em; border-radius: 0.25em; }
.tag { display: inline-block; padding: 0.05rem 0.5rem;
       border-radius: 0.25rem;
       font: 11px/1.5 ui-monospace, monospace;
       font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
.tag.live   { background: #10b98122; color: #10b981; }
.tag.poll   { background: #f59e0b22; color: #f59e0b; }
.tag.static { background: #ef444422; color: #ef4444; }
.hint { margin-top: 1rem; color: #555; font-size: 11px; line-height: 1.6; }
"""

HEAD = [
    h.title("Queue test"),
    h.style(Safe(CSS)),
    h.script({"type": "module",
              "src": "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"}),
]

r = Router()


@r.get("/")
def home(req):
    return page(
        head=HEAD,
        body=[
            h.div({"class": "box"},
                h.h1("py_sse queue test"),
                h.div({"id": "q",
                       "data-init": "@get('/stream')"},
                      h.div({"class": "grid"},
                            h.div({"class": "k"}, "loading"),
                            h.div({"class": "v"}, "…"))),
                h.div({"class": "hint"},
                      f"soft_cap={CAPACITY.soft_cap}: tabs 0-{CAPACITY.soft_cap-1} get live SSE.",
                      h.br(),
                      f"hard_cap={CAPACITY.hard_cap}: positions 0-{CAPACITY.hard_cap-1} are in queue.",
                      h.br(),
                      "Open multiple tabs and watch each tab's id and queue position.",
                      h.br(),
                      "Open DevTools > Network to see /stream requests with their signals.")),
        ])


@r.get("/stream")
def stream(req):
    def render(ctx):
        n = bump(ctx.tab_id)
        # Log to server console for observation
        print(f"  render: tab={ctx.tab_id[:8]} pos={ctx.position} "
              f"mode={ctx.mode} renders={n} q={CAPACITY.queue_size('queue')}")

        rows = [
            ("tab id",          h.span({"class": "tab-id"}, ctx.tab_id[:8])),
            ("mode",            h.span({"class": f"tag {ctx.mode}"}, ctx.mode)),
            ("queue position",  ("—" if ctx.position is None else str(ctx.position))),
        ]
        if ctx.mode == "poll":
            rows.append(("poll every", f"{ctx.poll_ms} ms"))
        rows += [
            ("renders this tab", str(n)),
            ("streamers",        str(ctx.streamers)),
            ("pollers",          str(ctx.pollers)),
            ("queue size",       str(CAPACITY.queue_size("queue"))),
            ("rendered at",      time.strftime("%H:%M:%S.") + f"{int((time.time()%1)*1000):03d}"),
        ]
        cells = []
        for k, v in rows:
            cells.append(h.div({"class": "k"}, k))
            cells.append(h.div({"class": "v"}, v))
        return h.div({"id": "q"},
            h.div({"class": "grid"}, *cells))

    return live(topic="queue", render=render,
                changes=CHANGES, capacity=CAPACITY)


if __name__ == "__main__":
    print("=" * 60)
    print("Queue test app")
    print(f"soft_cap={CAPACITY.soft_cap}  hard_cap={CAPACITY.hard_cap}")
    print(f"min_poll={CAPACITY.min_poll_ms}ms  max_poll={CAPACITY.max_poll_ms}ms")
    print("Open http://127.0.0.1:8000 in multiple tabs.")
    print("=" * 60)
    serve(r,
          host=os.environ.get("HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "8000")),
          on_event=on_event)
