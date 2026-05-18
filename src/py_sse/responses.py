"""Structured handler return values.

Every handler returns one of these. The transport's dispatch reads
the type and produces the right HTTP response. Mirrors Tina's
Route_Step idea: handlers describe intent; framework executes.

The user uses these via the helper functions: html(), redirect(),
no_content(), sse(), live() (live lives in live.py since it needs
Changes + Capacity).
"""
from typing import Iterable, Callable, Optional, Any

class Response:
    "Marker base; isinstance-checked by transport."

class Html(Response):
    "A one-shot HTML response. body is the rendered HTML string."
    __slots__ = ("body","status","headers")
    def __init__(s,body,status=200,headers=None):
        s.body,s.status,s.headers = body,status,list(headers or [])

class Redirect(Response):
    __slots__ = ("location","status")
    def __init__(s,location,status=303):
        if "\r" in location or "\n" in location: raise ValueError("bad redirect target")
        s.location,s.status = location,status

class Empty(Response):
    "204 No Content."
    __slots__ = ("status","headers")
    def __init__(s,status=204,headers=None):
        s.status,s.headers = status,list(headers or [])

class Sse(Response):
    "A raw SSE stream. frames is an iterable of strings (SSE frame text)."
    __slots__ = ("frames",)
    def __init__(s,frames): s.frames = frames

class Live(Response):
    """A live update intent. Resolved by the transport into either:
      - Sse stream (capacity allows live)
      - Html with data-on-interval (poll)
      - Html with bare wrapper (static)
    The transport reads .topic / .render / .capacity / .changes and
    dispatches accordingly.
    """
    __slots__ = ("topic","render","changes","capacity","hard_cap")
    def __init__(s,topic,render,changes,capacity,hard_cap=None):
        s.topic,s.render,s.changes,s.capacity,s.hard_cap = topic,render,changes,capacity,hard_cap


def html(body,status=200,headers=None): return Html(body,status,headers)
def redirect(location,status=303): return Redirect(location,status)
def no_content(): return Empty()
def sse(frames): return Sse(frames)

def ds_redirect(url):
    """Tell the browser to navigate to `url` via Datastar.

    This is ONLY for cases where the page's identity should change — i.e.
    after sign-in (now you're an authenticated user, go to /home) or
    sign-out (you're anonymous, go to /). The browser actually navigates;
    the URL bar updates; back/forward works.

    Do NOT use this to "show the user the result of a write." Writes
    should mutate state, call `db.changes.notify(topic)`, and return
    `no_content()`. Any live() region subscribed to that topic re-renders
    automatically. The framework is built around that loop; ds_redirect
    is the escape hatch for the case where the loop doesn't apply
    (because the user's identity changed).
    """
    from .sse import datastar_redirect
    return Sse(iter([datastar_redirect(url)]))

def page(body, head=None, title=None, ui_theme="dark", status=200,
         tab_signal=True):
    """Build a full HTML page Response.

    body:       Node, Safe, or iterable of those — goes inside <body>
    head:       iterable of Nodes — extra <head> fragments
    title:      optional string — adds <title> at the start of <head>
    ui_theme:   value for <html data-ui-theme="...">
    tab_signal: if True (default), emit data-signals-psse_tab on <body>
                so each tab self-assigns a UUID via crypto.randomUUID().
                The signal is declared on <body> (which is never morphed),
                so it persists for the lifetime of the tab.

    Returns an Html Response.
    """
    from html_tags import h, Safe, render as h_render
    head_frags = list(head or [])
    if title is not None:
        head_frags = [h.title(title)] + head_frags

    if isinstance(body,(list,tuple)):
        body_children = list(body)
    else:
        body_children = [body]

    body_attrs = {"class": "page stage"}
    if tab_signal:
        # Client-owned identity. Each tab evaluates crypto.randomUUID()
        # once on page load via a Datastar expression. The signal lives
        # on <body>, never morphed, so it persists for the tab's lifetime.
        #
        # We use the JSON-object form `data-signals__ifmissing="{psseTab: ...}"`
        # rather than the colon syntax so the signal name is unambiguous
        # (avoids any attribute-name-parsing issues with underscores).
        # The signal name "psseTab" is camelCase — no underscores, which
        # are reserved in Datastar attribute names.
        body_attrs["data-signals__ifmissing"] = "{psseTab: crypto.randomUUID()}"

    doc = h.html({"id":"page","data-ui-theme":ui_theme},
        h.head(
            h.meta(charset="utf-8"),
            h.meta(name="viewport", content="width=device-width, initial-scale=1"),
            *head_frags),
        h.body(body_attrs, *body_children))
    return Html("<!doctype html>" + h_render(doc), status)
