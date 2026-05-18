"""live() helper. Composes Changes + Capacity + SSE.

A handler returns live(...). The transport resolves it into one of:
  - Sse stream      (capacity says live)
  - Html with data-on-interval on the top-level element (capacity says poll)
  - Html with no transport attrs (capacity says static, or hard_cap reached)

The render callable returns a single top-level html_tags Node. The
Node MUST have an id — Datastar morphs by element id per the response.

render() can optionally take a RenderCtx argument:

    def render(ctx):
        # ctx.mode      → "live" | "poll" | "static"
        # ctx.poll_ms   → current poll interval if mode == "poll"
        # ctx.streamers → SSE viewers on this topic right now
        # ctx.pollers   → polled viewers on this topic right now
        ...

If render takes no arguments, it's called with none. The framework
inspects the signature once at live() construction.

State: none here. live() is just a constructor. State lives in the
Changes and Capacity instances the caller passes.
"""
import inspect
import time

from .responses import Live, Html, Sse
from . import sse as sse_mod


class RenderCtx:
    """Context passed into render() when render takes a parameter.

    Frozen for the duration of one render call. Cheap to construct;
    read fresh values from capacity for each render."""
    __slots__ = ("mode","poll_ms","streamers","pollers")
    def __init__(s, mode, poll_ms, streamers, pollers):
        s.mode, s.poll_ms, s.streamers, s.pollers = mode, poll_ms, streamers, pollers


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


def resolve(live_resp, req, on_event=None):
    """Turn a Live into a concrete Sse or Html. Called by the transport.
    Reads req["cookies"] for the tab id; adds to req["_cookies_out"] if
    none was set."""
    import uuid
    from html_tags import render as h_render
    cap = live_resp.capacity
    t = live_resp.topic
    hard = live_resp.hard_cap if live_resp.hard_cap is not None else cap.hard_cap
    path = req["path"]

    # Tab identity. Used to count distinct polled tabs (not request volume).
    tab_id = req.get("cookies", {}).get("pysse_tab")
    if not tab_id:
        tab_id = uuid.uuid4().hex
        # Session cookie: no Max-Age, no Expires. Dies when browser tab/window closes.
        # HttpOnly so JS can't see it. SameSite=Lax so it works on normal navigations.
        req.setdefault("_cookies_out", []).append(
            f"pysse_tab={tab_id}; Path=/; HttpOnly; SameSite=Lax"
        )

    mode = cap.mode(t)
    if hard is not None and cap.count(t) >= hard:
        mode = "static"

    wants_ctx = _wants_ctx(live_resp.render)

    if mode == "live":
        return Sse(_stream(live_resp, wants_ctx, on_event))

    # poll / static: one-shot HTML
    ctx = RenderCtx(
        mode=mode,
        poll_ms=cap.poll_interval_ms(t) if mode == "poll" else 0,
        streamers=cap.streamers(t),
        pollers=cap.pollers(t),
    )
    node = _call_render(live_resp.render, ctx, wants_ctx)

    if mode == "poll":
        # Record this specific tab. One tab = one count, no matter how
        # fast it polls.
        cap.touch_poll(t, tab_id)
        ms = ctx.poll_ms
        _set_attr(node, f"data-on-interval__duration.{ms}ms", f"@get('{path}')")

    if on_event:
        on_event({"type":"page_render","topic":t,"mode":mode,
                  "viewer_count":cap.count(t)})
    return Html(h_render(node))


def _set_attr(node, key, value):
    a = getattr(node, "attrs", None)
    if a is None:
        raise TypeError(
            f"live() render() returned {type(node).__name__}; "
            "expected an html_tags Node with .attrs.")
    a[key] = value


def _stream(live_resp, wants_ctx, on_event):
    from html_tags import render as h_render
    cap = live_resp.capacity
    changes = live_resp.changes
    t = live_resp.topic
    started = time.time()
    frames = 0

    def _ctx():
        return RenderCtx(mode="live", poll_ms=0,
                         streamers=cap.streamers(t),
                         pollers=cap.pollers(t))

    with cap.join(t):
        if on_event:
            on_event({"type":"stream_open","topic":t,
                      "viewer_count":cap.count(t)})
        # peers re-render with new viewer count
        try: changes.notify(t)
        except Exception: pass
        try:
            yield sse_mod.datastar_patch_elements(
                h_render(_call_render(live_resp.render, _ctx(), wants_ctx)))
            frames += 1
            while True:
                try:
                    woke = changes.wait(t, timeout=2)
                except Exception:
                    return
                try:
                    if woke:
                        yield sse_mod.datastar_patch_elements(
                            h_render(_call_render(live_resp.render, _ctx(), wants_ctx)))
                        frames += 1
                    else:
                        yield sse_mod.keepalive()
                except (OSError, BrokenPipeError):
                    return
                except Exception:
                    if on_event:
                        on_event({"type":"stream_error","topic":t})
                    return
        finally:
            if on_event:
                on_event({"type":"stream_close","topic":t,
                          "viewer_count":cap.count(t)-1,
                          "duration_s":time.time()-started,
                          "frames":frames})
            try: changes.notify(t)
            except Exception: pass
