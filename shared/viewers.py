"""Who is watching, for the admin page.

Used by both servers a viewer can reach -- the VPS origin (behind Cloudflare)
and the China relay -- so it is stdlib only and runs on Python 3.6, which is
what the China box has.

A viewer is whatever shows up as a distinct (entry point, IP, user agent).
Presence is judged by playlist polls, not segment fetches: Cloudflare caches
segments, so a viewer behind it may hit the origin only for the playlist.
Everything here is in memory and bounded; nothing is written to disk.
"""
import threading
import time

# A player polls its playlist about once a second, so a viewer silent for
# longer than this has stopped watching.
ACTIVE_WINDOW = 15.0

# Fields come from the network and are attacker-controlled, so each is cut
# short before it is stored.
_LIMITS = {"ip": 64, "ua": 200, "stream": 64, "via": 32,
           "country": 8, "colo": 8, "source": 16}


def _clip(value, field):
    return str(value or "")[:_LIMITS[field]]


class ViewerLog(object):
    def __init__(self, max_entries=2000, forget_after=600.0):
        self._lock = threading.Lock()
        self._v = {}
        self._max = max_entries
        self._forget = forget_after

    def record(self, ip, ua, source, via, stream, kind, nbytes=0,
               country="", colo="", internal=False, now=None):
        """Note one successful request. `kind` is "playlist" or "segment"."""
        now = time.time() if now is None else now
        ip, ua = _clip(ip, "ip"), _clip(ua, "ua")
        source, via = _clip(source, "source"), _clip(via, "via")
        key = (source, via, ip, ua)
        with self._lock:
            v = self._v.get(key)
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

    def _shed(self, now):
        """Over the cap: forget the stale ones, then the oldest tenth."""
        for k in [k for k, v in self._v.items()
                  if now - v["last"] > self._forget]:
            del self._v[k]
        if len(self._v) > self._max:
            oldest = sorted(self._v, key=lambda k: self._v[k]["last"])
            for k in oldest[:max(1, self._max // 10)]:
                del self._v[k]

    def snapshot(self, now=None):
        """Viewers newest first, as ages rather than timestamps so a reader
        never has to trust this machine's clock."""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for k in [k for k, v in self._v.items()
                      if now - v["last"] > self._forget]:
                del self._v[k]
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
