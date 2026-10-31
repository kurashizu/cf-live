#!/usr/bin/env python3
"""Tests for the viewer log and the admin lockout."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from viewers import ACTIVE_WINDOW, SESSION_GAP, FailureLimiter, ViewerLog  # noqa: E402

fails = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


print("-- recording")
log = ViewerLog()
log.record("1.2.3.4", "AVPro", "cloudflare", "cloudflare", "main", "playlist",
           country="AU", colo="SYD", now=100.0)
log.record("1.2.3.4", "AVPro", "cloudflare", "cloudflare", "main", "segment",
           nbytes=5000, now=101.0)
s = log.snapshot(now=102.0)
check(len(s) == 1, "same entry point, IP and agent is one viewer")
check(s[0]["playlists"] == 1 and s[0]["segments"] == 1, "counts playlists and segments apart")
check(s[0]["bytes"] == 5000, "sums bytes")
check(s[0]["country"] == "AU" and s[0]["colo"] == "SYD", "keeps country and edge colo")
check(s[0]["seen_ago"] == 1.0 and s[0]["watched_for"] == 1.0, "reports ages, not timestamps")

log.record("1.2.3.4", "AVPro", "cn", "https-9999", "main", "playlist", now=102.0)
check(len(log.snapshot(now=102.0)) == 2, "a different entry point is a separate row")
log.record("5.6.7.8", "AVPro", "cn", "http-80", "main", "playlist", now=102.0)
check(len(log.snapshot(now=102.0)) == 3, "a different IP is a separate viewer")

print("-- activity and sessions")
log = ViewerLog()
log.record("9.9.9.9", "x", "cn", "http-80", "main", "playlist", now=0.0)
check(log.snapshot(now=ACTIVE_WINDOW - 1)[0]["active"], "recent viewer is active")
r = log.snapshot(now=ACTIVE_WINDOW + 1)
check(len(r) == 1 and not r[0]["active"], "silent viewer goes idle but is still in the session")
check(log.snapshot(now=SESSION_GAP + 1) == [], "past the gap the viewer has left")
done = log.drain_closed(now=SESSION_GAP + 1)
check(len(done) == 1 and done[0]["ip"] == "9.9.9.9", "a left viewer becomes a closed session")
check(done[0]["first"] == 0.0 and done[0]["last"] == 0.0, "keeps wall-clock first and last seen")
check(log.drain_closed(now=SESSION_GAP + 2) == [], "each closed session is handed out once")

log = ViewerLog()
log.record("1.1.1.1", "ua", "cloudflare", "cloudflare", "main", "playlist", now=0.0)
log.record("1.1.1.1", "ua", "cloudflare", "cloudflare", "main", "playlist", now=10.0)
log.record("1.1.1.1", "ua", "cloudflare", "cloudflare", "main", "segment", nbytes=500, now=20.0)
check(len(log.snapshot(now=21.0)) == 1 and not log.drain_closed(now=21.0),
      "a gap shorter than the limit stays one session")
log.record("1.1.1.1", "ua", "cloudflare", "cloudflare", "main", "playlist", now=20.0 + SESSION_GAP + 5)
closed = log.drain_closed(now=20.0 + SESSION_GAP + 5)
check(len(closed) == 1 and closed[0]["playlists"] == 2 and closed[0]["bytes"] == 500,
      "coming back after the gap closes the old session with its own counts")
live = log.snapshot(now=20.0 + SESSION_GAP + 6)
check(len(live) == 1 and live[0]["playlists"] == 1 and live[0]["watched_for"] == 0.0,
      "and starts a fresh one")

log = ViewerLog()
log.record("127.0.0.1", "krsz-send/1", "local", "loopback", "main", "playlist", internal=True, now=0.0)
log.record("2.2.2.2", "p", "cloudflare", "cloudflare", "main", "playlist", now=0.0)
log.snapshot(now=SESSION_GAP + 1)
check([c["ip"] for c in log.drain_closed(now=SESSION_GAP + 1)] == ["2.2.2.2"],
      "relay-sender polling never reaches history, real viewers do")

print("-- closed list for telemetry")
log = ViewerLog()
for i in range(5):
    log.record("10.0.0.%d" % i, "ua", "cn", "http-80", "main", "playlist", now=float(i))
cl = log.closed_snapshot(now=100.0, limit=3)
check(len(cl) == 3, "limits to the newest few")
check(all(c["ended_ago"] > 90 for c in cl), "reports ages, not timestamps")
check(len(set(c["id"] for c in cl)) == 3, "every session has a distinct id")
check(log.closed_snapshot(now=101.0, limit=3)[0]["id"] == cl[0]["id"],
      "ids are stable between snapshots, so the receiver can de-duplicate")
check(ViewerLog()._boot != ViewerLog()._boot, "ids differ between processes")

print("-- checkpoint export")
log = ViewerLog()
log.record("4.4.4.4", "a", "cn", "http-80", "main", "playlist", now=5.0)
log.record("127.0.0.1", "krsz-send/1", "local", "loopback", "main", "playlist", internal=True, now=5.0)
check([v["ip"] for v in log.export_live()] == ["4.4.4.4"], "exports live sessions, minus internal ones")

print("-- bounds")
log = ViewerLog(max_entries=50)
for i in range(500):
    log.record("10.0.%d.%d" % (i // 250, i % 250), "x", "cn", "http-80", "main",
               "playlist", now=float(i))
check(len(log.snapshot(now=500.0)) <= 55, "live entries stay near the cap under a flood")
log2 = ViewerLog(max_entries=50)
for i in range(120):
    log2.record("10.0.%d.%d" % (i // 250, i % 250), "x", "cn", "http-80", "main", "playlist", now=1.0)
check(len(log2.snapshot(now=1.0)) <= 55, "same inside one second, where nothing has idled")
check(len(log2.drain_closed(now=1.0)) >= 60, "viewers shed by the cap are closed, not lost")

log = ViewerLog()
log.record("1.1.1.1", "A" * 5000, "cn", "http-80", "s" * 5000, "playlist", now=1.0)
r = log.snapshot(now=1.0)[0]
check(len(r["ua"]) <= 200 and len(r["stream"]) <= 64, "oversized fields are clipped")

print("-- lockout")
lim = FailureLimiter(limit=3, window=60)
check(not lim.blocked("a", now=0), "a fresh client is not blocked")
for t in (1, 2, 3):
    lim.fail("a", now=t)
check(lim.blocked("a", now=4), "blocked after the limit")
check(not lim.blocked("b", now=4), "other clients are unaffected")
check(not lim.blocked("a", now=70), "the lockout expires")

print()
print("%d failed" % len(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
