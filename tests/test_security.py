"""Verify that the html_tags-native rendering closes the XSS holes
from the prior string-concat implementation."""

import sys
sys.path.insert(0, "/home/claude/py_sse_v8")

from py_sse.server import _envelope_doc, _render_children, live
from py_sse import Capacity, Changes
from html_tags import h, Safe, Node


# 1. ui_theme can no longer break out of the html attribute.
malicious_theme = '"><script>alert(1)</script><x x="'
out = _envelope_doc([h.title("test")], h.div({"id": "live-root"}, "hi"),
                    ui_theme=malicious_theme)
assert "<script>alert(1)</script>" not in out, \
    f"ui_theme XSS leaked into output:\n{out[:500]}"
# It should appear escaped instead:
assert "&quot;" in out or "&lt;" in out
print("test 1 (ui_theme XSS escaped) OK")


# 2. data-init URL with a quote can no longer break out of the attribute.
# Simulate a request with a malicious path.
class FakeLive:
    def mode(self, t): return "live"
    def count(self, t): return 0
    def poll_interval_ms(self, t): return 1000
    def join(self, t):
        from contextlib import contextmanager
        @contextmanager
        def cm(): yield
        return cm()

handler = lambda req: h.span("hi")
wrapped = live(handler, topic="test")

req = {
    "_capacity": FakeLive(),
    "_changes": Changes(),
    "_head": [h.title("t")],
    "_ui_theme": "dark",
    "_on_event": None,
    "params": {},
    "headers": {},
    "path": "/'><script>alert(1)</script><x x='",
}
status, headers, body = wrapped(req)
assert "<script>alert(1)</script>" not in body, \
    f"path XSS leaked into data-init:\n{body[:500]}"
# The malicious path content should appear escaped:
assert "&lt;script&gt;" in body
print("test 2 (path XSS in data-init escaped) OK")


# 3. Handler returning a string raises a useful error.
try:
    _render_children("a raw string")
    raise AssertionError("should have raised TypeError")
except TypeError as e:
    assert "str" in str(e)
    assert "Safe" in str(e) or "Node" in str(e)
print("test 3 (TypeError on raw string with helpful message) OK")


# 4. Handler returning None raises a useful error.
try:
    _render_children(None)
    raise AssertionError("should have raised TypeError")
except TypeError as e:
    assert "NoneType" in str(e)
print("test 4 (TypeError on None with helpful message) OK")


# 5. Handler returning Node works.
out = _render_children(h.span("hello"))
assert out == "<span>hello</span>"
print("test 5 (Node rendered) OK")


# 6. Handler returning iterable of Node + Safe works.
out = _render_children([h.span("a"), Safe("<b>raw</b>"), h.i("c")])
assert out == "<span>a</span><b>raw</b><i>c</i>"
print("test 6 (mixed Node + Safe iterable rendered) OK")


# 7. Handler returning Safe works.
out = _render_children(Safe("<div>pre-rendered</div>"))
assert out == "<div>pre-rendered</div>"
print("test 7 (Safe pass-through) OK")


# 8. Handler with __node__ protocol works.
class Comp:
    def __node__(self):
        return h.article("from component")
out = _render_children(Comp())
assert "<article>from component</article>" in out
print("test 8 (__node__ protocol works) OK")


print()
print("All security/contract tests passed.")
