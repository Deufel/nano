# /// script
# requires-python = ">=3.13"
# dependencies = ["py-sse","html-tags>=0.4.4"]
#
# [tool.uv.sources]
# py-sse = { path = "../", editable = true }
# ///

"""Counter demo for py_sse 0.10.

Open http://localhost:8000. Click + to increment. Open more tabs to
see live updates fan out. Open enough tabs to exceed soft_cap and watch
the new tabs shift to poll mode.

Architecture:
    GET  /         → page handler (static HTML shell)
    GET  /stream   → live region (SSE / poll / static, capacity-decided)
    POST /inc      → write: bump the counter, notify subscribers
    POST /reset    → write: zero the counter, notify subscribers

The page contains:
    <div id="counter" data-init="@get('/stream')"></div>

Datastar fires @get('/stream') on load. The server returns either an
SSE stream or one-shot HTML. Either way, the inner <div id="counter">
is morphed into the page. On notify, the stream pushes a new frame;
Datastar morphs the change in.

Run:
    uv run examples/counter.py
"""
import os
import time

from py_sse import Router, Capacity, Database, live, page, no_content, serve
from html_tags import h, Safe


# ─── State (in the right place: a database) ──────────────────────────

db = Database("/tmp/counter.db", schema="""
    CREATE TABLE IF NOT EXISTS counter (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        n  INTEGER NOT NULL DEFAULT 0
    );
""")
db.execute("INSERT OR IGNORE INTO counter (id, n) VALUES (1, 0)")


# ─── Capacity (in the right place: a Capacity object) ────────────────
# soft_cap=3 → first 3 tabs get live SSE
# hard_cap=6 → past 6 concurrent SSE, new tabs get static (no auto-update)

CAPACITY = Capacity(
    soft_cap=3,
    hard_cap=6,
    min_poll_ms=500,
    max_poll_ms=3000,
    ramp_users=3,
)


# ─── Styling ──────────────────────────────────────────────────────────
# CSS lives in <head>, never inside the morphed region. (Datastar's
# idiomorph parses morphed HTML via <template>; <style> tags inside
# morphed content can corrupt the parser.)

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
    font: 16px/1.5 ui-sans-serif, system-ui, sans-serif;
    background: #0a0a0a; color: #e5e5e5;
    min-height: 100vh; margin: 0;
    display: grid; place-items: center;
    padding: 2rem;
}
.app {
    width: 100%; max-width: 28rem;
    padding: 2.5rem;
    border: 1px solid #2a2a2a;
    border-radius: 0.75rem;
    background: #0f0f0f;
    text-align: center;
}
h1 { margin: 0 0 1rem; font-size: 1rem; font-weight: 500;
     color: #888; letter-spacing: 0.1em; text-transform: uppercase; }
.value {
    font: 700 7rem/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    letter-spacing: -0.05em;
    margin: 1rem 0 2rem;
    font-variant-numeric: tabular-nums;
}
.buttons { display: flex; gap: 0.5rem; justify-content: center; }
button {
    flex: 1; max-width: 8rem;
    padding: 0.75rem 1.25rem;
    border: 1px solid #333; border-radius: 0.5rem;
    background: #181818; color: #e5e5e5;
    font: inherit; font-weight: 600;
    cursor: pointer;
    transition: background 80ms;
}
button:hover { background: #222; }
button.primary { background: #10b981; border-color: #10b981; color: #0a0a0a; }
button.primary:hover { background: #0ea670; }
.status {
    margin-top: 2rem; padding-top: 1.5rem;
    border-top: 1px solid #2a2a2a;
    display: grid; grid-template-columns: auto 1fr; gap: 0.5rem 1.5rem;
    font: 13px/1.5 ui-monospace, monospace; text-align: left;
}
.k { color: #666; text-align: right; }
.v { color: #e5e5e5; }
.tag { display: inline-block;
       padding: 0.1rem 0.5rem;
       border-radius: 0.25rem;
       font: 11px/1.5 ui-monospace, monospace;
       font-weight: 600;
       letter-spacing: 0.05em;
       text-transform: uppercase; }
.tag.live   { background: #10b98122; color: #10b981; }
.tag.poll   { background: #f59e0b22; color: #f59e0b; }
.tag.static { background: #ef444422; color: #ef4444; }
.hint { color: #555; font-size: 12px; margin-top: 1.5rem; line-height: 1.6; }
"""

HEAD = [
    h.title("Counter"),
    h.style(Safe(CSS)),
    h.script(type="module",
             src="https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"),
]


# ─── Read helper ──────────────────────────────────────────────────────

def get_count():
    n, = db.one("SELECT n FROM counter WHERE id = 1")
    return n


# ─── Page handler: plain HTML shell ───────────────────────────────────

r = Router()


@r.get("/")
def home(req):
    return page(
        head=HEAD,
        body=[
            h.div({"class": "app"},
                h.h1("Counter"),
                # The live region. Empty on first paint;
                # /stream fills it within ~30ms via data-init.
                h.div({"id": "counter",
                       "data-init": "@get('/stream')"},
                      h.div({"class": "value"}, "—")),
                h.div({"class": "buttons"},
                    h.button({"class": "primary",
                              "data-on:click": "@post('/inc')"},
                             "+1"),
                    h.button({"data-on:click": "@post('/reset')"},
                             "reset")),
                h.div({"class": "hint"},
                      "Open more tabs to watch live updates fan out. ",
                      h.br(),
                      f"Tabs 1–{CAPACITY.soft_cap} get SSE; tab {CAPACITY.soft_cap + 1}+ polls.")),
        ])


# ─── Stream handler: the live region, rendered on every notify ───────

@r.get("/stream")
def stream(req):
    """The render closure is called once per render.
    - For live mode: called on initial open and on every db.changes.notify('counter').
    - For poll mode: called once per polling request.
    - For static mode: called once at the moment the tab arrives.

    The ctx argument tells us which mode THIS render is for — so the
    badge shows the tab's actual transport, not just whatever
    capacity.mode() returned at the time."""
    def render(ctx):
        n = get_count()
        streamers = CAPACITY.streamers("counter")
        pollers   = CAPACITY.pollers("counter")
        total_streamers = CAPACITY.total_streamers()
        total_pollers   = CAPACITY.total_pollers()
        # Single top-level Node with an id — Datastar morphs by id.
        return h.div({"id": "counter"},
            h.div({"class": "value"}, str(n)),
            h.div({"class": "status"},
                h.div({"class": "k"}, "this tab"),
                h.div({"class": "v"},
                      h.span({"class": f"tag {ctx.mode}"}, ctx.mode)),
                h.div({"class": "k"}, "on this page"),
                h.div({"class": "v"},
                      f"{streamers} streamers + {pollers} pollers"),
                h.div({"class": "k"}, "on server"),
                h.div({"class": "v"},
                      f"{total_streamers} streamers + {total_pollers} pollers"),
                h.div({"class": "k"}, "rendered at"),
                h.div({"class": "v"}, time.strftime("%H:%M:%S"))))

    return live(
        topic="counter",
        render=render,
        changes=db.changes,
        capacity=CAPACITY,
    )


# ─── Write handlers: short-lived POSTs ────────────────────────────────

@r.post("/inc")
def inc(req):
    db.execute("UPDATE counter SET n = n + 1 WHERE id = 1")
    db.changes.notify("counter")
    return no_content()


@r.post("/reset")
def reset(req):
    db.execute("UPDATE counter SET n = 0 WHERE id = 1")
    db.changes.notify("counter")
    return no_content()


# ─── Run ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    serve(
        r,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
