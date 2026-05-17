# /// script
# requires-python = ">=3.13"
# dependencies = ["py-sse>=0.8.0","html-tags>=0.4.4"]
# ///
"""Tiny capacity demo: open browser tabs to watch the mode shift.

Caps are intentionally tiny:
  soft_cap=1  → 1st tab is live, 2nd tab starts polling
  hard_cap=2  → 3rd tab gets static (no auto-update)

Run:
    uv run examples/status.py
"""
import os
from py_sse import serve, live, Database, Capacity
from html_tags import h, Safe

db = Database("/tmp/status.db", schema="")  # no app tables; topic only
CAPACITY = Capacity(soft_cap=1, hard_cap=2, min_poll_ms=500, max_poll_ms=3000, ramp_users=2)

# CSS lives in <head>, not inside the morphed live-root region.
# Datastar's idiomorph uses a <template> internally to parse incoming
# HTML; <style> blocks inside the morphed region confuse that parser.
CSS = """
body { font: 16px/1.5 system-ui, sans-serif; background: #0a0a0a;
       color: #e5e5e5; min-height: 100vh; margin: 0;
       display: flex; align-items: center; justify-content: center; }
.card { padding: 3rem; border: 1px solid #333; border-radius: 1rem;
        text-align: center; min-width: 24rem; }
.mode { font-size: 4rem; font-weight: 700; letter-spacing: -0.04em;
        margin: 0.5rem 0; }
.count { font-size: 2rem; color: #888; }
.hint { font-size: 0.875rem; color: #666; margin-top: 2rem;
        line-height: 1.6; }
.live   { color: #10b981; }
.poll   { color: #f59e0b; }
.static { color: #ef4444; }
"""


@live(topic="status")
def home(req):
    mode = CAPACITY.mode("status")
    return [
        h.div({"class": "card"},
            h.div({"class": f"mode {mode}"}, mode.upper()),
            h.div({"class": "count"}, f"{CAPACITY.count('status')} viewer(s)"),
            h.div({"class": "hint"},
                "Open more tabs to this URL. ",
                h.br(),
                "Tab 1 → live (SSE). Tab 2 → poll. Tab 3 → static."))
    ]


HEAD = [
    h.title("status"),
    h.meta(charset="utf-8"),
    h.meta(name="viewport", content="width=device-width, initial-scale=1"),
    h.style(Safe(CSS)),
    h.script(type="module",
             src="https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"),
]


if __name__ == "__main__":
    serve([("GET", "/", home)],
          host=os.environ.get("HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "8000")),
          changes=db.changes, capacity=CAPACITY, head=HEAD)
