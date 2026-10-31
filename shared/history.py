"""Closed viewer sessions, kept so the admin page can look back.

An append-only JSON-lines file plus an in-memory list. Append-only so a crash
cannot corrupt what is already written; bounded by count and by age so it
cannot grow without limit or keep addresses longer than intended.

Sessions arrive from two places, the origin's own viewers and the China
relay's telemetry, and the second is lossy and repeats itself, so everything
is de-duplicated by session id. Stdlib only.
"""
import json
import os
import threading
import time
import zlib


class HistoryStore(object):
    def __init__(self, path=None, max_items=5000, keep_days=30.0):
        self._path = path or None
        self._max = max_items
        self._keep = keep_days * 86400.0
        self._lock = threading.Lock()
        self._items = []
        self._ids = set()
        self._lines = 0
        if self._path:
            self._load()

    # ---- persistence ----------------------------------------------------
    def _load(self):
        try:
            with open(self._path) as f:
                raw = f.read().splitlines()
        except OSError:
            return
        cutoff = time.time() - self._keep
        kept = []
        for line in raw:
            try:
                s = json.loads(line)
                if s["id"] and s["last"] >= cutoff:
                    kept.append(s)
            except (ValueError, KeyError, TypeError):
                continue                      # a torn last line after a crash
        for s in kept[-self._max:]:
            if s["id"] not in self._ids:
                self._ids.add(s["id"])
                self._items.append(s)
        self._lines = len(raw)
        if len(raw) != len(self._items):
            self._rewrite()

    def _rewrite(self):
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                for s in self._items:
                    f.write(json.dumps(s, separators=(",", ":")) + "\n")
            os.replace(tmp, self._path)
            self._lines = len(self._items)
        except OSError:
            pass

    # ---- adding and reading ---------------------------------------------
    def add(self, sessions):
        """Store new sessions; returns how many were not already known."""
        fresh = []
        with self._lock:
            for s in sessions:
                if not s.get("id") or s["id"] in self._ids:
                    continue
                self._ids.add(s["id"])
                self._items.append(s)
                fresh.append(s)
            over = len(self._items) - self._max
            if over > 0:
                for old in self._items[:over]:
                    self._ids.discard(old["id"])
                del self._items[:over]
            if fresh and self._path:
                try:
                    with open(self._path, "a") as f:
                        for s in fresh:
                            f.write(json.dumps(s, separators=(",", ":")) + "\n")
                    self._lines += len(fresh)
                except OSError:
                    pass
                if self._lines > 2 * self._max:
                    self._rewrite()
        return len(fresh)

    def prune(self, now=None):
        """Drop sessions older than the retention period."""
        now = time.time() if now is None else now
        with self._lock:
            keep = [s for s in self._items if now - s["last"] <= self._keep]
            if len(keep) != len(self._items):
                self._items = keep
                self._ids = set(s["id"] for s in keep)
                if self._path:
                    self._rewrite()

    def query(self, limit=500, since=None):
        """Newest first."""
        with self._lock:
            rows = [s for s in self._items if since is None or s["last"] >= since]
        rows.sort(key=lambda s: s["last"], reverse=True)
        return rows[:max(1, limit)]

    def __len__(self):
        with self._lock:
            return len(self._items)

    # ---- sessions in progress -------------------------------------------
    def checkpoint(self, live, server, now=None):
        """Remember who is watching right now.

        A restart would otherwise lose every session in progress: they only
        reach history by closing, and a killed process never closes them.
        """
        if not self._path:
            return
        now = time.time() if now is None else now
        doc = {"t": now, "sessions": [_as_session(v, server) for v in live]}
        tmp = self._path + ".live.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, self._path + ".live")
        except OSError:
            pass

    def recover(self):
        """Close the sessions a previous process left open. Returns the count."""
        if not self._path:
            return 0
        live = self._path + ".live"
        try:
            with open(live) as f:
                doc = json.load(f)
            sessions = doc["sessions"]
        except (OSError, ValueError, KeyError, TypeError):
            return 0
        n = self.add(sessions)
        try:
            os.remove(live)
        except OSError:
            pass
        return n


def _sid(prefix, first, ip, ua):
    """A stable id, so recovering the same checkpoint twice adds nothing."""
    return "%s-%d-%08x" % (prefix, int(first * 1000),
                           zlib.crc32(("%s|%s" % (ip, ua)).encode()) & 0xffffffff)


def _as_session(v, server):
    """A ViewerLog record (live or closed) as a stored session."""
    return {
        "id": v.get("id") or _sid("ck", v["first"], v["ip"], v["ua"]),
        "server": server, "ip": v["ip"], "ua": v["ua"],
        "source": v["source"], "via": v["via"], "stream": v["stream"],
        "country": v["country"], "colo": v["colo"],
        "first": round(v["first"], 1), "last": round(v["last"], 1),
        "playlists": v["playlists"], "segments": v["segments"], "bytes": v["bytes"],
    }


def from_local(closed, server="origin"):
    """Closed ViewerLog records -> stored sessions."""
    return [_as_session(r, server) for r in closed]


def from_telemetry(closed, received_at, server="cn"):
    """Closed sessions from a telemetry snapshot -> stored sessions.

    The sender describes them as ages ("ended 40 s ago"), so the receiving
    machine places them on its own clock. The two boxes' clocks may differ,
    and a timestamp from the wrong one would put a session in the wrong hour.
    """
    out = []
    for r in closed:
        try:
            last = received_at - float(r["ended_ago"])
            out.append({
                "id": "%s-%s" % (server, r["id"]), "server": server,
                "ip": str(r["ip"]), "ua": str(r["ua"]),
                "source": str(r["source"]), "via": str(r["via"]),
                "stream": str(r.get("stream", "")),
                "country": str(r.get("country", "")), "colo": str(r.get("colo", "")),
                "first": round(last - float(r["watched_for"]), 1),
                "last": round(last, 1),
                "playlists": int(r.get("playlists", 0)),
                "segments": int(r.get("segments", 0)),
                "bytes": int(r.get("bytes", 0)),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out
