"""Real-time backbone: in-process event bus (ring buffer + per-client queues) and a source watcher.
SSE clients reconnect with Last-Event-ID and replay what they missed. Stdlib only."""
import hashlib
import itertools
import os
import queue
import threading
import time
from collections import deque

from common import WORK

MIGRATION = WORK / "migration"


class Bus:
    def __init__(self, keep: int = 300):
        self.lock = threading.Lock()
        self.ring = deque(maxlen=keep)
        self.subs: set[queue.Queue] = set()
        self.ids = itertools.count(1)

    def publish(self, type_: str, data: dict | None = None) -> dict:
        with self.lock:
            ev = {"id": next(self.ids), "type": type_, "data": data or {}, "ts": time.time()}
            self.ring.append(ev)
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except queue.Full:          # slow client: drop it, it will reconnect and replay
                    self.subs.discard(q)
            return ev

    def subscribe(self, last_id: int = 0) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.lock:
            for ev in self.ring:
                if ev["id"] > last_id:
                    q.put_nowait(ev)
            self.subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)

    def clients(self) -> int:
        return len(self.subs)


def _stat(p) -> int:
    try:
        return os.stat(p).st_mtime_ns
    except OSError:
        return 0


def fingerprint() -> str:
    """Cheap change detector: ref files of every bare repo + docs + CSVs (stat only, no reads)."""
    h = hashlib.sha1()
    for d in sorted(MIGRATION.iterdir()):
        if (d / "HEAD").exists():
            h.update(f"{d.name}:{_stat(d / 'packed-refs')}:{_stat(d / 'refs' / 'heads')}".encode())
    for pat in ("REPORTE_*.md", "SECURITY-AUDIT.md", "repo_github/*.csv", "migration/*.sh"):
        for f in sorted(WORK.glob(pat)):
            h.update(f"{f.name}:{_stat(f)}".encode())
    for f in sorted((WORK / "Documentacion").rglob("*.md")):
        h.update(f"{f.name}:{_stat(f)}".encode())
    return h.hexdigest()


class Watcher(threading.Thread):
    def __init__(self, bus: Bus, reindex, interval: float = 5.0):
        super().__init__(daemon=True)
        self.bus, self.reindex, self.interval = bus, reindex, interval
        self.last = fingerprint()
        self.trigger = threading.Event()

    def run(self):
        while True:
            self.trigger.wait(self.interval)
            forced = self.trigger.is_set()
            self.trigger.clear()
            try:
                fp = fingerprint()
                if forced or fp != self.last:
                    self.last = fp
                    self.bus.publish("source:changed", {"forced": forced})
                    self.reindex()
            except Exception as e:  # noqa: BLE001 - watcher must never die
                self.bus.publish("error", {"where": "watcher", "msg": f"{type(e).__name__}: {e}"})
