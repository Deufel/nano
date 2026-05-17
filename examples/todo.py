# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "py-sse>=0.8.0",
#   "html-tags>=0.4.4",
# ]
# ///
"""
Tiny todo demo for py_sse 0.8.0.

Run:
    uv run todo.py

Open http://localhost:8000. Open a second tab. Edits in either tab
appear in the other instantly via the SSE read stream.

Architecture (CQRS, pure Datastar, new live() API):

    - The page is one route, one handler. Returns html_tags elements.
    - @live("todo") wraps the handler with SSE/poll/static degradation.
    - The framework owns the <html><head><body> envelope. Head fragments
      passed to serve() are the same on every render — idiomorph leaves
      head alone, scripts stay alive across morphs.
    - One URL serves all three transport modes. Initial GET returns full
      HTML with data-init that opens the SSE stream. Subsequent SSE
      requests (Accept: text/event-stream) stream the same content.
    - All writes are short-lived @post() actions. Signals flow up; the
      read stream brings the new state back down.
"""

import os
import time

from py_sse import (
    serve, live, html, Database, signals,
)
from html_tags import h


STICK = "https://cdn.jsdelivr.net/gh/Deufel/toolbox@d32d8da/css/style.css"
DATASTAR = "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"

SCHEMA = """
CREATE TABLE IF NOT EXISTS todo (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
"""

db = Database("todo.db", schema=SCHEMA)


# ─── data access ──────────────────────────────────────────────────────

def list_todos():
    return db.all("SELECT id, text, done FROM todo ORDER BY id DESC")

def add_todo(text):
    db.execute("INSERT INTO todo (text, created_at) VALUES (?, ?)",
               (text, int(time.time())))
    db.changes.notify("todo")

def toggle_todo(todo_id):
    db.execute("UPDATE todo SET done = 1 - done WHERE id = ?", (todo_id,))
    db.changes.notify("todo")

def delete_todo(todo_id):
    db.execute("DELETE FROM todo WHERE id = ?", (todo_id,))
    db.changes.notify("todo")


# ─── components ───────────────────────────────────────────────────────

def todo_row(todo_id, text, done):
    return h.li({"class": "row spread",
                 "style": "padding: 0.4lh 0; gap: 0.5lh; align-items: center"},
        h.button({
            "type": "button",
            "class": "tag suc" if done else "tag",
            "style": "min-inline-size: 2em; cursor: pointer",
            "aria-label": "toggle done",
            "data-on:click": f"@post('/todos/{todo_id}/toggle')",
        }, "✓" if done else " "),
        h.span({"style": f"flex: 1; "
                         f"{'opacity: 0.5; text-decoration: line-through' if done else ''}"},
               text),
        h.button({
            "type": "button",
            "class": "tag dgr",
            "style": "cursor: pointer",
            "aria-label": "delete",
            "data-on:click": f"@post('/todos/{todo_id}/delete')",
        }, "×"))


# ─── routes ───────────────────────────────────────────────────────────
#
# One @live page. The handler just returns html_tags elements — the
# framework wraps them in the standard envelope and decides how to
# serve based on viewer count.

@live(topic="todo")
def home(req):
    todos = list_todos()
    open_count = sum(1 for _, _, d in todos if not d)
    submit = "$text && (@post('/todos'), $text = '')"

    return [
        h.header({"class": "pg-header spread"},
            h.strong({"style": "--type: 1"}, "✓ todos"),
            h.span({"style": "--type: -2; --fg: -0.5"},
                   f"{open_count} open · py_sse 0.7.0")),
        h.main({"class": "pg-main column",
                "style": "max-inline-size: 32rem; margin: 1lh auto; gap: 1lh"},
            h.div({"class": "card stage column",
                   "data-signals": '{"text": ""}'},
                h.h2({"style": "--type: 1"}, "add"),
                h.div({"class": "row", "style": "gap: 0.5lh"},
                    h.input({
                        "class": "input",
                        "data-bind:text": "",
                        "placeholder": "what needs doing?",
                        "autofocus": True,
                        "style": "flex: 1",
                        "data-on:keydown": f"evt.key === 'Enter' && ({submit})",
                    }),
                    h.button({
                        "type": "button",
                        "class": "btn",
                        "style": "--bg: var(--cfg-bg-loud); --fg: -1; "
                                 "border-color: transparent",
                        "data-on:click": submit,
                    }, "add"))),
            h.div({"class": "card stage column"},
                h.h2({"style": "--type: 1"}, f"todos ({len(todos)})"),
                (h.p({"style": "--fg: -0.5"}, "nothing yet — add one above")
                 if not todos else
                 h.ul({"style": "list-style: none; padding: 0; margin: 0"},
                      *[todo_row(tid, text, done)
                        for tid, text, done in todos])))),
    ]


def post_todos(req):
    sigs = signals(req)
    text = (sigs.get("text") or "").strip()
    if text:
        add_todo(text)
    return (200, [], b"")


def post_toggle(req):
    toggle_todo(int(req["params"]["id"]))
    return (200, [], b"")


def post_delete(req):
    delete_todo(int(req["params"]["id"]))
    return (200, [], b"")


ROUTES = [
    ("GET",  "/",                       home),
    ("POST", "/todos",                  post_todos),
    ("POST", "/todos/{id}/toggle",      post_toggle),
    ("POST", "/todos/{id}/delete",      post_delete),
]


# ─── head fragments — same on every render ────────────────────────────
# The framework injects these into <head> on every page. Identical bytes
# every time → idiomorph diff sees no change → script keeps running.

HEAD = [
    h.title("todos"),
    h.link(rel="stylesheet", href=STICK),
    h.script(type="module", src=DATASTAR),
]


if __name__ == "__main__":
    serve(
        ROUTES,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        changes=db.changes,
        head=HEAD,
    )
