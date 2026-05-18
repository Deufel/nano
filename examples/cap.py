# /// script
# requires-python = ">=3.13"
# dependencies = ["py-sse","html-tags>=0.4.4"]
#
# [tool.uv.sources]
# py-sse = { path = "../", editable = true }
# ///
"""Events demo — py_sse 0.10.

Model:
  - The database is the source of truth.
  - Each page is a reflection of database state.
  - Writes mutate the DB and call notify(topic).
  - live() regions subscribed to that topic re-render automatically.
  - Navigation is for going somewhere else, not for showing write results.

Pages (real URLs, real navigation via <a href>):
  GET  /              — landing page. Public events listing. Anonymous OK.
  GET  /home          — authenticated home. Events filtered by user.
  GET  /login         — pick a user / create a new one.
  GET  /events/new    — create event form.

Writes (Datastar @post via signals — return no_content unless auth changes):
  POST /users               → insert user, notify('users')
  POST /events              → insert event, notify('events'), ds_redirect /home
  POST /events/{id}/delete  → delete event, notify('events')
  POST /login               → set cookie, ds_redirect /home
  POST /logout              → clear cookie, ds_redirect /

Live regions:
  /          — public events list (subscribed to 'events')
  /home      — user's events list (subscribed to 'events')
  /login     — users list (subscribed to 'users')

Three topics: 'events', 'users', and each page just listens to what it needs.
"""
import os
import time

from py_sse import (Router, Capacity, Database, live, page,
                    no_content, redirect, ds_redirect, signals, serve)
from html_tags import h, Safe


# ─── State ──────────────────────────────────────────────────────────

db = Database("/tmp/events.db", schema="""
    CREATE TABLE IF NOT EXISTS user (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        name  TEXT NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS event (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id   INTEGER NOT NULL REFERENCES user(id),
        title      TEXT NOT NULL,
        when_at    TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK (visibility IN ('public','private'))
    );
""")

if not db.one("SELECT COUNT(*) FROM user")[0]:
    for name in ("alice", "bob", "carol"):
        db.execute("INSERT INTO user (name) VALUES (?)", (name,))
    db.execute("""INSERT INTO event (owner_id, title, when_at, visibility) VALUES
        (1, 'Standup',            '2026-06-01 09:00', 'public'),
        (1, 'Therapy',            '2026-06-01 16:00', 'private'),
        (2, 'Code review',        '2026-06-01 14:00', 'public'),
        (3, 'Coffee with Alice',  '2026-06-02 10:00', 'public'),
        (3, 'Performance review', '2026-06-03 15:00', 'private')""")

CAP = Capacity(soft_cap=8, hard_cap=64, min_poll_ms=2000, max_poll_ms=10000)


# ─── Auth helpers ───────────────────────────────────────────────────

def current_user(req):
    uid = req.get("cookies", {}).get("uid")
    if not uid: return None
    row = db.one("SELECT id, name FROM user WHERE id = ?", (int(uid),))
    return {"id": row[0], "name": row[1]} if row else None

def set_uid_cookie(req, uid):
    req.setdefault("_cookies_out", []).append(
        f"uid={uid}; Path=/; HttpOnly; SameSite=Lax")

def clear_uid_cookie(req):
    req.setdefault("_cookies_out", []).append(
        "uid=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")


# ─── Pure queries (DB → data) ───────────────────────────────────────

def public_events():
    rows = db.all("""SELECT e.id, e.title, e.when_at, u.name
                     FROM event e JOIN user u ON u.id = e.owner_id
                     WHERE e.visibility = 'public'
                     ORDER BY e.when_at""")
    return [dict(id=r[0], title=r[1], when_at=r[2], owner=r[3]) for r in rows]

def events_for_user(user_id):
    rows = db.all("""SELECT e.id, e.title, e.when_at, e.visibility,
                            u.name, u.id
                     FROM event e JOIN user u ON u.id = e.owner_id
                     WHERE e.visibility = 'public' OR e.owner_id = ?
                     ORDER BY e.when_at""", (user_id,))
    return [dict(id=r[0], title=r[1], when_at=r[2], visibility=r[3],
                 owner=r[4], owner_id=r[5]) for r in rows]

def all_users():
    return [{"id": r[0], "name": r[1]}
            for r in db.all("SELECT id, name FROM user ORDER BY name")]


# ─── Styling ────────────────────────────────────────────────────────

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
    font: 14px/1.5 ui-sans-serif, system-ui, sans-serif;
    background: #0a0a0a; color: #e5e5e5;
    margin: 0; padding: 2rem;
}
.container { max-width: 40rem; margin: 0 auto; }
a { color: #6aa6ff; text-decoration: none; }
a:hover { text-decoration: underline; }

header {
    display: flex; justify-content: space-between; align-items: baseline;
    padding-bottom: 1rem; margin-bottom: 1.5rem;
    border-bottom: 1px solid #2a2a2a;
}
h1 { margin: 0; font-size: 1rem; font-weight: 600;
     letter-spacing: 0.1em; text-transform: uppercase; color: #888; }
h2 { font-size: 0.85rem; font-weight: 600;
     letter-spacing: 0.1em; text-transform: uppercase; color: #888;
     margin: 2rem 0 0.75rem; }
.who { font: 13px/1 ui-monospace, monospace; color: #999;
       display: flex; gap: 0.75rem; align-items: baseline; }
.who button {
    background: none; border: none; color: #f59e0b; cursor: pointer;
    padding: 0; font: inherit; text-decoration: underline;
}

.events { list-style: none; padding: 0; margin: 0; }
.event {
    display: grid; grid-template-columns: 1fr auto;
    gap: 0.25rem 1rem;
    padding: 0.75rem 0; border-bottom: 1px solid #1a1a1a;
}
.event .title { color: #e5e5e5; font-weight: 600; }
.event .meta { font: 12px ui-monospace, monospace; color: #777; }
.event .actions { grid-column: 2; grid-row: 1 / 3; align-self: center; }
.event button.delete {
    background: transparent; border: 1px solid #333; color: #999;
    padding: 0.25rem 0.5rem; border-radius: 0.25rem;
    cursor: pointer; font: 11px ui-monospace, monospace;
}
.event button.delete:hover {
    background: #ef444422; color: #ef4444; border-color: #ef4444;
}
.event.private .title::after {
    content: "private"; margin-left: 0.5rem;
    padding: 0.05rem 0.4rem; border-radius: 0.25rem;
    font: 10px ui-monospace, monospace; letter-spacing: 0.05em;
    text-transform: uppercase; color: #f59e0b; background: #f59e0b18;
}
.empty { color: #555; padding: 2rem 0; text-align: center; }

.toolbar { margin-top: 1.5rem; }
.toolbar a.btn {
    display: inline-block;
    background: #161616; border: 1px solid #333; border-radius: 0.25rem;
    padding: 0.5rem 1rem; color: #e5e5e5; font-weight: 600;
}
.toolbar a.btn:hover { background: #1f1f1f; border-color: #6aa6ff;
                      text-decoration: none; }

.form { display: grid; gap: 0.75rem; margin-top: 1rem; }
.form label { display: grid; gap: 0.25rem;
              font: 12px ui-monospace, monospace; color: #888; }
.form input, .form select {
    background: #161616; color: #e5e5e5;
    border: 1px solid #333; border-radius: 0.25rem;
    padding: 0.5rem 0.75rem; font: inherit;
}
.form input:focus, .form select:focus {
    outline: none; border-color: #6aa6ff;
}
.form .actions { display: flex; gap: 0.5rem; margin-top: 0.5rem; }
.form button.primary {
    background: #6aa6ff; color: #0a0a0a;
    border: 1px solid #6aa6ff; border-radius: 0.25rem;
    padding: 0.5rem 1.25rem; font-weight: 600; cursor: pointer;
}
.form button.primary:hover { background: #5a96ee; }

.users-list { list-style: none; padding: 0; margin: 0;
              display: grid; gap: 0.5rem; }
.users-list li button.user {
    width: 100%; text-align: left;
    background: #161616; color: #e5e5e5;
    border: 1px solid #333; border-radius: 0.25rem;
    padding: 0.75rem 1rem; cursor: pointer; font: inherit;
}
.users-list li button.user:hover { background: #1f1f1f; border-color: #6aa6ff; }
"""

HEAD = [
    h.title("Events"),
    h.style(Safe(CSS)),
    h.script({"type": "module",
              "src": "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"}),
]


def header_for(user, *, label):
    if user:
        right = [
            h.span("signed in as "),
            h.strong(user["name"]),
            h.button({"data-on:click": "@post('/logout')"}, "sign out"),
        ]
    else:
        right = [h.a({"href": "/login"}, "sign in")]
    return h.header(h.h1(label), h.div({"class": "who"}, *right))


# ─── Routes ─────────────────────────────────────────────────────────

r = Router()


# Landing page — public events. Available to anyone.

@r.get("/")
def index(req):
    user = current_user(req)
    toolbar = []
    if user:
        toolbar.append(h.a({"href": "/home", "class": "btn"}, "your home →"))
    else:
        toolbar.append(h.a({"href": "/login", "class": "btn"}, "sign in"))
    return page(head=HEAD, body=[
        h.div({"class": "container"},
            header_for(user, label="events · public"),
            h.div({"id": "events-public",
                   "data-init": "@get('/stream/public')"},
                h.div({"class": "empty"}, "loading…")),
            h.div({"class": "toolbar"}, *toolbar))
    ])


@r.get("/stream/public")
def stream_public(req):
    def render(ctx):
        evs = public_events()
        if not evs:
            return h.div({"id": "events-public"},
                h.div({"class": "empty"}, "no public events"))
        return h.div({"id": "events-public"},
            h.ul({"class": "events"},
                *[_render_event_readonly(ev) for ev in evs]))
    return live(topic="events", render=render,
                changes=db.changes, capacity=CAP)


def _render_event_readonly(ev):
    return h.li({"class": "event"},
        h.div({"class": "title"}, ev["title"]),
        h.div({"class": "meta"}, ev["when_at"], "  ·  ", h.em(ev["owner"])))


# Authenticated home — user's events.

@r.get("/home")
def home(req):
    user = current_user(req)
    if not user:
        return redirect("/login")
    return page(head=HEAD, body=[
        h.div({"class": "container"},
            header_for(user, label="your events"),
            h.div({"id": "events-home",
                   "data-init": "@get('/stream/home')"},
                h.div({"class": "empty"}, "loading…")),
            h.div({"class": "toolbar"},
                h.a({"href": "/events/new", "class": "btn"}, "+ new event"),
                " ",
                h.a({"href": "/"}, "public listing")))
    ])


@r.get("/stream/home")
def stream_home(req):
    user = current_user(req)  # captured by the closure
    if not user:
        # If they sign out mid-stream, send static empty content.
        def render(ctx):
            return h.div({"id": "events-home"},
                h.div({"class": "empty"}, "signed out"))
        return live(topic="events", render=render,
                    changes=db.changes, capacity=CAP)

    def render(ctx):
        evs = events_for_user(user["id"])
        if not evs:
            return h.div({"id": "events-home"},
                h.div({"class": "empty"}, "no events yet"))
        return h.div({"id": "events-home"},
            h.ul({"class": "events"},
                *[_render_event_owned(ev, user) for ev in evs]))
    return live(topic="events", render=render,
                changes=db.changes, capacity=CAP)


def _render_event_owned(ev, user):
    classes = "event" + (" private" if ev["visibility"] == "private" else "")
    children = [
        h.div({"class": "title"}, ev["title"]),
        h.div({"class": "meta"}, ev["when_at"], "  ·  ", h.em(ev["owner"])),
    ]
    if user["id"] == ev["owner_id"]:
        children.append(
            h.div({"class": "actions"},
                h.button({"class": "delete",
                          "data-on:click": f"@post('/events/{ev['id']}/delete')"},
                         "×")))
    return h.li({"class": classes}, *children)


# Login page — users list (live, so new users appear) + register form.

@r.get("/login")
def login_page(req):
    return page(head=HEAD, body=[
        h.div({"class": "container"},
            h.header(
                h.h1("sign in"),
                h.div({"class": "who"}, h.a({"href": "/"}, "← back"))),
            h.p({"style": "color:#888"},
                "Pick a user. No password — this is a demo."),
            h.div({"id": "users-list",
                   "data-init": "@get('/stream/users')"},
                h.div({"class": "empty"}, "loading…")),
            h.h2("or register"),
            h.div({"class": "form"},
                h.label("name",
                    h.input({"data-bind:newName": "",
                             "placeholder": "new user name",
                             "autofocus": True})),
                h.div({"class": "actions"},
                    h.button({"class": "primary",
                              "data-on:click":
                                  "@post('/users'); $newName=''"},
                             "create"))))
    ])


@r.get("/stream/users")
def stream_users(req):
    def render(ctx):
        users = all_users()
        if not users:
            return h.div({"id": "users-list"},
                h.div({"class": "empty"}, "no users yet"))
        return h.div({"id": "users-list"},
            h.ul({"class": "users-list"},
                *[h.li(
                    h.button({"class": "user",
                              "data-on:click":
                                  f"$uid={u['id']}; @post('/login')"},
                             u["name"]))
                  for u in users]))
    return live(topic="users", render=render,
                changes=db.changes, capacity=CAP)


# ─── Writes ─────────────────────────────────────────────────────────

@r.post("/users")
def users_create(req):
    name = (signals(req).get("newName") or "").strip()
    if name:
        try:
            db.execute("INSERT INTO user (name) VALUES (?)", (name,))
            db.changes.notify("users")
        except Exception:
            pass  # duplicate name — silent for demo
    return no_content()


@r.post("/login")
def login(req):
    uid = signals(req).get("uid")
    if uid is not None and db.one("SELECT 1 FROM user WHERE id = ?", (int(uid),)):
        set_uid_cookie(req, str(uid))
        return ds_redirect("/home")
    return ds_redirect("/login")


@r.post("/logout")
def logout(req):
    clear_uid_cookie(req)
    return ds_redirect("/")


@r.get("/events/new")
def event_new_page(req):
    user = current_user(req)
    if not user:
        return redirect("/login")
    return page(head=HEAD, body=[
        h.div({"class": "container"},
            header_for(user, label="new event"),
            h.div({"class": "form"},
                h.label("title",
                    h.input({"data-bind:title": "", "autofocus": True})),
                h.label("when",
                    h.input({"data-bind:when": "",
                             "placeholder": "2026-06-01 09:00"})),
                h.label("visibility",
                    h.select({"data-bind:visibility": ""},
                        h.option({"value": "public"}, "public"),
                        h.option({"value": "private"}, "private"))),
                h.div({"class": "actions"},
                    h.button({"class": "primary",
                              "data-on:click": "@post('/events')"},
                             "create event"),
                    h.a({"href": "/home"}, "cancel"))))
    ])


@r.post("/events")
def event_create(req):
    user = current_user(req)
    if not user:
        return ds_redirect("/login")

    s = signals(req)
    title = (s.get("title") or "").strip()
    when  = (s.get("when")  or "").strip()
    vis   = s.get("visibility") or "public"
    if vis not in ("public", "private"): vis = "public"

    if title and when:
        db.execute("""INSERT INTO event (owner_id, title, when_at, visibility)
                      VALUES (?, ?, ?, ?)""",
                   (user["id"], title, when, vis))
        db.changes.notify("events")
        return ds_redirect("/home")
    # Empty inputs — stay on the form.
    return no_content()


@r.post("/events/{eid}/delete")
def event_delete(req):
    eid = req["params"]["eid"]
    user = current_user(req)
    if not user:
        return no_content()
    row = db.one("SELECT owner_id FROM event WHERE id = ?", (int(eid),))
    if row and row[0] == user["id"]:
        db.execute("DELETE FROM event WHERE id = ?", (int(eid),))
        db.changes.notify("events")
    return no_content()


# ─── Run ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Events demo — py_sse 0.10")
    print("Open http://127.0.0.1:8000")
    print("Pre-seeded users: alice, bob, carol")
    print("=" * 60)
    serve(r,
          host=os.environ.get("HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "8000")))
