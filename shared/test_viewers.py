#!/usr/bin/env python3
"""Tests for the viewer log and the admin lockout."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from viewers import ACTIVE_WINDOW, FailureLimiter, ViewerLog  # noqa: E402

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

print("-- activity and expiry")
log = ViewerLog(forget_after=600)
log.record("9.9.9.9", "x", "cn", "http-80", "main", "playlist", now=0.0)
check(log.snapshot(now=ACTIVE_WINDOW - 1)[0]["active"], "recent viewer is active")
check(not log.snapshot(now=ACTIVE_WINDOW + 1)[0]["active"], "silent viewer goes inactive")
check(log.snapshot(now=601) == [], "an old viewer is forgotten")

print("-- bounds")
log = ViewerLog(max_entries=50)
for i in range(500):
    log.record("10.0.%d.%d" % (i // 250, i % 250), "x", "cn", "http-80", "main",
               "playlist", now=float(i))
check(len(log.snapshot(now=500.0)) <= 55, "entries stay near the cap under a flood")
newest = [r["ip"] for r in log.snapshot(now=500.0)]
check("10.0.1.249" in newest, "the newest viewer survives shedding")

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
