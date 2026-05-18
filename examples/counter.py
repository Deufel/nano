# /// script
# requires-python = ">=3.13"
# dependencies = ["py-sse","html-tags>=0.4.4"]
#
# [tool.uv.sources]
# py-sse = { path = "../", editable = true }
# ///
"""Counter demo for py_sse 0.10 — with diagnostic UI.

Each tab shows:
  • its own tab id (truncated to 8 hex)
  • its current mode (live / poll / static)
  • the poll interval if polling
  • a per-tab render counter (so polls are visible even when nothing else changed)
  • streamers + pollers on this topic
  • streamers + pollers across the whole server
  • the count value and render time

Open multiple tabs. With soft_cap=3, hard_cap=6:
  Tabs 1–3:  live (SSE), instant updates.
  Tabs 4–6:  poll mode, 1–5s refresh, each contributes 1 to "pollers".
  Tab 7+:    static, no auto-update.
"""
import os
import threading
import time

from py_sse import Router, Capacity, Database, live, page, no_content, serve
from html_tags import h, Safe


# ─── State ───────────────────────────────────────────────────────────

db = Database("/tmp/counter.db", schema="""
    CREATE TABLE IF NOT EXISTS counter (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        n  INTEGER NOT NULL DEFAULT 0
    );
""")
db.execute("INSERT OR IGNORE INTO counter (id, n) VALUES (1, 0)")

CAPACITY = Capacity(
    soft_cap=3,
    hard_cap=6,
    min_poll_ms=1000,
    max_poll_ms=5000,
    ramp_users=3,
)


# ─── Per-tab render counter (diagnostic only) ─────────────────────────

_render_lock = threading.Lock()
_render_counts = {}

def bump_render_count(tab_id):
    with _render_lock:
        _render_counts[tab_id] = _render_counts.get(tab_id, 0) + 1
        return _render_counts[tab_id]


# ─── Styling (in <head>, never inside the morphed region) ────────────

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
    width: 100%; max-width: 32rem;
    padding: 2.5rem;
    border: 1px solid #2a2a2a;
    border-radius: 0.75rem;
    background: #0f0f0f;
    text-align: center;
}
h1 { margin: 0 0 1rem; font-size: 0.8rem; font-weight: 500;
     color: #888; letter-spacing: 0.15em; text-transform: uppercase; }
.value {
    font: 700 6rem/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    letter-spacing: -0.05em;
    margin: 1rem 0 1.5rem;
    font-variant-numeric: tabular-nums;
    color: #fafafa;
}
.buttons { display: flex; gap: 0.5rem; justify-content: center; margin-bottom: 1.5rem; }
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

.diag {
    padding-top: 1.5rem;
    border-top: 1px solid #2a2a2a;
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.4rem 1rem;
    font: 12px/1.5 ui-monospace, monospace;
    text-align: left;
}
.diag .k { color: #666; }
.diag .v { color: #ddd; font-variant-numeric: tabular-nums; }
.tab-id {
    color: #6aa6ff;
    background: #6aa6ff18;
    padding: 0.05em 0.4em;
    border-radius: 0.25em;
}

.tag {
    display: inline-block;
    padding: 0.05rem 0.5rem;
    border-radius: 0.25rem;
    font: 11px/1.5 ui-monospace, monospace;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
}
.tag.live   { background: #10b98122; color: #10b981; }
.tag.poll   { background: #f59e0b22; color: #f59e0b; }
.tag.static { background: #ef444422; color: #ef4444; }

.hint {
    color: #555;
    font-size: 11px;
    margin-top: 1.5rem;
    line-height: 1.6;
}
"""

HEAD = [
    h.title("Counter"),
    h.style(Safe(CSS)),
    h.script({"type": "module",
              "src": "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"}),
]


# ─── Read helper ──────────────────────────────────────────────────────

def get_count():
    n, = db.one("SELECT n FROM counter WHERE id = 1")
    return n


# ─── Routes ───────────────────────────────────────────────────────────

r = Router()


@r.get("/")
def home(req):
    return page(
        head=HEAD,
        body=[
            h.div({"class": "app"},
                h.h1("py_sse counter"),
                h.div({"id": "counter",
                       "data-init": "@get('/stream')"},
                      h.div({"class": "value"}, "—")),
                h.div({"class": "buttons"},
                    h.button({"class": "primary",
                              "data-on:click": "@post('/inc')"}, "+1"),
                    h.button({"data-on:click": "@post('/reset')"}, "reset")),
                h.div({"class": "hint"},
                      f"soft_cap={CAPACITY.soft_cap}: tabs 1–{CAPACITY.soft_cap} get live SSE.",
                      h.br(),
                      f"hard_cap={CAPACITY.hard_cap}: past this, tabs get static (no auto-update).")),
        ])


@r.get("/stream")
def stream(req):
    def render(ctx):
        n = get_count()
        renders = bump_render_count(ctx.tab_id)
        short = ctx.tab_id[:8]

        rows = [
            ("this tab",            h.span({"class": "tab-id"}, short)),
            ("mode",                h.span({"class": f"tag {ctx.mode}"}, ctx.mode)),
            ("queue position",      ("—" if ctx.position is None else str(ctx.position))),
        ]
        if ctx.mode == "poll":
            rows.append(("poll every", f"{ctx.poll_ms} ms"))
        rows += [
            ("renders for this tab", str(renders)),
            ("on this topic",        f"{ctx.streamers} streamers + {ctx.pollers} pollers"),
            ("across server",        f"{CAPACITY.total_streamers()} streamers + "
                                     f"{CAPACITY.total_pollers()} pollers"),
            ("rendered at",          time.strftime("%H:%M:%S")),
        ]

        # Flatten (key, value) pairs into grid cells.
        cells = []
        for k, v in rows:
            cells.append(h.div({"class": "k"}, k))
            cells.append(h.div({"class": "v"}, v))

        return h.div({"id": "counter"},
            h.div({"class": "value"}, str(n)),
            h.div({"class": "diag"}, *cells))

    return live(topic="counter", render=render,
                changes=db.changes, capacity=CAPACITY)


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


if __name__ == "__main__":
    serve(
        r,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
