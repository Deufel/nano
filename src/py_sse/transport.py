"""HTTP/1.1 transport. The minimum needed to serve our routers.

Responsibilities:
  - Accept TCP connections, spawn a thread per connection.
  - Parse the request line + headers + body.
  - Resolve the route via the Router; populate req["params"].
  - Run the handler in a fault boundary.
  - Convert the handler's Response into bytes on the wire.
  - For Sse responses, stream frames until the client disconnects.

The Connection-as-long-lived-entity for SSE is implicit: a thread sits
on the socket, iterating the generator, yielding frames. When the client
disconnects, the next write raises BrokenPipe and the thread exits.

Each handler runs in a fault boundary: any exception → 500 + log.
No partial responses leak; the connection closes cleanly.
"""
import json
import logging
import re
import socket
import threading
import time
from urllib.parse import parse_qs, unquote

from .responses import Response, Html, Redirect, Empty, Sse, Live
from . import _live as live_mod

logger = logging.getLogger("py_sse")

MAX_HEADER_BYTES = 64 * 1024
SSE_WRITE_TIMEOUT = 30.0
KEEP_ALIVE = "Connection: keep-alive\r\n"

REASON = {200:"OK", 204:"No Content", 301:"Moved Permanently",
          303:"See Other", 400:"Bad Request", 404:"Not Found",
          405:"Method Not Allowed", 408:"Request Timeout",
          413:"Payload Too Large", 500:"Internal Server Error",
          503:"Service Unavailable"}


# ─── request parsing ─────────────────────────────────────────────────

def _readline(sock_file):
    line = sock_file.readline(MAX_HEADER_BYTES + 1)
    if len(line) > MAX_HEADER_BYTES: raise ValueError("header line too long")
    return line

def parse_request(sock):
    sock.settimeout(15.0)
    f = sock.makefile("rb", buffering=0)
    line = _readline(f)
    if not line: raise ConnectionAbortedError("empty")
    parts = line.decode("latin-1").rstrip("\r\n").split(" ")
    if len(parts) != 3: raise ValueError(f"bad request line: {parts}")
    method, target, _ver = parts
    path, _, qs = target.partition("?")
    path = unquote(path)
    headers = {}
    total = 0
    while True:
        ln = _readline(f)
        total += len(ln)
        if total > MAX_HEADER_BYTES: raise ValueError("headers too large")
        if ln in (b"\r\n", b"\n", b""): break
        k, _, v = ln.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length","0") or "0")
    body = f.read(n) if n > 0 else b""
    cookies = _parse_cookies(headers.get("cookie",""))
    return {
        "method": method.upper(), "path": path, "raw_query": qs,
        "query": parse_qs(qs, keep_blank_values=True),
        "headers": headers, "body": body, "cookies": cookies,
        "_cookies_out": [],
        "params": {},
    }

def _parse_cookies(s):
    out = {}
    for piece in s.split(";"):
        k, _, v = piece.partition("=")
        k = k.strip()
        if k: out[k] = v.strip()
    return out


# ─── response writing ─────────────────────────────────────────────────

def _status_line(code): return f"HTTP/1.1 {code} {REASON.get(code,'OK')}\r\n"

def write_response(sock, status, headers, body):
    if isinstance(body, str): body = body.encode("utf-8")
    elif body is None: body = b""
    out = [_status_line(status)]
    seen = set()
    for k, v in headers:
        seen.add(k.lower())
        out.append(f"{k}: {v}\r\n")
    if "content-length" not in seen:
        out.append(f"content-length: {len(body)}\r\n")
    if "connection" not in seen:
        out.append("connection: close\r\n")
    out.append("\r\n")
    sock.sendall("".join(out).encode("latin-1"))
    if body: sock.sendall(body)

def write_sse_head(sock, extra_headers):
    out = [_status_line(200),
           "content-type: text/event-stream\r\n",
           "cache-control: no-cache\r\n",
           "x-accel-buffering: no\r\n",
           "connection: keep-alive\r\n"]
    for k, v in extra_headers:
        out.append(f"{k}: {v}\r\n")
    out.append("\r\n")
    sock.sendall("".join(out).encode("latin-1"))


# ─── signals helper (Datastar) ───────────────────────────────────────

def signals(req):
    """Parse Datastar signals from req. Supports JSON body (POST) and
    `datastar` query param (GET). Returns {} on absent/invalid."""
    if req["body"]:
        ct = req["headers"].get("content-type","")
        if "application/json" in ct:
            try: return json.loads(req["body"])
            except Exception: return {}
    ds = req["query"].get("datastar")
    if ds:
        try: return json.loads(ds[0])
        except Exception: return {}
    return {}


# ─── the dispatcher: handler → bytes on the wire ─────────────────────

def _dispatch(sock, req, router, on_event):
    handler, params = router.resolve(req["method"], req["path"])
    if handler is None:
        write_response(sock, 404, [("content-type","text/plain")], "not found")
        return
    req["params"] = params

    try:
        result = handler(req)
    except Exception:
        logger.exception("handler raised: %s %s", req["method"], req["path"])
        write_response(sock, 500, [("content-type","text/plain")], "internal error")
        return

    # Resolve Live → Sse | Html (the only response that needs resolution)
    if isinstance(result, Live):
        try:
            result = live_mod.resolve(result, req["path"], on_event=on_event)
        except Exception:
            logger.exception("live.resolve raised")
            write_response(sock, 500, [("content-type","text/plain")], "internal error")
            return

    if isinstance(result, Empty):
        write_response(sock, result.status, result.headers, b"")
        return

    if isinstance(result, Redirect):
        write_response(sock, result.status,
                       [("location", result.location)], b"")
        return

    if isinstance(result, Html):
        body = result.body if isinstance(result.body,(bytes,str)) else str(result.body)
        headers = list(result.headers) + [("content-type","text/html; charset=utf-8")]
        write_response(sock, result.status, headers, body)
        return

    if isinstance(result, Sse):
        write_sse_head(sock, [])
        sock.settimeout(SSE_WRITE_TIMEOUT)
        try:
            for frame in result.frames:
                if frame is None: continue
                data = frame.encode("utf-8") if isinstance(frame, str) else frame
                sock.sendall(data)
        except (OSError, ConnectionError):
            pass
        finally:
            try: result.frames.close()
            except Exception: pass
        return

    # Unknown return type → 500
    logger.error("handler returned %s (not a Response)", type(result).__name__)
    write_response(sock, 500, [("content-type","text/plain")], "internal error")


def _handle_connection(sock, addr, router, on_event, access_log):
    start = time.time()
    method = path = "?"
    try:
        try:
            req = parse_request(sock)
            method, path = req["method"], req["path"]
        except Exception as e:
            logger.info("bad request from %s: %s", addr, e)
            try: write_response(sock, 400, [("content-type","text/plain")], "bad request")
            except Exception: pass
            return
        _dispatch(sock, req, router, on_event)
    finally:
        try: sock.close()
        except Exception: pass
        if access_log:
            logger.info("%s %s %s %.1fms", addr[0] if addr else "?",
                        method, path, (time.time()-start)*1000)


# ─── serve() ─────────────────────────────────────────────────────────

class _Shutdown:
    def __init__(s): s.flag = False
    def set(s): s.flag = True

def serve(router, host="127.0.0.1", port=8000,
          on_event=None, access_log=True, max_threads=256):
    """Run the HTTP server.

    Thread-per-connection. Each handler runs in a fault boundary.
    `on_event` is an optional callback fired by live() for observability:
      type ∈ {"page_render", "stream_open", "stream_close", "stream_error"}.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    logger.info("py_sse listening on http://%s:%d", host, port)

    shutdown = _Shutdown()
    sem = threading.BoundedSemaphore(max_threads)
    try:
        while not shutdown.flag:
            try:
                sock, addr = srv.accept()
            except OSError:
                break

            if not sem.acquire(blocking=False):
                # backpressure: too many in-flight; reject
                try:
                    write_response(sock, 503,
                                   [("content-type","text/plain")],
                                   "server busy")
                except Exception: pass
                try: sock.close()
                except Exception: pass
                continue

            def run(sock=sock, addr=addr):
                try:    _handle_connection(sock, addr, router, on_event, access_log)
                finally: sem.release()
            threading.Thread(target=run, daemon=True).start()
    finally:
        try: srv.close()
        except Exception: pass
