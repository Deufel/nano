"""SQLite (APSW) wrapper. Per-thread connections.

Pure pass-through to SQL. The app is responsible for calling
`db.changes.notify("subject")` with appropriate subjects when state
changes. No magic dependency tracking, no SQL parsing — just a
database.

Notification model:
    Each Database has a Changes instance. Writers call notify() with
    a dotted subject like "game.5.score" or "chat.room1". Subscribers
    wait() on a pattern that may include wildcards.

    The set of subscribers is implicit: it's the set of threads
    currently parked in changes.wait(). A dropped connection ends its
    handler thread, which ends the subscription. No registry, no list.

Handler code:
    db.execute("INSERT INTO score ...", (...))
    db.changes.notify(f"game.{game_id}.score")

Reader code (inside an SSE stream):
    db.changes.wait(f"game.{game_id}.*", timeout=15)
"""

import atexit
import os
import threading

import apsw

from .changes import Changes


class Database:
    """SQLite wrapper. Per-thread connections via thread-local storage.

    Owns a Changes instance for pub/sub notifications, but does NOT
    auto-notify on writes — the app handler decides when and what to
    publish.
    """

    def __init__(self, path, schema="", changes=None,
                 dev_mode=False, remove_on_exit=False, busy_timeout=5000):
        self.path = path
        self.schema = schema or ""
        self.changes = changes or Changes()
        self.dev_mode = dev_mode
        self.remove_on_exit = remove_on_exit or dev_mode
        self.busy_timeout = busy_timeout
        self._tls = threading.local()
        if self.schema:
            self._init_schema()
        if self.remove_on_exit:
            atexit.register(self.cleanup)

    def _conn(self):
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = apsw.Connection(self.path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout)}")
            self._tls.conn = conn
        return conn

    def _init_schema(self):
        conn = apsw.Connection(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            for sql in self.schema.strip().split(";"):
                sql = sql.strip()
                if sql:
                    conn.execute(sql)
        finally:
            conn.close()

    def conn(self):
        return self._conn()

    def execute(self, sql, params=()):
        return self._conn().execute(sql, params)

    def one(self, sql, params=()):
        return self.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.execute(sql, params).fetchall()

    def transaction(self):
        return self._conn().transaction()

    def cleanup(self):
        try:
            conn = getattr(self._tls, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                self._tls.conn = None
        finally:
            if self.remove_on_exit and self.path:
                for suf in ("", "-wal", "-shm"):
                    p = self.path + suf
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
