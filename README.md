# py_sse

A small Python web framework for real-time hypermedia apps.
HTTP/1.1, one OS thread per connection, brotli-compressed SSE,
topic-scoped pub/sub. No async. About 1200 lines.

Pairs with [html-tags](https://pypi.org/project/html-tags/) and
[Datastar](https://data-star.dev/).

## Install

```
pip install py-sse
```

## Hello, live page

```python
from py_sse import serve, live, Database, signals
from html_tags import h

db = Database("app.db", schema="CREATE TABLE IF NOT EXISTS msg (text TEXT)")

@live(topic="chat")
def home(req):
    msgs = db.all("SELECT text FROM msg")
    return [
        h.h1("chat"),
        h.ul(*[h.li(t) for (t,) in msgs]),
        h.input({"data-bind:text": "",
                 "data-on:keydown": "evt.key === 'Enter' && "
                                    "($text && (@post('/say'), $text=''))"}),
    ]

def say(req):
    text = (signals(req).get("text") or "").strip()
    if text:
        db.execute("INSERT INTO msg (text) VALUES (?)", (text,))
        db.changes.notify("chat")
    return (200, [], b"")

ROUTES = [("GET", "/", home), ("POST", "/say", say)]

if __name__ == "__main__":
    serve(ROUTES, port=8000, changes=db.changes, head=[
        h.title("chat"),
        h.script(type="module", src="https://cdn.jsdelivr.net/gh/"
                 "starfederation/datastar@v1.0.1/bundles/datastar.js"),
    ])
```

Open two browser tabs. Type in one, watch it appear in the other.

## How it works

- **One URL per live page.** `@live(topic="x")` makes the handler serve
  three transport modes from the same route, dispatched by `Accept`:
  - Live SSE while viewers are under `soft_cap`.
  - Polling between `soft_cap` and `hard_cap` (interval ramps exponentially).
  - Static above `hard_cap` (page stops auto-updating).
  - Polling clients re-upgrade to live when capacity drops.

- **CQRS.** Reads come down the long-lived SSE stream. Writes are
  short-lived `@post()` calls that mutate state and call
  `db.changes.notify("topic")`. All open streams re-render.

- **Topic patterns.** Subjects are dotted: `"game.42.score"`. A waiter
  on `"game.42.*"` wakes on any of `game.42.score`, `game.42.players`,
  etc. `@live(topic="game.{id}.*")` expands `{id}` from URL parameters.
  `changes.wait([p1, p2, ...])` waits on multiple patterns at once.

- **Fat morph.** Every frame is the whole page. idiomorph diffs in place.
  Brotli's cross-frame state compresses repeated HTML at ~200:1.

- **Framework owns the envelope.** Pass `head=[...]` to `serve()` once.
  The `<html><head><body>` is byte-identical on every render, so
  idiomorph leaves it alone and scripts stay running across morphs.

## API

```python
serve(routes, *, host, port, changes, head, capacity, on_event,
      before_hooks, ui_theme, max_connections, access_log)

live(handler, topic=..., hard_cap=...)  # function or @live(topic="...")

Database(path, schema)                  # apsw passthrough + .changes
Changes()                               # topic-scoped pub/sub
  .notify(subject)                      # publish
  .wait(pattern, timeout=...)           # pattern: str | iterable of str
Capacity(soft_cap, hard_cap, ...)       # viewer capacity

html(body), redirect(loc), error(status, msg), no_content()
signals(req)                            # parse Datastar signals
set_cookie(req, name, value, **opts)
```

Handlers return `html_tags` `Node`, `Safe`, or an iterable of those.
Writes return a `(status, headers, body)` tuple.

`on_event(event_dict)` is an optional callback fired on
`stream_open`, `stream_close`, and `page_render`. No-op by default.

## Status

0.8.0. Small, opinionated, not yet hardened for hostile traffic.
Pair with a reverse proxy for TLS. Compression and SSE buffering
are already correct on the application side.

MIT.
