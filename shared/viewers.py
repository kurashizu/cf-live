"""Who is watching, and who was, for the admin page.

Used by both servers a viewer can reach -- the VPS origin (behind Cloudflare)
and the China relay -- so it is stdlib only and runs on Python 3.6, which is
what the China box has.

A viewer is whatever shows up as a distinct (entry point, IP, user agent). It
has a *session*: it starts with the first request and ends when the viewer
has been silent for SESSION_GAP seconds, at which point it moves to the
closed list. The same viewer coming back later starts a new session.

Presence is judged by playlist polls, not segment fetches: Cloudflare caches
segments, so a viewer behind it may hit the origin only for the playlist.
Everything here is in memory and bounded; persistence is history.py's job.
"""
import binascii
import os
import threading
import time
from collections import deque

# A player polls its playlist about once a second, so a viewer silent for
# longer than this is not watching right now...
ACTIVE_WINDOW = 15.0
# ...and one silent for longer than this has left. Wider than ACTIVE_WINDOW
# so a network hiccup does not cut one viewing into two sessions.
SESSION_GAP = 30.0

# Fields come from the network and are attacker-controlled, so each is cut
# short before it is stored.
_LIMITS = {"ip": 64, "ua": 200, "stream": 64, "via": 32,
           "country": 8, "colo": 8, "source": 16}


def _clip(value, field):
    return str(value or "")[:_LIMITS[field]]


class ViewerLog(object):
    def __init__(self, max_entries=2000, session_gap=SESSION_GAP, keep_closed=200):
        self._lock = threading.Lock()
        self._v = {}
        self._max = max_entries
        self._gap = session_gap
        self._closed = deque(maxlen=keep_closed)   # the latest sessions, for telemetry
        self._pending = []                         # closed and not yet drained
        # Session ids must stay unique across restarts and across the two
        # machines that each run one of these, so a random prefix per process.
        self._boot = binascii.hexlify(os.urandom(3)).decode()
        self._n = 0

    # ---- recording ------------------------------------------------------
    def record(self, ip, ua, source, via, stream, kind, nbytes=0,
               country="", colo="", internal=False, now=None):
        """Note one successful request. `kind` is "playlist" or "segment"."""
        now = time.time() if now is None else now
        ip, ua = _clip(ip, "ip"), _clip(ua, "ua")
        source, via = _clip(source, "source"), _clip(via, "via")
        key = (source, via, ip, ua)
        with self._lock:
            v = self._v.get(key)
            if v is not None and now - v["last"] > self._gap:
                self._close(key, v)       # came back after leaving: a new session
                v = None
            if v is None:
                v = self._v[key] = {
                    "ip": ip, "ua": ua, "source": source, "via": via,
                    "stream": "", "country": "", "colo": "",
                    "internal": bool(internal),
                    "first": now, "last": now,
                    "playlists": 0, "segments": 0, "bytes": 0,
                }
                if len(self._v) > self._max:
                    self._shed(now)
            v["last"] = now
            v["stream"] = _clip(stream, "stream") or v["stream"]
            v["country"] = _clip(country, "country") or v["country"]
            v["colo"] = _clip(colo, "colo") or v["colo"]
            if kind == "playlist":
                v["playlists"] += 1
            else:
                v["segments"] += 1
            v["bytes"] += max(0, int(nbytes))

    def _close(self, key, v):
        """Move a session to the closed list. Caller holds the lock."""
        self._v.pop(key, None)
        self._n += 1
        rec = dict(v, id="%s-%d" % (self._boot, self._n))
        self._closed.append(rec)
        # Relay-sender polling is not a viewer and never belongs in history.
        if not v["internal"]:
            self._pending.append(rec)

    def _sweep(self, now):
        for key in [k for k, v in self._v.items() if now - v["last"] > self._gap]:
            self._close(key, self._v[key])

    def _shed(self, now):
        """Over the cap: end the idle ones, then the longest-silent tenth."""
        self._sweep(now)
        if len(self._v) > self._max:
            oldest = sorted(self._v, key=lambda k: self._v[k]["last"])
            for k in oldest[:max(1, self._max // 10)]:
                self._close(k, self._v[k])

    # ---- reading --------------------------------------------------------
    def snapshot(self, now=None):
        """Viewers still in a session, newest first, as ages rather than
        timestamps so a reader never has to trust this machine's clock."""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            self._sweep(now)
            for v in self._v.values():
                out.append({
                    "ip": v["ip"], "ua": v["ua"], "source": v["source"],
                    "via": v["via"], "stream": v["stream"],
                    "country": v["country"], "colo": v["colo"],
                    "internal": v["internal"],
                    "seen_ago": round(now - v["last"], 1),
                    "watched_for": round(v["last"] - v["first"], 1),
                    "playlists": v["playlists"], "segments": v["segments"],
                    "bytes": v["bytes"],
                    "active": (now - v["last"]) <= ACTIVE_WINDOW,
                })
        out.sort(key=lambda r: r["seen_ago"])
        return out

    def closed_snapshot(self, now=None, limit=60):
        """The latest closed sessions, for the telemetry snapshot.

        Sent in full every time rather than as "what is new": the link drops
        some snapshots, and the receiver de-duplicates by id, so a lost one
        costs nothing as long as the next still carries it.
        """
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            recent = [r for r in self._closed if not r["internal"]][-limit:]
        return [{
            "id": r["id"], "ip": r["ip"], "ua": r["ua"], "source": r["source"],
            "via": r["via"], "stream": r["stream"], "country": r["country"],
            "colo": r["colo"], "playlists": r["playlists"],
            "segments": r["segments"], "bytes": r["bytes"],
            "ended_ago": round(now - r["last"], 1),
            "watched_for": round(r["last"] - r["first"], 1),
        } for r in recent]

    def drain_closed(self, now=None):
        """Sessions that closed since the last call, with wall-clock times."""
        now = time.time() if now is None else now
        with self._lock:
            self._sweep(now)
            out, self._pending = self._pending, []
        return out

    def export_live(self):
        """Sessions in progress, with wall-clock times, for a checkpoint."""
        with self._lock:
            return [dict(v) for v in self._v.values() if not v["internal"]]


class FailureLimiter(object):
    """Lock a client out after repeated wrong admin passwords."""

    def __init__(self, limit=10, window=60.0):
        self._lock = threading.Lock()
        self._fails = {}
        self._limit = limit
        self._window = window

    def blocked(self, who, now=None):
        now = time.time() if now is None else now
        with self._lock:
            recent = [t for t in self._fails.get(who, []) if now - t < self._window]
            if recent:
                self._fails[who] = recent
            else:
                self._fails.pop(who, None)
            return len(recent) >= self._limit

    def fail(self, who, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._fails.setdefault(who, []).append(now)
            if len(self._fails) > 5000:       # bounded under a spray of sources
                self._fails.pop(next(iter(self._fails)))
