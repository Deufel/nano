"""chi-style router.

Routes are registered on an explicit Router object via @r.get/.post/...
Path params use {name} syntax: "/users/{id}". Matching is exact —
no regex, no greedy wildcards. Sub-routers can be mounted at a prefix.
Middleware (`use`) wraps every handler in this router.

The router is a value, not a singleton. You can have several. You can
pass them around. `serve()` takes one.

A "handler" is `(req) -> Response`. Middleware is `(handler) -> handler`.
"""
import re
from functools import reduce

class Route:
    __slots__ = ("method","pattern","handler","_regex","_params")
    def __init__(s,method,pattern,handler):
        s.method,s.pattern,s.handler = method.upper(),pattern,handler
        s._params = re.findall(r"\{([^/}]+)\}", pattern)
        rx = re.sub(r"\{[^/}]+\}", r"([^/]+)", pattern)
        s._regex = re.compile("^" + rx + "$")

    def match(s,method,path):
        if method.upper() != s.method: return None
        m = s._regex.match(path)
        if not m: return None
        return dict(zip(s._params, m.groups()))

class Router:
    def __init__(s):
        s._routes = []
        s._middleware = []
        s._prefix = ""

    def use(s,mw):
        "Append middleware. Order is outer-to-inner (first added wraps last)."
        s._middleware.append(mw); return s

    def _add(s,method,pattern,handler):
        full = s._prefix + pattern
        wrapped = reduce(lambda h,m: m(h), reversed(s._middleware), handler)
        s._routes.append(Route(method, full, wrapped))
        return handler

    def get   (s,p): return lambda h: s._add("GET",   p, h)
    def post  (s,p): return lambda h: s._add("POST",  p, h)
    def put   (s,p): return lambda h: s._add("PUT",   p, h)
    def patch (s,p): return lambda h: s._add("PATCH", p, h)
    def delete(s,p): return lambda h: s._add("DELETE",p, h)
    def route (s,method,p): return lambda h: s._add(method, p, h)

    def mount(s,prefix,sub):
        "Mount another Router at prefix. Its routes get the prefix; its middleware composes."
        for rt in sub._routes:
            full = prefix + rt.pattern
            wrapped = reduce(lambda h,m: m(h), reversed(s._middleware), rt.handler)
            s._routes.append(Route(rt.method, full, wrapped))
        return s

    def resolve(s,method,path):
        "Return (handler, params) or (None, {})."
        for rt in s._routes:
            params = rt.match(method,path)
            if params is not None:
                return rt.handler, params
        return None, {}
