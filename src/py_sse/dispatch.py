"""live() helper. Composes Changes + Capacity + SSE.

A handler returns live(...). The transport resolves it into one of:
  - Sse stream      (capacity.assign says "live")
  - Html with data-on-interval on the top-level element ("poll")
  - Html with no transport attrs ("static")

The render callable returns a single top-level html_tags Node. The
Node MUST have an id — Datastar morphs by element id per the response.

render() can optionally take a RenderCtx argument:

    def render(ctx):
        # ctx.mode      → "live" | "poll" | "static"
        # ctx.poll_ms   → current poll interval if mode == "poll"
        # ctx.streamers → SSE viewers on this topic right now
        # ctx.pollers   → polled viewers on this topic right now
        # ctx.tab_id    → this client's UUID
        # ctx.position  → 0-based queue position (None if static)
        ...

If render takes no arguments, it's called with none. The framework
inspects the signature once at live() construction.

Tab identity flows via a Datastar `psseTab` signal. The signal is
established by the page handler — page() emits
`data-signals__ifmissing="{psseTab: crypto.randomUUID()}"` on <body>.
Each fresh tab evaluates crypto.randomUUID() once on page load, gets
its own UUID, and the signal lives on <body> for the tab's lifetime.

Because <body> is never morphed by dispatch responses, the signal is
never lost. On every /stream request Datastar sends the signal along
with the request, so the server can identify the requesting tab.

Identity is client-owned. The server reads the signal but does not mint
UUIDs (apart from an ephemeral fallback id for requests that arrive
without the signal, e.g. before Datastar initializes or from non-Datastar
clients).

State: none here. live() is a constructor. State lives in the Changes
and Capacity instances the caller passes.
"""
import inspect
import time
import uuid

from .responses import Live, Html, Sse
from . import sse as sse_mod


class RenderCtx:
    """Per-render context.

    Fields:
      mode       → "live" | "poll" | "static"
      poll_ms    → poll interval in ms when mode == "poll", else 0
      streamers  → SSE viewers on this topic
      pollers    → polled viewers on this topic
      tab_id     → this client's UUID
      position   → 0-based queue position; None for static
    """
    __slots__ = ("mode","poll_ms","streamers","pollers","tab_id","position")
    def __init__(s, mode, poll_ms, streamers, pollers, tab_id, position):
        s.mode      = mode
        s.poll_ms   = poll_ms
        s.streamers = streamers
        s.pollers   = pollers
        s.tab_id    = tab_id
        s.position  = position


def live(*, topic, render, changes, capacity, hard_cap=None):
    return Live(topic=topic, render=render, changes=changes,
                capacity=capacity, hard_cap=hard_cap)


def _wants_ctx(render):
    """True if render() takes a positional arg (for the RenderCtx)."""
    try:
        sig = inspect.signature(render)
    except (TypeError, ValueError):
        return False
    pos = [p for p in sig.parameters.values()
           if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.POSITIONAL_ONLY)]
    return len(pos) >= 1


def _call_render(render, ctx, wants_ctx):
    return render(ctx) if wants_ctx else render()


def _set_attr(node, key, value):
    a = getattr(node, "attrs", None)
    if a is None:
        raise TypeError(
            f"live() render() returned {type(node).__name__}; "
            "expected an html_tags Node with .attrs.")
    a[key] = value


def _read_tab_id(req):
    """Read the client's psseTab signal. Returns None if absent.

    The signal is established by the page handler (via page() with
    tab_signal=True, which puts data-signals__ifmissing on <body>).
    On every subsequent request, Datastar sends the signal automatically.
    If the signal is missing, the request is either pre-Datastar-init
    or from a non-Datastar client; we generate a one-shot ephemeral id
    (not persisted) so dispatch can still run."""
    from .transport import signals as parse_signals
    sigs = parse_signals(req)
    tab_id = sigs.get("psseTab")
    if tab_id:
        return tab_id, False           # client-owned, persistent
    return uuid.uuid4().hex, True      # ephemeral, one-shot


def resolve(live_resp, req, on_event=None):
    """Turn a Live into a concrete Sse or Html. Called by the transport."""
    from html_tags import render as h_render
    cap = live_resp.capacity
    t = live_resp.topic
    path = req["path"]

    tab_id, ephemeral = _read_tab_id(req)
    mode   = cap.assign(t, tab_id)

    if (live_resp.hard_cap is not None
            and cap.queue_size(t) > live_resp.hard_cap
            and mode != "static"):
        mode = "static"

    wants_ctx = _wants_ctx(live_resp.render)

    if mode == "live":
        return Sse(_stream(live_resp, wants_ctx, on_event, tab_id))

    # poll / static — one-shot HTML
    if mode == "poll":
        cap.touch_poll(t, tab_id)

    ctx = RenderCtx(
        mode      = mode,
        poll_ms   = cap.poll_interval_ms(t) if mode == "poll" else 0,
        streamers = cap.streamers(t),
        pollers   = cap.pollers(t),
        tab_id    = tab_id,
        position  = cap.position(t, tab_id),
    )
    node = _call_render(live_resp.render, ctx, wants_ctx)

    if mode == "poll":
        ms = ctx.poll_ms
        _set_attr(node, f"data-on-interval__duration.{ms}ms", f"@get('{path}')")

    # Note: we do NOT embed data-signals here. The signal lives on
    # <body> (set by page() helper) so it's never lost to morphs of
    # this element. Embedding here would attach the signal to a morphed
    # element, which causes Datastar to clear the signal when the
    # element is morphed again on the next response.

    if on_event:
        on_event({"type":"page_render","topic":t,"mode":mode,
                  "tab_id":tab_id, "position":ctx.position,
                  "ephemeral":ephemeral})
    return Html(h_render(node))


def _stream(live_resp, wants_ctx, on_event, tab_id):
    from html_tags import render as h_render
    cap = live_resp.capacity
    changes = live_resp.changes
    t = live_resp.topic
    started = time.time()
    frames = 0

    def _ctx():
        return RenderCtx(
            mode      = "live",
            poll_ms   = 0,
            streamers = cap.streamers(t),
            pollers   = cap.pollers(t),
            tab_id    = tab_id,
            position  = cap.position(t, tab_id),
        )

    def _render_frame():
        node = _call_render(live_resp.render, _ctx(), wants_ctx)
        # No signal embedding here. The signal lives on <body>, set by
        # page() at first page load. Subsequent SSE frames just morph
        # the inner content; the signal on body persists untouched.
        return h_render(node)

    with cap.join(t, tab_id):
        if on_event:
            on_event({"type":"stream_open","topic":t,"tab_id":tab_id})
        # Peers re-render with the new viewer count.
        try: changes.notify(t)
        except Exception: pass
        try:
            yield sse_mod.datastar_patch_elements(_render_frame())
            frames += 1
            while True:
                try:
                    woke = changes.wait(t, timeout=2)
                except Exception:
                    return
                try:
                    if woke:
                        yield sse_mod.datastar_patch_elements(_render_frame())
                        frames += 1
                    else:
                        yield sse_mod.keepalive()
                except (OSError, BrokenPipeError):
                    return
                except Exception:
                    if on_event:
                        on_event({"type":"stream_error","topic":t,"tab_id":tab_id})
                    return
        finally:
            if on_event:
                on_event({"type":"stream_close","topic":t,"tab_id":tab_id,
                          "duration_s":time.time()-started,
                          "frames":frames})
            try: changes.notify(t)
            except Exception: pass
