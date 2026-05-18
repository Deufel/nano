# py_sse

A small Python web framework for hypermedia apps with live updates over
Server-Sent Events. Threaded, HTTP/1.1, brotli-compressed SSE,
queue-based load shedding.

Built to pair with [Datastar](https://data-star.dev/) (the JS framework
that wires server-rendered HTML to client interactivity via `data-*`
attributes) and [html-tags](https://pypi.org/project/html-tags/) (a
functional Python DSL for generating HTML).

The shape of the framework is shaped by two ideas:

- **State in the right place.** State lives where it's read and written
  most. Identity goes to the client; persistence goes to the database;
  load shedding goes to the framework. Each piece knows what it owns.
- **The Tao of Datastar.** Backend is the source of truth. Reads stream
  down; writes go up as short-lived requests; the server re-renders;
  Datastar morphs the result into the page. No diffs, no operational
  transforms, no optimistic UI.

## Install

```bash
uv add py-sse
```

Python 3.13+. Depends on `apsw`, `brotli`, `html-tags`.

## Hello, live page

```python
from py_sse import Router, Capacity, Database, live, page, no_content, serve
from html_tags import h

db = Database("counter.db", schema="""
    CREATE TABLE IF NOT EXISTS counter (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        n  INTEGER NOT NULL DEFAULT 0
    );
""")
db.execute("INSERT OR IGNORE INTO counter (id, n) VALUES (1, 0)")

CAP = Capacity(soft_cap=8, hard_cap=64)

r = Router()

@r.get("/")
def home(req):
    return page(
        head=[h.title("counter"),
              h.script({"type":"module",
                        "src":"https://cdn.jsdelivr.net/gh/starfederation/"
                              "datastar@v1.0.1/bundles/datastar.js"})],
        body=[
            h.h1("counter"),
            h.div({"id":"counter", "data-init":"@get('/stream')"}, "—"),
            h.button({"data-on:click":"@post('/inc')"}, "+1"),
        ])

@r.get("/stream")
def stream(req):
    def render():
        n, = db.one("SELECT n FROM counter WHERE id = 1")
        return h.div({"id":"counter"}, str(n))
    return live(topic="counter", render=render,
                changes=db.changes, capacity=CAP)

@r.post("/inc")
def inc(req):
    db.execute("UPDATE counter SET n = n + 1 WHERE id = 1")
    db.changes.notify("counter")
    return no_content()

if __name__ == "__main__":
    serve(r, port=8000)
```

Open two tabs. Click in one, watch it update in the other.

## What the framework does

Real-time reads, short-lived writes, with automatic graceful degradation
under load.

- **Live updates** come down a long-lived SSE stream. The server holds
  one open socket per viewer. When state changes you call
  `db.changes.notify("topic")` and every open stream re-renders.
- **Writes** are ordinary `@post()` calls. Handler mutates state, calls
  `notify`, returns 204. Datastar morphs the live region.
- **Load shedding.** Once the live-stream count crosses `soft_cap`, new
  viewers receive HTML with `data-on-interval` and poll on a back-off
  schedule. Past `hard_cap`, new viewers get static HTML with no
  auto-update. A polled tab automatically upgrades to live SSE on its
  next poll if a stream slot has opened.

The same handler serves all three modes. The framework chooses based on
current load; the handler just describes the content.

## How it's put together

Four orthogonal primitives plus one composing helper. Each is a value
you create, hold, and pass in.

```
Router    — routes (get, post, mount, use)
Changes   — bounded topic pub/sub
Capacity  — queue-based load shedding (live → poll → static)
Database  — apsw SQLite wrapper that owns a Changes instance
live()    — Changes + Capacity + SSE for one URL
```

Handlers return structured `Response` values: `Html`, `Redirect`,
`Empty`, `Sse`, `Live`. The transport interprets them.

## Live updates

`live()` is a helper that returns a `Live` response. The transport
resolves it into an SSE stream, polled HTML, or static HTML based on
current capacity.

```python
@r.get("/stream")
def stream(req):
    def render(ctx):
        # ctx is optional. Fields:
        #   ctx.mode      — "live" | "poll" | "static"
        #   ctx.poll_ms   — interval for poll mode
        #   ctx.streamers — open SSE streams on this topic
        #   ctx.pollers   — polled tabs on this topic
        #   ctx.tab_id    — this client's UUID
        #   ctx.position  — 0-based queue position
        return h.div({"id":"my-region"}, "current state")

    return live(topic="my-topic", render=render,
                changes=my_changes, capacity=my_capacity)
```

The render function returns a single top-level `html_tags` Node with an
`id`. Datastar morphs by id, replacing the element's contents on each
update.

Render is called:
- once per SSE frame in live mode (initial + every `notify`),
- once per polled request,
- once at static-mode render time.

## Tab identity (client-owned)

Each browser tab is identified by a UUID it generates itself. The
`page()` helper emits this on `<body>`:

```html
<body data-signals__ifmissing="{psseTab: crypto.randomUUID()}">
```

Datastar evaluates `crypto.randomUUID()` once on page load, sets the
`psseTab` signal, and sends it on every `@get`/`@post` to the server as
`?datastar={"psseTab":"<uuid>"}`. Because the signal lives on `<body>`
(which the framework never morphs), it persists for the tab's lifetime.

The server reads the signal to identify which tab is requesting and
which slot to give them. The framework never mints UUIDs — identity
belongs to the client.

If you bypass `page()` and write your own envelope, you're responsible
for emitting the `data-signals__ifmissing` on a stable parent element.
Otherwise queue identity won't survive between polls.

## Capacity

```python
Capacity(soft_cap=8, hard_cap=64,
         min_poll_ms=1000, max_poll_ms=8000, ramp_users=8)
```

- `soft_cap` — max concurrent SSE streams per topic. Past this, viewers
  poll instead of streaming.
- `hard_cap` — max active subscribers (streams + polled) per topic. Past
  this, viewers get static HTML.
- `min_poll_ms` / `max_poll_ms` — polling interval bounds. Interval ramps
  toward `max_poll_ms` as load grows (logarithmic).
- `ramp_users` — controls how fast the interval ramps.

Capacity tracks an ordered queue of clients per topic. Position
`0..soft_cap-1` is live, `soft_cap..hard_cap-1` is poll, beyond is
static. When a live client's socket closes, the next polled client
implicitly promotes to live on their next request.

## Changes (pub/sub)

```python
ch = Changes(max_waiters_per_topic=256)
ch.notify("topic")
ch.wait("topic", timeout=2)
```

`Changes` is a thread-safe topic broker. `wait` blocks until someone
calls `notify` for the topic, or the timeout fires. Used internally by
`live()` to wake SSE streams when state changes.

`max_waiters_per_topic` is a hard ceiling. Past it, `wait` raises
`OverCapacity` rather than queueing indefinitely. Bounded resources by
default.

## Database

```python
db = Database("app.db", schema="CREATE TABLE IF NOT EXISTS ...")
db.execute("INSERT INTO ... VALUES (?)", (arg,))
db.one("SELECT ... WHERE id = ?", (arg,))
db.all("SELECT ... FROM ...")
db.changes.notify("topic")
```

Thin wrapper around `apsw`. Owns its own `Changes` instance at
`db.changes`. Schema runs once on connection; safe to re-run.

## Routing

```python
r = Router()

@r.get("/")
def home(req): ...

@r.post("/inc")
def inc(req): ...

@r.get("/users/{id}")
def user(req, id): ...

# Nested
api = Router()
@api.get("/items")
def items(req): ...
r.mount("/api", api)

# Middleware
@r.use
def auth(req, nxt):
    if not signals(req).get("token"):
        return Redirect("/login")
    return nxt(req)
```

`req` is a dict with `method`, `path`, `headers`, `body`, `cookies`,
`query`, `_cookies_out` (for setting cookies on the response).

`signals(req)` parses the Datastar signals from the query string (GET)
or JSON body (POST/PUT/etc.).

## Responses

Handlers return structured values:

```python
html(body, status=200, headers=None)
redirect("/elsewhere", status=303)
no_content()                              # 204
page(body, head=None, title=None, ...)    # full HTML envelope
sse(frames)                                # raw SSE
live(topic=..., render=..., changes=..., capacity=...)
```

`page(body, head, ...)` emits `<!doctype html><html><head>...</head><body>...</body></html>`
with the `psseTab` signal on `<body>`. Pass `tab_signal=False` to skip
it (if you handle tab identity yourself).

## Serve

```python
serve(router, host="127.0.0.1", port=8000,
      on_event=None, access_log=True, max_threads=256,
      log_level=logging.INFO)
```

Threaded HTTP server. One OS thread per connection. Clean shutdown on
SIGINT / SIGTERM. Auto-configures stdout logging if no handlers are
installed; pass `log_level=None` to skip and use your own.

`on_event` is an optional callback fired on lifecycle events:
`stream_open`, `stream_close`, `stream_error`, `page_render`. Useful for
metrics and debugging.

## Why threads, not async

To keep the framework simple. Async Python introduces colored functions,
event-loop scheduling, careful avoidance of blocking calls, and a
distinct set of debugging tools. For a small framework that mostly does
"hold a socket, render a string, push it down the wire," threads give
you straight-line code: one connection, one thread, one place the bug
can be.

Throughput-wise, threads and async are comparable for the kind of work
this framework does (mostly I/O with short renders). If your workload
warrants async, use a different framework — async ecosystems for Python
are mature and well-supported. py_sse trades that ceiling for less
cognitive overhead at the floor.

## CQRS

The framework encourages a CQRS shape:

- **Reads** flow down long-lived SSE streams. The render function is the
  canonical "what does this region look like right now?"
- **Writes** are short-lived `@post()` calls that mutate state and call
  `notify`. No optimistic UI; the server is the source of truth.

This keeps the data flow linear: one direction at a time. No diffs sent
from client to server, no operational transforms. The server re-renders
the live region; Datastar morphs it in.

## SSE wire format

`py_sse` emits Datastar's `datastar-patch-elements` events. Each frame
is the full HTML for one region (the live region's top-level element).
Brotli's cross-frame state compresses repeated HTML at very high
ratios — for unchanged frames the ratio is often around 200:1.

## Working with Datastar: signal naming

When you use Datastar's `data-bind:*`, `data-signals:*`, `data-on:*`, or
similar attributes, the signal name lives inside an HTML attribute. **HTML
attribute names are case-insensitive per the HTML spec** — the browser's
parser silently lowercases them when reading your HTML. So this:

```html
<input data-bind:userName>     <!-- looks like it binds to "userName" -->
```

…is parsed by the browser as `data-bind:username`. Datastar reads the
DOM attribute (`username`) and creates a signal called `username`. If
your server reads `signals(req).get("userName")` it will always get
`None`, the field will always be empty, and you'll spend an hour
debugging the database.

There are two safe ways to write signal names. Pick one and stick with
it.

**Option 1 — Use all-lowercase keys in attribute names.** Simple and
unambiguous. The downside is a flat namespace with no visual word
separation.

```python
h.input({"data-bind:newname": "", ...})         # signal: newname
# server reads:  signals(req).get("newname")
```

**Option 2 — Use the value form.** Put the signal name in the *value*,
not the attribute key. Attribute values preserve case; only attribute
*names* get lowercased.

```python
h.input({"data-bind": "newName", ...})          # signal: newName
# server reads:  signals(req).get("newName")
```

Datastar supports both forms — see the
[`data-bind` reference](https://data-star.dev/reference/attributes#data-bind).
The value form is the framework's preferred convention because the
signal name is unambiguous regardless of casing.

For Datastar's own kebab→camel conversion rule (`data-bind:my-signal` →
signal `mySignal`), that conversion happens *after* the browser has
already lowercased the attribute, so it still works because dashes
survive lowercasing. But mixing dashes and intended capital letters is
where surprises live. Stick to all-lowercase keys or use the value form.

## Rough edges

Things you should know before depending on this.

- **The API isn't frozen.** Parameter names, capacity semantics, and
  response shapes are still moving as real apps are built on top. Pin
  a specific alpha; expect to read the changelog before upgrading.
- **No built-in CSRF, rate limiting, or request size limits.** A
  malicious client can open arbitrarily many SSE connections up to
  `max_threads`, send arbitrarily large bodies, or replay writes. If
  you're exposing this to untrusted traffic, put a reverse proxy in
  front of it with appropriate limits, or write the middleware
  yourself.
- **No TLS.** Pair with a reverse proxy (nginx, caddy, cloudflare).
- **Tab identity assumes Datastar.** If a request arrives without the
  `psseTab` signal, the framework treats it as an anonymous one-shot
  client and won't maintain its queue position. Non-Datastar clients
  effectively can't use poll mode coherently.
- **No graceful migration when capacity caps change at runtime.** If
  you mutate `soft_cap`/`hard_cap` on a running `Capacity` object, the
  queue isn't rebalanced — existing clients keep whatever mode they
  were assigned. Set caps once at startup.
- **Single-process only.** No built-in story for running multiple
  workers and sharing `Changes` / `Capacity` state between them. If you
  need to scale across processes, you need a different pub/sub backend
  (Redis, Postgres LISTEN/NOTIFY, etc.) and you'd write your own
  `Changes`-shaped adapter.
- **Render functions run on the request thread.** If your render does a
  slow query, that request blocks for the duration. Keep renders fast
  or push slow work elsewhere.
- **Brotli compression is on by default.** If you're proxying through
  something that double-compresses or doesn't handle `br` correctly,
  responses will look corrupt to the browser. Disable at the proxy or
  in py_sse, not both.

## Status

0.10.0 alpha. Used by the author for real projects, not yet hardened
for general use. Read the rough edges. File issues. Pin your version.

MIT.
