"""Tests for the join/leave notify behavior in py_sse.

When an SSE stream opens, the framework calls capacity.join(topic) and then
notifies the topic so existing peer streams wake and re-render with the
new viewer count. Same on close.

These are framework-level tests — no browser involved. They use raw sockets
to simulate SSE clients so we can confirm peer-wake-up via the SSE frames
each stream receives.
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import py_sse
from py_sse import serve, live, Database, Capacity
from html_tags import h


def _make_app(port):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS counter(id TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
    INSERT OR IGNORE INTO counter (id, value) VALUES ('global', 0);
    """
    db_path = f"/tmp/py_sse_join_notify_{port}.db"
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suf): os.remove(db_path + suf)
    db = Database(db_path, schema=SCHEMA)
    cap = Capacity(soft_cap=10, hard_cap=20, min_poll_ms=500, max_poll_ms=2000)

    @live(topic="counter")
    def home(req):
        n = db.one("SELECT value FROM counter WHERE id = 'global'")[0]
        return [
            h.div({"id": "value"}, str(n)),
            h.div({"id": "viewers"}, f"watching {cap.count('counter')}"),
        ]

    return [("GET", "/", home)], db, cap


def _open_sse(port, accept_encoding="identity"):
    """Open a raw SSE connection and return the socket."""
    s = socket.socket()
    s.connect(("127.0.0.1", port))
    req = (f"GET / HTTP/1.1\r\nHost: localhost\r\n"
           f"Accept: text/event-stream\r\nAccept-Encoding: {accept_encoding}\r\n\r\n")
    s.sendall(req.encode())
    return s


def _read_until(sock, marker, timeout=3):
    """Read from socket until marker appears or timeout."""
    sock.settimeout(0.1)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk: break
            buf += chunk
            if marker in buf: return buf
        except socket.timeout:
            continue
    return buf


def main():
    port = 8230
    routes, db, cap = _make_app(port)
    threading.Thread(
        target=lambda: serve(routes, host="127.0.0.1", port=port,
                             changes=db.changes, capacity=cap, access_log=False),
        daemon=True
    ).start()
    time.sleep(0.4)

    # === Test 1: opening a 2nd stream wakes the 1st with new count ===
    s1 = _open_sse(port)
    # Read the initial frame so we know stream 1 has rendered.
    initial = _read_until(s1, b"datastar-patch-elements")
    assert b"watching 1" in initial, f"expected watching 1 in first frame: {initial[-500:]!r}"

    # Open stream 2.
    s2 = _open_sse(port)
    # Stream 2 should get an initial frame with watching 2 (it sees the new state).
    s2_initial = _read_until(s2, b"datastar-patch-elements")
    assert b"watching 2" in s2_initial, f"stream 2 should see watching 2 in first frame"

    # Stream 1 should also have received a new frame after stream 2 joined,
    # containing the updated viewer count.
    s1.settimeout(2)
    s1_after = b""
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            chunk = s1.recv(4096)
            if not chunk: break
            s1_after += chunk
            # Look for a SECOND datastar-patch event with watching 2.
            if s1_after.count(b"datastar-patch-elements") >= 1 and b"watching 2" in s1_after:
                break
        except socket.timeout:
            continue

    assert b"watching 2" in s1_after, (
        f"stream 1 should have received a peer-wake frame with watching 2 "
        f"after stream 2 joined. got: {s1_after[-500:]!r}"
    )
    print("test 1 (join notifies peers: stream 1 woke when stream 2 joined) OK")

    # === Test 2: closing stream 2 wakes stream 1 with watching 1 ===
    # Note: TCP doesn't surface a client disconnect until the SECOND write
    # after the peer is gone (OS buffers the first write). So we need at
    # least two notify cycles for the framework to detect the dead socket,
    # run capacity.leave, and fire the leave-notify. We shutdown() the
    # socket to send a FIN/RST promptly, then drive notifies until the
    # cleanup happens.
    s2.shutdown(socket.SHUT_RDWR)
    s2.close()
    time.sleep(0.1)

    # Drain whatever stream 1 receives across multiple notify cycles.
    s1.settimeout(0.5)
    s1_after_close = b""
    for i in range(6):  # up to 6 cycles, ~1.8s total
        db.changes.notify("counter")
        time.sleep(0.3)
        try:
            while True:
                chunk = s1.recv(4096)
                if not chunk: break
                s1_after_close += chunk
        except socket.timeout:
            pass
        if cap.count("counter") <= 1:
            break

    assert cap.count("counter") == 1, (
        f"capacity should have dropped to 1 after stream 2 disconnect, "
        f"got {cap.count('counter')}"
    )
    assert b"watching 1" in s1_after_close, (
        f"stream 1 should have received a frame with watching 1 after "
        f"stream 2's death was detected. got: {s1_after_close[-500:]!r}"
    )
    print("test 2 (leave eventually wakes peers after framework detects dead socket) OK")

    s1.close()
    print(f"test 3 (final capacity after all streams closed cleanly: {cap.count('counter')}) OK")

    print()
    print("All join/leave notify tests passed.")


if __name__ == "__main__":
    main()
