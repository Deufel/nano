"""py_sse server — Wirth-style minimal SSE web framework.

One OS thread per connection. Topic-scoped pub/sub. SSE with brotli
cross-frame compression. No async/await.

A page is a function that returns html_tags elements. The framework
wraps them in a stable envelope (html/head/body) so idiomorph has
something to anchor to.

A "live" page is a page wrapped with live(handler, topic). It serves
three transport modes based on per-page viewer count:

    0 to soft_cap         → live SSE       (data-init opens stream)
    soft_cap to hard_cap  → polling        (data-on-interval refetches)
    above hard_cap        → static         (no automatic updates)

One URL serves all three modes. The framework dispatches on the
incoming Accept header: text/event-stream means open a stream; anything
else means return a one-shot HTML page with the right transport
attribute baked into the wrapper.
"""

import logging
import re
import signal
import socket
import threading
import time
import zlib
from functools import wraps
from urllib.parse import parse_qs, unquote

import brotli

# ─── Configuration ────────────────────────────────────────────────────

MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_CONNECTIONS = 256
HEADER_READ_TIMEOUT = 10
BODY_READ_TIMEOUT = 60
SSE_WRITE_TIMEOUT = 60
SHUTDOWN_GRACE = 5
SSE_BROTLI_LGWIN = 18
SSE_BROTLI_QUALITY = 4
SSE_GZIP_LEVEL = 6

_METHOD_RE = re.compile(b'^[A-Z]{1,16}$')
_TARGET_RE = re.compile(b'^/[\\x21-\\x7e]{0,2047}$')
_HEADER_NAME_RE = re.compile(b"^[!#$%&'*+\\-.0-9A-Z^_`a-z|~]{1,128}$")
_COOKIE_FORBIDDEN = re.compile('[\\r\\n\\x00]')
PARAM_RE = re.compile('\\{(\\w+)\\}')

logger = logging.getLogger('py_sse')

# The DOM id used for the live wrapper. Idiomorph anchors on this.
LIVE_ROOT_ID = "live-root"


# ─── Section 1: Changes — topic-scoped pub/sub ────────────────────────

class Changes:
    """In-process pub/sub. Threads wait on dotted subject patterns;
    publishes match by walking the hierarchy.

        # Writer
        changes.notify("game.5.score")

        # Reader — single pattern
        while True:
            changes.wait("game.5.*", timeout=15)
            yield render_frame()

        # Reader — multiple patterns (wakes on any match)
        changes.wait(["account.*", "product.*", "sale.*"], timeout=15)

    Matching: notify("a.b.c") wakes waiters on "a.b.c", "a.b.*",
    "a.*", and "*".
    """

    def __init__(self):
        self._events = {}        # pattern → Event (single-pattern waiters)
        self._fanout = {}        # pattern → set of Events (multi-pattern waiters)
        self._lock = threading.Lock()

    def _event_for(self, pattern):
        with self._lock:
            if pattern not in self._events:
                self._events[pattern] = threading.Event()
            return self._events[pattern]

    def notify(self, subject):
        parts = subject.split(".")
        patterns = [subject]
        for i in range(len(parts) - 1, -1, -1):
            patterns.append(".".join(parts[:i] + ["*"]))
        with self._lock:
            for p in patterns:
                if p in self._events:
                    self._events[p].set()
                if p in self._fanout:
                    for evt in self._fanout[p]:
                        evt.set()

    def wait(self, pattern, timeout=None):
        """Wait for a notify matching `pattern`.

        `pattern` is a string (single subject) or iterable of strings
        (wake on any match). Returns True on match, False on timeout.
        """
        if isinstance(pattern, str):
            evt = self._event_for(pattern)
            ok = evt.wait(timeout=timeout)
            if ok:
                evt.clear()
            return ok

        # Multi-pattern: register a private Event under each pattern's
        # fanout set, wait, then deregister.
        patterns = tuple(pattern)
        evt = threading.Event()
        with self._lock:
            for p in patterns:
                self._fanout.setdefault(p, set()).add(evt)
        try:
            return evt.wait(timeout=timeout)
        finally:
            with self._lock:
                for p in patterns:
                    s = self._fanout.get(p)
                    if s is not None:
                        s.discard(evt)
                        if not s:
                            del self._fanout[p]


# ─── Section 2: low-level I/O ─────────────────────────────────────────

def read_until_double_crlf(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("client closed before sending headers")
        buf += chunk
        if len(buf) > MAX_HEADER_BYTES:
            raise ValueError("request headers exceed limit")
    head, _, leftover = buf.partition(b"\r\n\r\n")
    return head, leftover


def read_body(sock, content_length, already_have, limit=MAX_BODY_BYTES):
    if content_length < 0:
        raise ValueError("negative content-length")
    if content_length > limit:
        raise ValueError(f"body exceeds limit ({content_length} > {limit})")
    buf = already_have
    while len(buf) < content_length:
        chunk = sock.recv(min(65536, content_length - len(buf)))
        if not chunk:
            raise ConnectionError("client closed mid-body")
        buf += chunk
    return buf[:content_length]


def write_response(sock, status, headers, body=b""):
    if isinstance(body, str):
        body = body.encode("utf-8")
    reason = {200: "OK", 204: "No Content", 303: "See Other",
              400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
              404: "Not Found", 408: "Request Timeout",
              413: "Payload Too Large", 500: "Internal Server Error",
              503: "Service Unavailable",
              507: "Insufficient Storage"}.get(status, "OK")
    lines = [f"HTTP/1.1 {status} {reason}"]
    headers = list(headers)
    headers.append(("content-length", str(len(body))))
    headers.append(("connection", "close"))
    for k, v in headers:
        lines.append(f"{k}: {v}")
    head = "\r\n".join(lines).encode("ascii") + b"\r\n\r\n"
    try:
        sock.sendall(head + body)
    except OSError:
        pass


def write_sse_headers(sock, extra_headers=(), encoding="identity"):
    lines = [
        "HTTP/1.1 200 OK",
        "content-type: text/event-stream",
        "cache-control: no-cache",
        "x-accel-buffering: no",
        "proxy-buffering: off",
        "connection: keep-alive",
    ]
    if encoding and encoding != "identity":
        lines.append(f"content-encoding: {encoding}")
        lines.append("vary: accept-encoding")
    for k, v in extra_headers:
        lines.append(f"{k}: {v}")
    sock.sendall("\r\n".join(lines).encode("ascii") + b"\r\n\r\n")


def write_sse_frame(sock, payload, encoder=None):
    raw = payload.encode("utf-8") + b"\n\n"
    if encoder is None or encoder.name == "identity":
        sock.sendall(raw)
        return
    chunk = encoder.encode(raw) + encoder.flush()
    if chunk:
        sock.sendall(chunk)


# ─── Section 3: SSE encoder ───────────────────────────────────────────

class _SseEncoder:
    """Per-connection streaming encoder. Brotli's cross-frame state
    is the whole game: frame N+1 mostly equals frame N, so the encoder
    emits 'copy from N KB ago' for almost everything."""
    __slots__ = ("name", "_c")

    def __init__(self, encoding):
        self.name = encoding
        if encoding == "br":
            self._c = brotli.Compressor(
                quality=SSE_BROTLI_QUALITY, lgwin=SSE_BROTLI_LGWIN)
        elif encoding == "gzip":
            self._c = zlib.compressobj(level=SSE_GZIP_LEVEL, wbits=31)
        elif encoding == "identity":
            self._c = None
        else:
            raise ValueError(f"unsupported sse encoding: {encoding}")

    def encode(self, data):
        if self._c is None:
            return data
        if self.name == "br":
            return self._c.process(data)
        return self._c.compress(data)

    def flush(self):
        if self._c is None:
            return b""
        if self.name == "br":
            return self._c.flush()
        return self._c.flush(zlib.Z_SYNC_FLUSH)

    def finish(self):
        if self._c is None:
            return b""
        if self.name == "br":
            return self._c.finish()
        return self._c.flush(zlib.Z_FINISH)


def pick_encoding(req, prefer=("br", "gzip")):
    raw = req["headers"].get("accept-encoding", "").lower()
    if not raw:
        return "identity"
    offered = {part.split(";", 1)[0].strip() for part in raw.split(",")}
    for enc in prefer:
        if enc == "br" and "br" in offered:
            return "br"
        if enc == "gzip" and "gzip" in offered:
            return "gzip"
    return "identity"


# ─── Section 4: request parsing ───────────────────────────────────────

def parse_request(sock):
    sock.settimeout(HEADER_READ_TIMEOUT)
    head, leftover = read_until_double_crlf(sock)
    raw_lines = head.split(b"\r\n")
    if not raw_lines or not raw_lines[0]:
        raise ValueError("empty request")
    rl = raw_lines[0]
    if len(rl) > 8192:
        raise ValueError("request line too long")
    parts = rl.split(b" ")
    if len(parts) != 3:
        raise ValueError("malformed request line")
    method_b, target_b, version_b = parts
    if not _METHOD_RE.match(method_b):
        raise ValueError("invalid method")
    if not _TARGET_RE.match(target_b):
        raise ValueError("invalid target")
    if version_b not in (b"HTTP/1.1", b"HTTP/1.0"):
        raise ValueError("unsupported HTTP version")
    method = method_b.decode("ascii")
    target = target_b.decode("ascii")
    raw_path, _, raw_query = target.partition("?")
    path = unquote(raw_path)
    query = {k: v[0] if len(v) == 1 else v
             for k, v in parse_qs(raw_query).items()}
    headers = {}
    for raw in raw_lines[1:]:
        if not raw:
            continue
        if b":" not in raw:
            raise ValueError("malformed header line")
        name_b, _, value_b = raw.partition(b":")
        if not _HEADER_NAME_RE.match(name_b):
            raise ValueError("invalid header name")
        if any(b in value_b for b in (b"\r", b"\n", b"\x00")):
            raise ValueError("invalid byte in header value")
        headers[name_b.decode("ascii").lower()] = value_b.decode("iso-8859-1").strip()
    cookies = parse_cookies(headers.get("cookie", ""))
    content_length = 0
    if "content-length" in headers:
        try:
            content_length = int(headers["content-length"])
        except ValueError:
            raise ValueError("invalid content-length")
    if content_length:
        sock.settimeout(BODY_READ_TIMEOUT)
        body = read_body(sock, content_length, leftover)
    else:
        body = b""
    return {
        "method":  method,
        "path":    path,
        "query":   query,
        "headers": headers,
        "cookies": cookies,
        "body":    body,
        "_sock":   sock,
        "params":  {},
        "_cookies_out": [],
    }


def parse_cookies(cookie_header):
    out = {}
    for pair in cookie_header.split(";"):
        if "=" in pair:
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k and not _COOKIE_FORBIDDEN.search(k) and not _COOKIE_FORBIDDEN.search(v):
                out[k] = v
    return out


def set_cookie(req, name, value, **opts):
    if _COOKIE_FORBIDDEN.search(name) or _COOKIE_FORBIDDEN.search(str(value)):
        raise ValueError("cookie name/value contains forbidden control characters")
    pieces = [f"{name}={value}"]
    for k, v in opts.items():
        k = k.replace("_", "-")
        if _COOKIE_FORBIDDEN.search(str(v)):
            raise ValueError(f"cookie option {k} contains forbidden characters")
        if isinstance(v, bool):
            if v:
                pieces.append(k)
        else:
            pieces.append(f"{k}={v}")
    req["_cookies_out"].append("; ".join(pieces))


def signals(req):
    """Parse Datastar signals from a request."""
    import json
    if req["method"] == "GET":
        raw = req["query"].get("datastar", "{}")
        return json.loads(raw) if isinstance(raw, str) else raw
    if not req["body"]:
        return {}
    data = json.loads(req["body"])
    return data.get("datastar", data) if isinstance(data, dict) else data


# ─── Section 5: routing ───────────────────────────────────────────────

def compile_routes(routes):
    compiled = []
    for method, path, handler in routes:
        if "{" in path:
            regex = "^" + PARAM_RE.sub(r"(?P<\1>[^/]+)", path) + "$"
            compiled.append((method.upper(), re.compile(regex), handler))
        else:
            compiled.append((method.upper(), re.compile("^" + re.escape(path) + "$"), handler))
    return compiled


def match_route(routes, method, path):
    for route_method, pattern, handler in routes:
        if route_method != method:
            continue
        m = pattern.match(path)
        if m:
            return handler, m.groupdict()
    return None


# ─── Section 6: response helpers ──────────────────────────────────────

def html(body, status=200):
    return (status, [("content-type", "text/html; charset=utf-8")], body)


def redirect(location, status=303):
    if "\r" in location or "\n" in location:
        raise ValueError("redirect target contains forbidden characters")
    return (status, [("location", location)], b"")


def no_content():
    return (204, [], b"")


def blob(data, content_type, filename=None):
    headers = [
        ("content-type", content_type),
        ("x-content-type-options", "nosniff"),
    ]
    if filename:
        safe = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_"
                       for c in filename)[:200] or "file"
        headers.append(("content-disposition", f'attachment; filename="{safe}"'))
    return (200, headers, data)


def error(status, message=""):
    return (status, [("content-type", "text/plain; charset=utf-8")], message)


# ─── Section 7: SSE primitives ────────────────────────────────────────

def sse_data(text):
    return "\n".join(f"data: {line}" for line in (text.splitlines() or [""]))


def sse_event(event_name, data):
    return f"event: {event_name}\n{sse_data(data)}"


def sse_keepalive():
    return ":"


# ─── Section 8: html_tags integration ─────────────────────────────────
#
# html_tags is a hard dependency. We use:
#   - Node          base class for type checking
#   - Safe          str subclass that bypasses escaping when rendered
#   - render        Node → HTML string (handles Node, Safe, str)
#   - h.div etc.    tag factories — we build the envelope and wrapper
#                   as real Node trees, so attribute values are
#                   properly escaped automatically
#
# A handler returns one of:
#   - A Node                       (single root element)
#   - An iterable of Node | Safe   (rendered in sequence as a fragment)
#   - A Safe                       (already-rendered HTML, pass-through)
#
# Nothing else. Strings are not accepted because we can't tell escaped
# from unescaped — use Safe() to be explicit, or wrap in h.span("text").

from html_tags import h, Node, Safe, render as _h_render


def _render_children(value):
    """Render a handler's output to an HTML string.

    Accepts: Node, Safe, or iterable of Node | Safe. Raises TypeError
    on anything else with a useful message.
    """
    if isinstance(value, Node):
        return _h_render(value)
    if isinstance(value, Safe):
        return value
    if hasattr(value, "__node__"):
        return _h_render(value.__node__())
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        parts = []
        for child in value:
            if isinstance(child, Node):
                parts.append(_h_render(child))
            elif isinstance(child, Safe):
                parts.append(child)
            elif hasattr(child, "__node__"):
                parts.append(_h_render(child.__node__()))
            else:
                raise TypeError(
                    f"live handler returned an iterable containing "
                    f"{type(child).__name__}; expected Node or Safe. "
                    f"Wrap strings explicitly with Safe() or h.span().")
        return "".join(parts)
    raise TypeError(
        f"live handler returned {type(value).__name__}; expected "
        f"Node, Safe, or iterable of Node/Safe.")


def _envelope_doc(head_fragments, body_inner_node, ui_theme="dark"):
    """Build a full HTML document as a Node tree, render it once.

    body_inner_node is a Node (the <div id="live-root"> wrapper).
    Everything is properly escaped — no string concatenation.
    """
    page = h.html(
        {"id": "page", "data-ui-theme": ui_theme},
        h.head(
            h.meta(charset="utf-8"),
            h.meta(name="viewport",
                   content="width=device-width, initial-scale=1"),
            *head_fragments,
        ),
        h.body({"class": "page stage"}, body_inner_node),
    )
    return "<!doctype html>" + _h_render(page)


# ─── Section 9: live() — the heart of the new API ─────────────────────
#
# live(handler, topic) wraps a page handler with SSE/polling/static
# degradation. The same URL serves all three modes; the framework
# dispatches on the request's Accept header.
#
# The handler returns html_tags elements (a list or a single element).
# The framework:
#
#   1. Renders the elements to a string.
#   2. Wraps them in <div id="live-root" data-...> based on transport.
#   3. For SSE requests: streams the wrapped content as patch-elements
#      events, re-rendering on changes.notify(topic).
#   4. For non-SSE requests: returns the full HTML page (envelope +
#      wrapped content) with the appropriate transport attribute.

def live(handler=None, *, topic=None, hard_cap=None):
    """Wrap a handler for SSE/polling/static degradation.

    Usable as a function or a decorator:

        # As a function (explicit composition)
        routes = [("GET", "/", live(home, topic="todo"))]

        # As a decorator
        @live(topic="todo")
        def home(req): ...
        routes = [("GET", "/", home)]

        # With URL parameter expansion
        @live(topic="game.{id}.*")
        def scorecard(req): ...   # req["params"]["id"] is substituted

    Args:
        handler:  the page handler. Returns Node, Safe, or iterable of
                  those. None means decorator form.
        topic:    string or callable(req) -> string. Identifies the page
                  for Capacity bucketing and Changes subscription. If a
                  string contains "{name}", it is formatted with values
                  from req["params"] before use.
        hard_cap: optional per-route override of the Capacity's hard_cap.
    """
    if handler is None:
        def decorator(fn):
            return live(fn, topic=topic, hard_cap=hard_cap)
        return decorator

    if topic is None:
        raise ValueError("live() requires a topic")

    def _topic_for(req):
        if callable(topic):
            return topic(req)
        if "{" in topic:
            return topic.format(**req.get("params", {}))
        return topic

    @wraps(handler)
    def wrapper(req):
        capacity = req["_capacity"]
        changes = req["_changes"]
        head_fragments = req.get("_head", [])
        ui_theme = req.get("_ui_theme", "dark")
        on_event = req.get("_on_event") or _noop
        if capacity is None or changes is None:
            raise RuntimeError(
                "live() requires req['_capacity'] and req['_changes']")

        t = _topic_for(req)

        def render_inner_html():
            return _render_children(handler(req))

        # The framework distinguishes browser navigations from Datastar
        # follow-up requests by the `Datastar-Request` header. Navigations
        # always get a full HTML envelope with data-init; Datastar requests
        # get capacity-aware transport (SSE for live, HTML+interval for
        # poll, HTML+nothing for static).
        #
        # Why not branch on the Accept header? Datastar accepts both
        # text/event-stream and text/html. The server chooses based on
        # capacity, not the client. Branching on Accept caused polled
        # tabs to upgrade to SSE on their first interval tick because
        # Datastar always sends text/event-stream in Accept.
        is_datastar = req["headers"].get("datastar-request", "").lower() == "true"
        path = req["path"]

        if not is_datastar:
            # Browser navigation. Always return HTML with data-init so the
            # client's Datastar follow-up can resolve transport. No-JS
            # clients see the rendered content and stop here.
            inner_html = render_inner_html()
            wrapper_node = h.div({"id": LIVE_ROOT_ID,
                                  "data-init": f"@get('{path}')"},
                                 Safe(inner_html))
            on_event({"type": "page_render", "topic": t, "mode": "navigation",
                      "path": path, "viewer_count": capacity.count(t)})
            return html(_envelope_doc(head_fragments, wrapper_node, ui_theme))

        # Datastar request. Decide transport by capacity.
        mode = capacity.mode(t)
        if hard_cap is not None and capacity.count(t) >= hard_cap:
            mode = "static"

        if mode == "live":
            return _stream_live(t, capacity, changes, render_inner_html,
                                on_event, req)

        # Poll or static: respond with HTML. No capacity slot held.
        inner_html = render_inner_html()
        wrapper_attrs = {"id": LIVE_ROOT_ID}
        if mode == "poll":
            interval_ms = capacity.poll_interval_ms(t)
            wrapper_attrs[f"data-on-interval__duration.{interval_ms}ms"] = (
                f"@get('{path}')")
        # static: just the id, no transport attribute
        wrapper_node = h.div(wrapper_attrs, Safe(inner_html))
        on_event({"type": "page_render", "topic": t, "mode": mode,
                  "path": path, "viewer_count": capacity.count(t)})
        # Datastar response: just the morphed fragment, not a full envelope.
        return html(_h_render(wrapper_node))

    return wrapper


def _noop(_event):
    pass


def _stream_live(topic, capacity, changes, render_inner_html,
                 on_event, req):
    """Generator for the SSE live stream. Each frame patches the
    #live-root element with the latest content. Fires observability
    events on open / close / error / frame.
    """
    def frame():
        wrapper = h.div({"id": LIVE_ROOT_ID}, Safe(render_inner_html()))
        return f"event: datastar-patch-elements\ndata: elements {_h_render(wrapper)}"

    started = time.time()
    frames_sent = 0
    remote = (req.get("_remote") or "?")
    on_event({"type": "stream_open", "topic": topic, "remote": remote})

    reason = "client_disconnect"
    try:
        # Wake peer streams parked in changes.wait(topic) so they re-render
        # with the new viewer count. Joins/leaves are rarer than writes; the
        # cost is bounded and matches what writes already do.
        with capacity.join(topic):
            changes.notify(topic)       # peer wake on join
            frames_sent += 1
            yield frame()
            while True:
                # 2-second timeout = ~4 seconds to detect a dead client
                # in the worst case (TCP buffers one write, second write
                # surfaces BrokenPipeError). Was 15s; that was too slow
                # for capacity to feel responsive on tab close.
                if changes.wait(topic, timeout=2):
                    try:
                        frames_sent += 1
                        yield frame()
                    except (OSError, BrokenPipeError):
                        return
                else:
                    try:
                        yield sse_keepalive()
                    except (OSError, BrokenPipeError):
                        return
    except Exception as e:
        reason = f"error:{type(e).__name__}"
        raise
    finally:
        # capacity.__exit__ has already decremented by the time we get here
        # (Python runs nested context managers / generator unwinding before
        # outer finally). Wake peers with the lower count.
        try:    changes.notify(topic)   # peer wake on leave
        except: pass
        on_event({"type": "stream_close", "topic": topic, "remote": remote,
                  "reason": reason, "frames": frames_sent,
                  "duration_s": time.time() - started})


# ─── Section 10: connection handling ──────────────────────────────────

def handle_connection(sock, addr, routes, before_hooks,
                      capacity=None, changes_obj=None,
                      head_fragments=None, ui_theme="dark",
                      on_event=None, access_log=True):
    """Run one request to completion, then close the socket."""
    start = time.time()
    status = 0
    method = path = "?"
    req = None
    head_fragments = head_fragments or []

    try:
        try:
            req = parse_request(sock)
            req["_capacity"] = capacity
            req["_changes"] = changes_obj
            req["_head"] = head_fragments
            req["_ui_theme"] = ui_theme
            req["_on_event"] = on_event
            req["_remote"] = addr[0] if addr else None
        except socket.timeout:
            status = 408
            write_response(sock, 408, [("content-type", "text/plain")],
                           "request timeout")
            return
        except (ValueError, ConnectionError) as e:
            status = 400
            logger.info("400 from %s: %s", addr[0] if addr else "?", e)
            write_response(sock, 400, [("content-type", "text/plain")],
                           "bad request")
            return
        except Exception:
            status = 400
            logger.exception("error parsing request from %s", addr)
            write_response(sock, 400, [("content-type", "text/plain")],
                           "bad request")
            return

        method = req["method"]
        path = req["path"]

        matched = match_route(routes, method, path)
        if matched is None:
            status = 404
            write_response(sock, 404, [("content-type", "text/plain")],
                           "not found")
            return
        handler, params = matched
        req["params"] = params

        try:
            for hook in before_hooks:
                hook(req)
        except Exception:
            status = 500
            logger.exception("before-hook failed")
            write_response(sock, 500, [("content-type", "text/plain")],
                           "internal error")
            return

        try:
            result = handler(req)
        except Exception:
            status = 500
            logger.exception("handler raised for %s %s", method, path)
            write_response(sock, 500, [("content-type", "text/plain")],
                           "internal error")
            return

        if result is None:
            result = no_content()

        # Tuple response (one-shot)
        if isinstance(result, tuple):
            status_, headers, body = result
            status = status_
            for c in req["_cookies_out"]:
                headers = list(headers) + [("set-cookie", c)]
            sock.settimeout(SSE_WRITE_TIMEOUT)
            write_response(sock, status, headers, body)
            return

        # Generator response (SSE)
        status = 200
        sock.settimeout(SSE_WRITE_TIMEOUT)
        extra = [("set-cookie", c) for c in req["_cookies_out"]]
        encoding = pick_encoding(req)
        encoder = _SseEncoder(encoding)
        try:
            write_sse_headers(sock, extra, encoding=encoding)
            for frame in result:
                if frame is None:
                    continue
                write_sse_frame(sock, frame, encoder=encoder)
        except (OSError, ConnectionError, socket.timeout):
            pass
        except Exception:
            logger.exception("SSE generator raised for %s %s", method, path)
        finally:
            try:
                tail = encoder.finish()
                if tail:
                    sock.sendall(tail)
            except Exception:
                pass
            try:
                result.close()
            except Exception:
                pass

    finally:
        try:
            sock.close()
        except Exception:
            pass
        if access_log:
            dt_ms = (time.time() - start) * 1000
            logger.info("%s %s %s → %d %.1fms",
                        addr[0] if addr else "?", method, path, status, dt_ms)


# ─── Section 11: serve() ──────────────────────────────────────────────

class _ShutdownFlag:
    def __init__(self):
        self._stop = False

    def set(self):
        self._stop = True

    def is_set(self):
        return self._stop


def serve(routes, *, host="127.0.0.1", port=8000,
          before_hooks=(), capacity=None, changes=None,
          head=None, ui_theme="dark", on_event=None,
          max_connections=MAX_CONNECTIONS, access_log=True):
    """Run the server.

    Args:
        routes:          list of (method, path, handler) tuples
        before_hooks:    callables run before each handler; may mutate req
        capacity:        Capacity instance (default: Capacity())
        changes:         Changes instance (default: new Changes())
        head:            list of html_tags elements injected into <head>
                         of every live-page response.
        ui_theme:        value for the <html data-ui-theme="..."> attr
        on_event:        optional callback(event_dict) for observability.
                         Fires on stream_open / stream_close / page_render.
                         No-op when None.
        max_connections: cap on concurrent connection threads
        access_log:      one INFO line per request
    """
    if not logger.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(sh)
        logger.setLevel(logging.INFO)

    from .capacity import Capacity
    if capacity is None:
        capacity = Capacity()
    if changes is None:
        changes = Changes()
    head_fragments = list(head or [])

    compiled = compile_routes(routes)
    semaphore = threading.BoundedSemaphore(max_connections)
    stop = _ShutdownFlag()

    def _signal(_signum, _frame):
        logger.info("shutdown signal received")
        stop.set()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _signal)
        signal.signal(signal.SIGTERM, _signal)
    else:
        logger.info("serve() running in background thread; SIGINT/SIGTERM "
                    "handlers not installed")

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(128)
    s.settimeout(0.5)
    logger.info("py_sse listening on http://%s:%d (max_connections=%d)",
                host, port, max_connections)

    in_flight = []
    try:
        while not stop.is_set():
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not semaphore.acquire(blocking=False):
                logger.warning("connection cap (%d) reached, dropping %s",
                               max_connections, addr[0])
                try:
                    write_response(conn, 503,
                                   [("content-type", "text/plain"),
                                    ("retry-after", "1")],
                                   "server busy")
                except Exception:
                    pass
                conn.close()
                continue

            def _run(c=conn, a=addr, cap=capacity, ch=changes,
                     hf=head_fragments, theme=ui_theme, oe=on_event):
                try:
                    handle_connection(
                        c, a, compiled, before_hooks,
                        capacity=cap, changes_obj=ch,
                        head_fragments=hf, ui_theme=theme,
                        on_event=oe, access_log=access_log)
                finally:
                    semaphore.release()

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            in_flight.append(t)
            if len(in_flight) > 1024:
                in_flight = [x for x in in_flight if x.is_alive()]
    finally:
        s.close()
        deadline = time.time() + SHUTDOWN_GRACE
        for t in in_flight:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            t.join(timeout=remaining)
        alive = sum(1 for t in in_flight if t.is_alive())
        if alive:
            logger.warning(
                "shutdown: %d threads still running after grace period", alive)
        logger.info("shutdown complete")
