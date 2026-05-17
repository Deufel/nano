"""pytest fixtures for the e2e browser tests.

Each test gets a fresh py_sse server on its own port. This is the cleanest
way to avoid cross-test pollution from leftover SSE streams: when a test
ends and its browser context closes, the server thread is abandoned, but
since each test has its own server it can't affect later tests.

The cost is ~300ms per test for server startup. With 9 tests that's ~2.7s
of overhead — acceptable for a browser-level test suite.

Note: `base_url` is session-scoped because pytest-base-url's plugin
requires it. We don't use it — tests reference the function-scoped `url`
fixture instead. `base_url` returns a sentinel that would crash any test
using it directly, surfacing the mistake clearly.
"""
import os
import socket
import sys
import threading
import time

import pytest

# Make the fixture app importable from this directory.
sys.path.insert(0, os.path.dirname(__file__))

_PORT_COUNTER = [8210]


def _wait_for_port(port, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server did not come up on port {port}")


@pytest.fixture
def server(tmp_path):
    """Fresh py_sse server per test, on its own port and DB."""
    port = _PORT_COUNTER[0]
    _PORT_COUNTER[0] += 1

    db_path = str(tmp_path / "e2e.db")
    os.environ["E2E_DB"] = db_path

    # Reimport the app module fresh so its module-level Database / Capacity
    # rebind against the new DB path. Without this, the second test would
    # reuse the first test's db and capacity instance.
    if "app" in sys.modules:
        del sys.modules["app"]
    import app  # noqa
    import py_sse

    def run():
        py_sse.serve(app.ROUTES, host="127.0.0.1", port=port,
                     changes=app.db.changes, capacity=app.CAPACITY,
                     head=app.HEAD, access_log=False)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    _wait_for_port(port)

    class _Server: pass
    s = _Server()
    s.port,s.url,s.db,s.capacity,s.app = port, f"http://127.0.0.1:{port}", app.db, app.CAPACITY, app
    yield s


@pytest.fixture
def url(server):
    return server.url


@pytest.fixture
def capacity(server):
    return server.capacity


# pytest-base-url's plugin requires `base_url` at session scope. We don't
# use it — tests should use `url` — but defining it silences ScopeMismatch.
@pytest.fixture(scope="session")
def base_url():
    return "http://localhost:0"
