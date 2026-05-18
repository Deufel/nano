"""py_sse — a small server framework for Datastar-style apps.

Designed around four orthogonal primitives:

  Router    — chi-style routing (routing.py)
  Changes   — bounded pub/sub (changes.py)
  Capacity  — load shedding (capacity.py)
  Database  — SQLite wrapper (db.py)

And one composing helper:

  live()    — composes Changes + Capacity + SSE for live updates (live.py)

Responses are structured values: Html, Redirect, Empty, Sse, Live.
Handlers return them; the transport interprets.

Each handler runs in a fault boundary — exceptions become 500s, never
partial responses, never one crashed handler taking down others.
"""

__version__ = "0.10.0"

from .routing  import Router
from .changes  import Changes, OverCapacity
from .capacity import Capacity
from .db       import Database
from .dispatch import live, RenderCtx
from .responses import html, redirect, ds_redirect, no_content, sse, page, Html, Redirect, Empty, Sse, Live, Response
from .transport import serve, signals

__all__ = [
    "Router", "Changes", "OverCapacity", "Capacity", "Database",
    "live", "RenderCtx",
    "html", "redirect", "ds_redirect", "no_content", "sse", "page",
    "Html", "Redirect", "Empty", "Sse", "Live", "Response",
    "serve", "signals",
    "__version__",
]
