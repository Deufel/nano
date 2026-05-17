"""Minimal py_sse fixture app for end-to-end browser tests.

Two live pages:
  /         counter,        @live(topic="counter")
  /c/{id}   per-id counter, @live(topic="counter.{id}")

Each page renders the current value, an increment button (Datastar @post),
and a footer with viewer count + mode tag. Semantic IDs throughout so
Playwright can target them without fragile CSS selectors.

Capacity is intentionally low (soft=2, hard=4) so tests can exercise
the live → poll → static degradation with a handful of browser contexts.
"""
import os
from py_sse import serve, live, Database, Capacity, signals
from html_tags import h

SCHEMA = """
CREATE TABLE IF NOT EXISTS counter(id TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO counter (id, value) VALUES ('global', 0);
"""
db = Database(os.environ.get("E2E_DB", "e2e.db"), schema=SCHEMA)

CAPACITY = Capacity(soft_cap=2, hard_cap=4, min_poll_ms=500, max_poll_ms=2000, ramp_users=2)


def _counter(id_):
    """Read or create counter row, return current int value."""
    row = db.one("SELECT value FROM counter WHERE id = ?", (id_,))
    if row is None:
        db.execute("INSERT INTO counter (id, value) VALUES (?, 0)", (id_,))
        return 0
    return row[0]


def _footer(topic):
    return h.footer({"id": "footer"},
        h.span({"id": "mode"},     CAPACITY.mode(topic)),
        h.span({"id": "viewers"},  f"watching {CAPACITY.count(topic)}"),
        h.span({"id": "topic"},    topic))


@live(topic="counter")
def home(req):
    n = _counter("global")
    return [
        h.h1("counter"),
        h.div({"id": "value"}, str(n)),
        h.button({"id": "inc", "type": "button",
                  "data-on:click": "@post('/inc')"}, "+"),
        h.a({"href": "/c/red",  "id": "link-red"},  "red"),
        h.a({"href": "/c/blue", "id": "link-blue"}, "blue"),
        _footer("counter"),
    ]


@live(topic="counter.{id}")
def per_id(req):
    id_ = req["params"]["id"]
    n = _counter(id_)
    return [
        h.h1({"id": "name"}, f"counter: {id_}"),
        h.div({"id": "value"}, str(n)),
        h.button({"id": "inc", "type": "button",
                  "data-on:click": f"@post('/c/{id_}/inc')"}, "+"),
        h.a({"href": "/", "id": "link-home"}, "home"),
        _footer(f"counter.{id_}"),
    ]


def inc(req):
    db.execute("UPDATE counter SET value = value + 1 WHERE id = ?", ("global",))
    db.changes.notify("counter")
    return (200, [], b"")


def inc_id(req):
    id_ = req["params"]["id"]
    _counter(id_)
    db.execute("UPDATE counter SET value = value + 1 WHERE id = ?", (id_,))
    db.changes.notify(f"counter.{id_}")
    return (200, [], b"")


ROUTES = [
    ("GET",  "/",            home),
    ("POST", "/inc",         inc),
    ("GET",  "/c/{id}",      per_id),
    ("POST", "/c/{id}/inc",  inc_id),
]

HEAD = [
    h.title("e2e"),
    h.script(type="module",
             src="https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"),
]


if __name__ == "__main__":
    serve(ROUTES,
          host=os.environ.get("HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "8210")),
          changes=db.changes, capacity=CAPACITY, head=HEAD)
