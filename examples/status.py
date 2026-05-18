# /// script
# requires-python = ">=3.13"
# dependencies = ["py-sse>=0.9.0","html-tags>=0.4.4"]
# ///
"""Capacity / mode demo for py_sse.

Open this URL in multiple browser tabs and watch the dispatch shift:
  Tabs 1-3       → live  (SSE; instant updates via notify)
  Tab 4+         → poll  (HTML re-fetched on interval)
  Burst arrivals → static (no auto-update at all; bigger flood needed
                            since polled tabs don't claim slots)

Run:
    uv run examples/status.py
"""
import os
import time
from py_sse import serve, live, Database, Capacity
from html_tags import h, Safe

db = Database("/tmp/status.db", schema="")
CAPACITY = Capacity(soft_cap=3, hard_cap=4, min_poll_ms=500, max_poll_ms=3000, ramp_users=3)

CSS = """
:root { color-scheme: dark; }
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
       background: #0a0a0a; color: #e5e5e5; margin: 0; min-height: 100vh;
       display: flex; align-items: center; justify-content: center; padding: 2rem; }
.card { width: 100%; max-width: 36rem; padding: 2rem;
        border: 1px solid #2a2a2a; border-radius: 0.5rem; background: #0f0f0f; }
.mode { font: 700 4rem/1 ui-sans-serif, system-ui, sans-serif;
        letter-spacing: -0.05em; margin: 0 0 1.5rem; text-align: center; }
.live   { color: #10b981; }
.poll   { color: #f59e0b; }
.static { color: #ef4444; }
.grid { display: grid; grid-template-columns: auto 1fr; gap: 0.5rem 1.5rem;
        margin-bottom: 1.5rem; padding: 1rem; background: #161616;
        border-radius: 0.375rem; }
.k { color: #888; text-align: right; }
.v { color: #e5e5e5; font-variant-numeric: tabular-nums; }
.v strong { color: #10b981; font-weight: 600; }
.timestamp { color: #555; font-size: 0.75rem; text-align: center;
             margin-top: 1rem; font-family: ui-monospace, monospace; }
.legend { color: #666; font-size: 0.75rem; line-height: 1.7;
          margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid #2a2a2a; }
.legend code { color: #999; }
.dot { display: inline-block; width: 0.5rem; height: 0.5rem;
       border-radius: 50%; vertical-align: middle; margin-right: 0.4rem; }
.dot.live { background: #10b981; }
.dot.poll { background: #f59e0b; }
.dot.static { background: #ef4444; }
"""


def _now():
    """Wall-clock timestamp string. Renders into each frame so you can
    see the moment of last update."""
    return time.strftime("%H:%M:%S")


@live(topic="status")
def home(req):
    mode = CAPACITY.mode("status")
    page_count = CAPACITY.count("status")
    server_count = CAPACITY.total()
    poll_ms = CAPACITY.poll_interval_ms("status")

    rows = [
        ("mode", h.span({"class": "v"},
            Safe(f'<span class="dot {mode}"></span>'),
            h.strong(mode))),
        ("this page", h.span({"class": "v"}, str(page_count),
            h.span({"style": "color:#555"}, f" / soft_cap {CAPACITY.soft_cap}"))),
        ("this server", h.span({"class": "v"}, str(server_count))),
    ]
    if mode == "poll":
        rows.append(("poll every", h.span({"class": "v"},
            f"{poll_ms} ms",
            h.span({"style": "color:#555"}, f" (max {CAPACITY.max_poll_ms} ms)"))))

    return [
        h.div({"class": "card"},
            h.div({"class": f"mode {mode}"}, mode.upper()),
            h.div({"class": "grid"},
                *[c for k, v in rows for c in (
                    h.div({"class": "k"}, k),
                    v)]),
            h.div({"class": "timestamp"}, "last render @ ", _now()),
            h.div({"class": "legend"},
                h.div("Soft cap ", h.code(str(CAPACITY.soft_cap)),
                      " — at most this many SSE streams under normal pacing."),
                h.div("Hard cap ", h.code(str(CAPACITY.hard_cap)),
                      " — burst threshold; arrivals past this get static."),
                h.div("Tabs 1–", h.code(str(CAPACITY.soft_cap)), " get ",
                      Safe('<span class="dot live"></span>'),
                      h.strong("live"), " (SSE)."),
                h.div("Tab ", h.code(str(CAPACITY.soft_cap + 1)),
                      "+ gets ", Safe('<span class="dot poll"></span>'),
                      h.strong("poll"), " (HTML on interval)."),
                h.div("Many concurrent arrivals can briefly hit ",
                      Safe('<span class="dot static"></span>'),
                      h.strong("static"), "."),
            ),
        )
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
