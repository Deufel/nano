# /// script
# requires-python = ">=3.13"
# dependencies = ["py-sse>=0.8.0","html-tags>=0.4.4"]
# ///
"""Multi-room chat — py_sse 0.8.0 demo.

Landing → signup/login → rooms list → room view. Sessions in a cookie,
passwords via stdlib scrypt. Anyone-can-join public rooms. Live updates
via SSE; room list also re-renders when any room gets a new message.

Run:
    uv run chat.py
"""
import os, time, hashlib, secrets
from urllib.parse import parse_qs, urlencode
from py_sse import serve, live, Database, Capacity, signals, html, redirect, set_cookie
from html_tags import h, Safe, render as h_render

STICK = "https://cdn.jsdelivr.net/gh/Deufel/toolbox@d32d8da/css/style.css"
DSTAR = "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"

SCHEMA = """
CREATE TABLE IF NOT EXISTS user(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS session(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES user(id), created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS room(name TEXT PRIMARY KEY, created_by INTEGER NOT NULL REFERENCES user(id), created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS msg(id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL REFERENCES room(name), user_id INTEGER NOT NULL REFERENCES user(id), text TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS msg_room_id ON msg(room, id DESC);
"""
db = Database("chat.db", schema=SCHEMA)

# Intentionally low caps for testing the live → poll → static degradation.
# With 2/4: viewers 1–2 get SSE, 3–4 get polling, 5+ get static.
# Open 3+ browser tabs to watch the page mode shift in real time.
CAPACITY = Capacity(soft_cap=2, hard_cap=4, min_poll_ms=1500, max_poll_ms=6000, ramp_users=4)

# ─── icons (Lucide MIT, inline so currentColor + --fg flow through) ─────
_S = 'xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
ICONS = {
 'messages': f'<svg {_S}><path d="M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v5Z"/><path d="M18 9h2a2 2 0 0 1 2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1"/></svg>',
 'users':    f'<svg {_S}><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
 'plus':     f'<svg {_S}><path d="M5 12h14"/><path d="M12 5v14"/></svg>',
 'login':    f'<svg {_S}><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" x2="3" y1="12" y2="12"/></svg>',
 'logout':   f'<svg {_S}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" x2="9" y1="12" y2="12"/></svg>',
 'send':     f'<svg {_S}><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>',
 'hash':     f'<svg {_S}><line x1="4" x2="20" y1="9" y2="9"/><line x1="4" x2="20" y1="15" y2="15"/><line x1="10" x2="8" y1="3" y2="21"/><line x1="16" x2="14" y1="3" y2="21"/></svg>',
 'sun':      f'<svg {_S}><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>',
 'moon':     f'<svg {_S}><path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/></svg>',
 'palette':  f'<svg {_S}><path d="M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25a1.75 1.75 0 0 0-1.4 2.8l.3.4a1.75 1.75 0 0 1-1.4 2.8z"/><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/></svg>',
 'type':     f'<svg {_S}><path d="m15 16 2.536-7.328a1 1 0 0 1 1.928 0L22 16"/><path d="M15.697 14h5.606"/><path d="m2 16 4.039-9.69a.5 .5 0 0 1 .923 0L11 16"/><path d="M3.304 13h6.392"/></svg>',
}
def icon(name): return Safe(ICONS[name])

# ─── auth ─────────────────────────────────────────────────────────────
def hash_pw(pw, salt):     return hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
def make_salt():           return secrets.token_hex(16)
def make_token():          return secrets.token_urlsafe(32)
def check_pw(pw, h, salt): return secrets.compare_digest(hash_pw(pw, salt), h)

def attach_user(req):
    """before_hook: look up session cookie, attach req['user'] if valid."""
    tok = req["cookies"].get("session")
    req["user"] = None
    if not tok: return
    row = db.one("SELECT u.id, u.name FROM session s JOIN user u ON u.id = s.user_id WHERE s.token = ?", (tok,))
    if row: req["user"] = {"id": row[0], "name": row[1]}

# ─── shared chrome ────────────────────────────────────────────────────
# Theme toggles cycle three signals: $theme ('dark'|'light'), $hue (0..360
# in 30° steps), $typeIdx (0..2 mapping to --type -0.25 / 0 / 0.25).
# A single data-effect propagates the signals to html attributes / style.
THEME_SIGNALS = '{"theme":"dark","hue":18,"typeIdx":1}'
THEME_EFFECT  = ("document.documentElement.dataset.uiTheme = $theme;"
                 "document.documentElement.style.setProperty('--hue', $hue);"
                 "document.documentElement.style.setProperty('--type', ['-0.25','0','0.25'][$typeIdx])")

def theme_buttons():
    return [
        h.button({"class": "icon-btn", "type": "button", "aria-label": "Toggle theme",
                  "data-on:click": "$theme = $theme === 'dark' ? 'light' : 'dark'"},
                 icon("moon")),
        h.button({"class": "icon-btn", "type": "button", "aria-label": "Cycle hue",
                  "data-on:click": "$hue = ($hue + 30) % 360"},
                 icon("palette")),
        h.button({"class": "icon-btn", "type": "button", "aria-label": "Cycle text size",
                  "data-on:click": "$typeIdx = ($typeIdx + 1) % 3"},
                 icon("type")),
    ]

def header(u):
    """pg-header. Brand left, theme toggles + auth controls right."""
    left = h.a({"href": ("/rooms" if u else "/"), "class": "row",
                "style": "gap:0.4lh; text-decoration:none; --type:1"},
               icon("messages"), h.span("chat"))
    auth = (h.a({"href": "/logout", "class": "row tag",
                 "style": "gap:0.3lh; text-decoration:none"},
                icon("logout"), "@" + u["name"])
            if u else
            [h.a({"href": "/login",  "class": "row tag",
                  "style": "gap:0.3lh; text-decoration:none"}, icon("login"), "login"),
             h.a({"href": "/signup", "class": "row tag suc",
                  "style": "gap:0.3lh; text-decoration:none"}, icon("plus"), "sign up")])
    right = h.div({"class": "row", "style": "gap:0.5lh; align-items:center",
                   "data-signals": THEME_SIGNALS, "data-effect": THEME_EFFECT},
                  *theme_buttons(),
                  *(auth if isinstance(auth, list) else [auth]))
    return h.div({"class": "pg-header flank-end"}, left, right)

def card(*kids, **attrs):
    attrs.setdefault("class", "card stage column")
    return h.div(attrs, *kids)

def footer(topic):
    """pg-footer with live status tags.

    'watching' shows current SSE viewers of `topic` from the Capacity
    instance. Note: viewers only increment on stream open; the count
    appears in re-renders triggered by db.changes.notify(), so a join
    by itself won't push the new number. Open caps low for testing
    (CAPACITY.soft_cap=2) so this is easy to observe.
    """
    n_users    = db.one("SELECT COUNT(*) FROM user")[0]
    n_rooms    = db.one("SELECT COUNT(*) FROM room")[0]
    n_msgs     = db.one("SELECT COUNT(*) FROM msg")[0]
    n_watching = CAPACITY.count(topic) if topic else 0
    mode       = CAPACITY.mode(topic)  if topic else "static"
    mode_tag   = {"live": "suc", "poll": "wrn", "static": "dgr"}[mode]
    items = [
        h.span({"class": f"tag {mode_tag}", "style": "--type:-2"}, mode),
        h.span({"class": "tag inf", "style": "--type:-2"}, f"watching {n_watching}"),
        h.span({"class": "tag", "style": "--type:-2"}, f"users {n_users}"),
        h.span({"class": "tag", "style": "--type:-2"}, f"rooms {n_rooms}"),
        h.span({"class": "tag", "style": "--type:-2"}, f"msgs {n_msgs}"),
    ]
    return h.div({"class": "pg-footer flank-end",
                  "style": "--type:-1; --fg:-0.5; padding-block:0.4lh"},
        h.span("py_sse 0.8 chat demo"),
        h.div({"class": "row", "style": "gap:0.3lh"}, *items))

# ─── envelope helper for non-live routes ──────────────────────────────
def _page(*body):
    """Wrap body fragments in a full HTML response (non-live routes)."""
    page = h.html({"id": "page", "data-ui-theme": "dark"},
        h.head(h.meta(charset="utf-8"),
               h.meta(name="viewport", content="width=device-width, initial-scale=1"),
               h.title("chat"),
               h.link(rel="stylesheet", href=STICK),
               h.script(type="module", src=DSTAR)),
        h.body({"class": "page stage"}, *body))
    return html("<!doctype html>" + h_render(page))

# ─── landing + auth (plain, not live) ─────────────────────────────────
def landing(req):
    if req["user"]: return redirect("/rooms")
    return _page(
        header(None),
        h.main({"class": "pg-main column",
                "style": "max-inline-size:36rem; margin:3lh auto; gap:1.5lh; align-items:center"},
            h.h1({"style": "--type:3; text-align:center"}, "real-time chat, in a tiny framework"),
            h.p({"style": "--fg:-0.5; text-align:center; max-inline-size:32rem"},
                "Sign up, pick a room, talk to whoever's there. ",
                "Built on py_sse + Datastar — every message arrives via SSE fat morph."),
            h.div({"class": "row", "style": "gap:1lh"},
                h.a({"href": "/signup", "class": "btn row",
                     "style": "gap:0.4lh; --bg:var(--cfg-bg-loud); --fg:-1; border-color:transparent; text-decoration:none"},
                    icon("plus"), "create account"),
                h.a({"href": "/login", "class": "btn row",
                     "style": "gap:0.4lh; text-decoration:none"},
                    icon("login"), "sign in"))),
        footer(None))

def signup_form(req):
    if req["user"]: return redirect("/rooms")
    return _page(header(None),
                 _auth_card("create account", "/signup", "sign up",
                            req["query"].get("err"), icon("plus")),
                 footer(None))

def login_form(req):
    if req["user"]: return redirect("/rooms")
    return _page(header(None),
                 _auth_card("sign in", "/login", "log in",
                            req["query"].get("err"), icon("login")),
                 footer(None))

def _auth_card(title, action, button_label, err, btn_icon):
    fields = [
        h.div(h.label("username"),
              h.input({"class": "input", "name": "name", "required": True, "autofocus": True,
                       "minlength": 3, "maxlength": 32, "pattern": "[a-zA-Z0-9_]+"})),
        h.div(h.label("password"),
              h.input({"class": "input", "name": "password", "type": "password",
                       "required": True, "minlength": 8})),
    ]
    if err: fields.append(h.p({"style": "--fg:-1; color:var(--dgr); --type:-1"}, err))
    fields.append(h.button({"type": "submit", "class": "btn row",
                            "style": "gap:0.4lh; justify-content:center; --bg:var(--cfg-bg-loud); --fg:-1; border-color:transparent"},
                           btn_icon, button_label))
    return h.main({"class": "pg-main column",
                   "style": "max-inline-size:26rem; margin:3lh auto"},
        card(h.h2({"style": "--type:1"}, title),
             h.form({"method": "post", "action": action}, *fields)))

def _err_redirect(path, msg): return redirect(f"{path}?{urlencode({'err': msg})}")

def do_signup(req):
    f = parse_qs(req["body"].decode("utf-8"))
    name, pw = (f.get("name") or [""])[0].strip(), (f.get("password") or [""])[0]
    if len(name) < 3:                                      return _err_redirect("/signup", "username too short")
    if not name.replace("_","").isalnum():                 return _err_redirect("/signup", "username must be alphanumeric or _")
    if len(pw) < 8:                                        return _err_redirect("/signup", "password must be ≥ 8 chars")
    if db.one("SELECT 1 FROM user WHERE name = ?",(name,)):return _err_redirect("/signup", "that username is taken")
    salt = make_salt()
    db.execute("INSERT INTO user (name, pw_hash, pw_salt, created_at) VALUES (?, ?, ?, ?)",
               (name, hash_pw(pw, salt), salt, int(time.time())))
    return _login_and_redirect(req, name)

def do_login(req):
    f = parse_qs(req["body"].decode("utf-8"))
    name, pw = (f.get("name") or [""])[0].strip(), (f.get("password") or [""])[0]
    row = db.one("SELECT id, pw_hash, pw_salt FROM user WHERE name = ?", (name,))
    if not row or not check_pw(pw, row[1], row[2]):
        return _err_redirect("/login", "wrong username or password")
    return _login_and_redirect(req, name)

def _login_and_redirect(req, name):
    uid = db.one("SELECT id FROM user WHERE name = ?", (name,))[0]
    tok = make_token()
    db.execute("INSERT INTO session (token, user_id, created_at) VALUES (?, ?, ?)",
               (tok, uid, int(time.time())))
    set_cookie(req, "session", tok, path="/", httponly=True, samesite="Lax", max_age=60*60*24*30)
    return redirect("/rooms")

def do_logout(req):
    tok = req["cookies"].get("session")
    if tok: db.execute("DELETE FROM session WHERE token = ?", (tok,))
    set_cookie(req, "session", "", path="/", httponly=True, samesite="Lax", max_age=0)
    return redirect("/")

# ─── rooms list (live) ────────────────────────────────────────────────
@live(topic="rooms")
def rooms_list(req):
    if not req["user"]: return _gated()
    u = req["user"]
    rooms = db.all("""
        SELECT r.name, COUNT(m.id), MAX(m.created_at)
        FROM room r LEFT JOIN msg m ON m.room = r.name
        GROUP BY r.name ORDER BY MAX(m.created_at) DESC NULLS LAST, r.name""")
    return [
        header(u),
        h.main({"class": "pg-main column",
                "style": "max-inline-size:36rem; margin:1lh auto; gap:1lh"},
            card({"data-signals": '{"new_room":""}'},
                h.h2({"class": "row", "style": "gap:0.4lh; --type:1"},
                     icon("plus"), "new room"),
                h.div({"class": "row", "style": "gap:0.5lh"},
                    h.input({"class": "input", "data-bind:new_room": "",
                             "placeholder": "room name (alphanumeric, _ or -)",
                             "style": "flex:1", "maxlength": 32,
                             "data-on:keydown": "evt.key === 'Enter' && ($new_room && (@post('/rooms'), $new_room=''))"}),
                    h.button({"type": "button", "class": "btn",
                              "style": "--bg:var(--cfg-bg-loud); --fg:-1; border-color:transparent",
                              "data-on:click": "$new_room && (@post('/rooms'), $new_room='')"},
                             "create"))),
            card(
                h.h2({"class": "row", "style": "gap:0.4lh; --type:1"},
                     icon("users"), f"rooms ({len(rooms)})"),
                (h.p({"style": "--fg:-0.5"}, "no rooms yet — create one above") if not rooms else
                 h.div({"class": "column", "style": "gap:0.3lh"},
                       *[_room_link(name, c, last) for name, c, last in rooms])))),
        footer("rooms")]

def _room_link(name, msg_count, last):
    when = _ts(last) if last else "no messages"
    return h.a({"href": f"/r/{name}", "class": "row spread",
                "style": "text-decoration:none; gap:0.5lh; padding:0.4lh 0.6lh; border:1px solid var(--border); border-radius:0.3lh"},
        h.span({"class": "row", "style": "gap:0.3lh; align-items:center"},
               icon("hash"), name),
        h.span({"style": "--type:-1; --fg:-0.5"}, f"{msg_count} msgs · {when}"))

def create_room(req):
    if not req["user"]: return redirect("/login")
    name = (signals(req).get("new_room") or "").strip().lower()
    if not name or not name.replace("_","").replace("-","").isalnum(): return (200, [], b"")
    if db.one("SELECT 1 FROM room WHERE name = ?", (name,)):           return (200, [], b"")
    db.execute("INSERT INTO room (name, created_by, created_at) VALUES (?, ?, ?)",
               (name, req["user"]["id"], int(time.time())))
    db.changes.notify("rooms")
    return (200, [], b"")

# ─── one room (live) ──────────────────────────────────────────────────
@live(topic="room.{name}")
def room_view(req):
    if not req["user"]: return _gated()
    u, name = req["user"], req["params"]["name"]
    if not db.one("SELECT 1 FROM room WHERE name = ?", (name,)):
        return [header(u),
                h.main({"class": "pg-main column",
                        "style": "max-inline-size:32rem; margin:2lh auto"},
                    card(h.p({"style": "--fg:-0.5"}, f"room '{name}' doesn't exist"),
                         h.a({"href": "/rooms", "class": "link"}, "← back to rooms"))),
                footer(None)]
    # Newest-first for column-reverse rendering: visually newest sits at bottom.
    msgs = db.all("""
        SELECT m.id, u.name, m.text, m.created_at FROM msg m JOIN user u ON u.id = m.user_id
        WHERE m.room = ? ORDER BY m.id DESC LIMIT 200""", (name,))
    return [
        header(u),
        h.main({"class": "pg-main column",
                "style": "max-inline-size:36rem; margin:1lh auto; gap:0.7lh"},
            h.div({"class": "row spread"},
                h.h2({"class": "row", "style": "gap:0.4lh; --type:1"},
                     icon("hash"), name),
                h.a({"href": "/rooms", "class": "link", "style": "--fg:-0.5"},
                    "← rooms")),
            h.div({"class": "card stage",
                   "style": "min-block-size:60vh; max-block-size:70vh; overflow-y:auto; "
                            "display:flex; flex-direction:column-reverse; gap:0.4lh; padding:0.7lh"},
                  (h.p({"style": "--fg:-0.5; margin:auto"}, "say something to start the conversation") if not msgs else
                   h.div({"class": "column",
                          "style": "display:flex; flex-direction:column-reverse; gap:0.5lh"},
                         *[_msg(author, text, ts, author == u["name"]) for _, author, text, ts in msgs]))),
            card({"data-signals": '{"text":""}'},
                h.div({"class": "row", "style": "gap:0.5lh"},
                    h.input({"class": "input", "data-bind:text": "",
                             "placeholder": "type a message…", "autofocus": True,
                             "style": "flex:1", "maxlength": 1000,
                             "data-on:keydown": f"evt.key === 'Enter' && ($text && (@post('/r/{name}/post'), $text=''))"}),
                    h.button({"type": "button", "class": "btn row",
                              "style": "gap:0.3lh; --bg:var(--cfg-bg-loud); --fg:-1; border-color:transparent",
                              "data-on:click": f"$text && (@post('/r/{name}/post'), $text='')"},
                             icon("send"), "send")))),
        footer(f"room.{name}")]

def _msg(author, text, ts, is_me):
    """One message. Own messages right-aligned with --hue-shift for separation."""
    align = "flex-end" if is_me else "flex-start"
    bg    = "0.55"     if is_me else "0.15"
    shift = "120"      if is_me else "0"
    return h.div({"class": "column",
                  "style": f"align-self:{align}; max-inline-size:75%; gap:0.1lh; --hue-shift:{shift}"},
        h.div({"class": "row", "style": "gap:0.4lh; --type:-1; --fg:-0.5"},
              h.span("@" + author), h.span(_ts(ts))),
        h.div({"style": f"--bg:{bg}; padding:0.4lh 0.7lh; border-radius:0.4lh"}, text))

def post_msg(req):
    if not req["user"]: return redirect("/login")
    name = req["params"]["name"]
    if not db.one("SELECT 1 FROM room WHERE name = ?", (name,)): return (404, [], b"no such room")
    text = (signals(req).get("text") or "").strip()
    if not text: return (200, [], b"")
    db.execute("INSERT INTO msg (room, user_id, text, created_at) VALUES (?, ?, ?, ?)",
               (name, req["user"]["id"], text[:1000], int(time.time())))
    db.changes.notify(f"room.{name}")
    db.changes.notify("rooms")  # so the rooms list updates last-activity
    return (200, [], b"")

# ─── helpers ──────────────────────────────────────────────────────────
def _gated():
    return [header(None),
            h.main({"class": "pg-main column",
                    "style": "max-inline-size:26rem; margin:3lh auto"},
                card(h.p({"style": "--fg:-0.5"}, "you need to be signed in to see this"),
                     h.a({"href": "/login", "class": "btn row",
                          "style": "gap:0.4lh; text-decoration:none; justify-content:center"},
                         icon("login"), "sign in"))),
            footer(None)]

def _ts(ts):
    """Fixed timestamp, never goes stale. Returns 'Nov 15 @ 7:42 PM'."""
    if ts is None: return ""
    t = time.localtime(int(ts))
    hour = time.strftime("%I", t).lstrip("0") or "12"
    return f"{time.strftime('%b %d', t)} @ {hour}{time.strftime(':%M %p', t)}"

# ─── routes + serve ───────────────────────────────────────────────────
ROUTES = [
    ("GET",  "/",              landing),
    ("GET",  "/signup",        signup_form),
    ("POST", "/signup",        do_signup),
    ("GET",  "/login",         login_form),
    ("POST", "/login",         do_login),
    ("GET",  "/logout",        do_logout),
    ("GET",  "/rooms",         rooms_list),
    ("POST", "/rooms",         create_room),
    ("GET",  "/r/{name}",      room_view),
    ("POST", "/r/{name}/post", post_msg),
]

HEAD = [
    h.title("chat"),
    h.link(rel="stylesheet", href=STICK),
    h.script(type="module", src=DSTAR),
]

if __name__ == "__main__":
    serve(ROUTES,
          host=os.environ.get("HOST", "0.0.0.0"),
          port=int(os.environ.get("PORT", "8000")),
          changes=db.changes, capacity=CAPACITY,
          head=HEAD, before_hooks=[attach_user])
