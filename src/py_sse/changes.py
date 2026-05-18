"""Topic-based notify/wait. Bounded — past max_waiters per topic, new
subscribe() calls raise OverCapacity. Backpressure is explicit; no
unbounded queue grows.

Threaded model: notify() wakes all current waiters on a topic.
subscribe() blocks the calling thread until wake or timeout.
"""
import threading

class OverCapacity(Exception):
    "Topic has too many concurrent waiters."

class Changes:
    __slots__ = ("_lock","_waiters","_max")
    def __init__(s,max_waiters_per_topic=1024):
        s._lock = threading.Lock()
        s._waiters = {}                # topic -> set[Condition]
        s._max = max_waiters_per_topic

    def notify(s,topic):
        "Wake every thread currently waiting on `topic`. No-op if none."
        with s._lock: conds = list(s._waiters.get(topic,()))
        for c in conds:
            with c: c.notify_all()

    def wait(s,topic,timeout=None):
        """Block until notify(topic) fires or timeout elapses.
        Returns True if woken by notify, False on timeout.
        Raises OverCapacity if topic is at max_waiters."""
        c = threading.Condition()
        with s._lock:
            ws = s._waiters.setdefault(topic,set())
            if len(ws) >= s._max:
                if not ws: s._waiters.pop(topic,None)
                raise OverCapacity(f"topic {topic!r} at max_waiters={s._max}")
            ws.add(c)
        try:
            with c: return bool(c.wait(timeout))
        finally:
            with s._lock:
                ws = s._waiters.get(topic)
                if ws is not None:
                    ws.discard(c)
                    if not ws: s._waiters.pop(topic,None)
