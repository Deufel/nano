"""py_sse — a Wirth-style minimal SSE web framework.

Real-time hypermedia over HTTP/1.1 with brotli-compressed SSE.
One OS thread per connection. Topic-scoped pub/sub via hierarchical
subjects. No async/await.

Public API:
    serve(routes, *, host, port, before_hooks, capacity, changes,
          head, on_event, ui_theme, max_connections, access_log)

    live(handler, topic=..., hard_cap=...)   # function or decorator

    Database(path, schema, changes=None)
    Changes()                            # topic-scoped pub/sub
    Capacity(soft_cap, hard_cap=None,    # viewer capacity
             min_poll_ms, max_poll_ms, ramp_users)

    # Responses
    html(body, status=200)
    redirect(location, status=303)
    error(status, message="")
    blob(data, content_type, filename=None)
    no_content()

    # Request helpers
    set_cookie(req, name, value, **opts)
    signals(req)

    # SSE primitives
    sse_data(text)
    sse_event(event_name, data)
    sse_keepalive()
"""

from .server import (
    serve,
    live,
    Changes,
    html,
    redirect,
    error,
    blob,
    no_content,
    set_cookie,
    signals,
    sse_data,
    sse_event,
    sse_keepalive,
)
from .db import Database
from .capacity import Capacity

__version__ = "0.8.0"

__all__ = [
    "serve",
    "live",
    "Changes",
    "Database",
    "Capacity",
    "html",
    "redirect",
    "error",
    "blob",
    "no_content",
    "set_cookie",
    "signals",
    "sse_data",
    "sse_event",
    "sse_keepalive",
]
