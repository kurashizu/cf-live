#!/usr/bin/env python3
"""Tests for the viewer history store."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from history import HistoryStore, from_local, from_telemetry  # noqa: E402
from viewers import ViewerLog  # noqa: E402

fails = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


def sess(i, last=None, **kw):
    last = time.time() - 10 if last is None else last
    d = {"id": "s%d" % i, "server": "origin", "ip": "1.1.1.%d" % (i % 250), "ua": "ua",
         "source": "cloudflare", "via": "cloudflare", "stream": "main",
         "country": "AU", "colo": "SYD", "first": last - 60, "last": last,
         "playlists": 60, "segments": 5, "bytes": 1000}
    d.update(kw)
    return d


tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "history.jsonl")

print("-- adding and querying")
h = HistoryStore(path)
check(h.add([sess(1), sess(2)]) == 2, "stores new sessions")
check(h.add([sess(1), sess(3)]) == 1, "a session id is only stored once")
check(len(h) == 3, "three distinct sessions")
now = time.time()
h.add([sess(10, last=now - 500), sess(11, last=now - 50)])
check([s["id"] for s in h.query(limit=2)] == ["s1", "s2"] or h.query(limit=2)[0]["last"] >= h.query(limit=2)[1]["last"],
      "newest first")
q = h.query(since=now - 100)
check("s10" not in [s["id"] for s in q] and "s11" in [s["id"] for s in q], "filters by time")
check(len(h.query(limit=2)) == 2, "respects the limit")

print("-- persistence")
h2 = HistoryStore(path)
check(len(h2) == len(h), "reloads what was written")
check(h2.add([sess(1)]) == 0, "and still remembers which ids it has")
with open(path, "a") as f:
    f.write('{"id":"torn","last":')           # a crash mid-write
h3 = HistoryStore(path)
check(len(h3) == len(h), "a torn last line is skipped, not fatal")
check(all(json.loads(l) for l in open(path).read().splitlines()), "and the file is repaired")

print("-- bounds")
hb = HistoryStore(os.path.join(tmp, "b.jsonl"), max_items=50)
for i in range(200):
    hb.add([sess(i)])
check(len(hb) == 50, "keeps only the newest max_items")
check(hb.add([sess(0)]) == 1, "an evicted id can be stored again")
check(sum(1 for _ in open(os.path.join(tmp, "b.jsonl"))) <= 101, "the file is compacted, not grown forever")

print("-- retention")
hr = HistoryStore(os.path.join(tmp, "r.jsonl"), keep_days=1)
hr.add([sess(1, last=time.time() - 2 * 86400), sess(2, last=time.time() - 60)])
hr.prune()
check([s["id"] for s in hr.query()] == ["s2"], "sessions past the retention period are dropped")
check([json.loads(l)["id"] for l in open(os.path.join(tmp, "r.jsonl"))] == ["s2"],
      "and removed from the file too")
hr2 = HistoryStore(os.path.join(tmp, "r.jsonl"), keep_days=1)
check(len(hr2) == 1, "an old row is also dropped on load")

print("-- sessions in progress survive a restart")
log = ViewerLog()
log.record("5.5.5.5", "ua", "cloudflare", "cloudflare", "main", "playlist", now=time.time() - 20)
log.record("5.5.5.5", "ua", "cloudflare", "cloudflare", "main", "playlist", now=time.time() - 5)
hp = HistoryStore(os.path.join(tmp, "p.jsonl"))
hp.checkpoint(log.export_live(), "origin")
hq = HistoryStore(os.path.join(tmp, "p.jsonl"))          # the restarted process
check(hq.recover() == 1 and hq.query()[0]["ip"] == "5.5.5.5", "recovers a session the old process left open")
check(not os.path.exists(os.path.join(tmp, "p.jsonl.live")), "the checkpoint is consumed")
hp.checkpoint(log.export_live(), "origin")
check(HistoryStore(os.path.join(tmp, "p.jsonl")).recover() == 0,
      "recovering the same session twice adds nothing")
check(HistoryStore(None).recover() == 0, "no file configured: nothing to recover")
HistoryStore(None).add([sess(1)])
check(len(HistoryStore(None)) == 0, "memory-only store works without a path")

print("-- conversions")
log = ViewerLog()
log.record("6.6.6.6", "ua", "cloudflare", "cloudflare", "main", "playlist", now=100.0)
log.record("6.6.6.6", "ua", "cloudflare", "cloudflare", "main", "playlist", now=130.0)
log.snapshot(now=200.0)
loc = from_local(log.drain_closed(now=200.0))
check(len(loc) == 1 and loc[0]["first"] == 100.0 and loc[0]["last"] == 130.0 and loc[0]["server"] == "origin",
      "a local closed session keeps its own clock")
tel = from_telemetry([{"id": "ab12-1", "ip": "7.7.7.7", "ua": "u", "source": "cn", "via": "http-80",
                       "ended_ago": 40.0, "watched_for": 90.0, "playlists": 9}], received_at=1000.0)
check(tel[0]["last"] == 960.0 and tel[0]["first"] == 870.0, "telemetry ages are placed on this machine's clock")
check(tel[0]["id"] == "cn-ab12-1", "ids from the China side are namespaced")
check(from_telemetry([{"id": "x"}], 1.0) == [], "a malformed telemetry row is skipped")
a = from_telemetry([{"id": "ab12-1", "ip": "7.7.7.7", "ua": "u", "source": "cn", "via": "x",
                     "ended_ago": 40.0, "watched_for": 90.0}], 1000.0)
b = from_telemetry([{"id": "ab12-1", "ip": "7.7.7.7", "ua": "u", "source": "cn", "via": "x",
                     "ended_ago": 42.0, "watched_for": 90.0}], 1002.0)
hd = HistoryStore(None)
check(hd.add(a) == 1 and hd.add(b) == 0, "the same session in two snapshots is stored once")

print()
print("%d failed" % len(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
