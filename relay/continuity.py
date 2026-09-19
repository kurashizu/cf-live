#!/usr/bin/env python3
"""Watch a viewer's stream for continuity and interruptions.

This is the question that decides whether playback is usable: not average
latency, but whether the timeline ever breaks. A player tolerates a slow
fragment; it stalls on a gap in the sequence, on a fragment it was told to
fetch and cannot, or on a playlist that stops advancing.

Tracks, once per second:
  - continuity: fragment indices must arrive in order with no skips
  - fetchability: everything advertised must download
  - liveness: the playlist must keep advancing
  - outages: runs of seconds with no usable playlist at all
"""
import collections
import json
import statistics as st
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9990"
MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
EVERY = float(sys.argv[3]) if len(sys.argv) > 3 else 120.0


def get(u, t=12):
    r = urllib.request.Request(u)
    r.add_header("User-Agent", "krsz-cont/1")
    r.add_header("Cache-Control", "no-cache")
    t0 = time.time()
    f = urllib.request.urlopen(r, timeout=t)
    try:
        return f.read(), (time.time() - t0) * 1000
    finally:
        f.close()


def idx(name):
    d = "".join(ch for ch in name if ch.isdigit())
    return int(d) if d else -1


c = collections.Counter()
pl_ms = []
frag_ms = []
played = []
outages = []
outage = 0
max_outage = 0
last_seq = -1
last_adv = time.time()
max_freeze = 0.0
fetched = set()
t0 = time.time()
nxt = t0 + EVERY
end = t0 + MINUTES * 60


def report(tag):
    el = time.time() - t0
    skips = 0
    skipped_total = 0
    for a, b in zip(played, played[1:]):
        if b > a + 1:
            skips += 1
            skipped_total += b - a - 1
    print("")
    print("=== %s  t=%.1f min ===" % (tag, el / 60))
    print("polls=%d  playlist_fail=%d  fragments=%d  UNFETCHABLE=%d" % (
        c["polls"], c["pl_fail"], len(fetched), c["unfetchable"]))
    print("CONTINUITY: %d breaks, %d fragments skipped (of %d consumed)" % (
        skips, skipped_total, len(played)))
    print("INTERRUPTIONS: %d outages, longest %ds | longest freeze %.1fs" % (
        len(outages), max_outage, max_freeze))
    if outages:
        print("  outage lengths: %s" % outages[-10:])
    if pl_ms:
        q = sorted(pl_ms)
        print("playlist ms med=%.1f p95=%.1f max=%.1f" % (
            st.median(q), q[min(len(q) - 1, int(len(q) * .95))], q[-1]))
    if frag_ms:
        q = sorted(frag_ms)
        print("fragment ms med=%.1f p95=%.1f max=%.1f" % (
            st.median(q), q[min(len(q) - 1, int(len(q) * .95))], q[-1]))
    try:
        s = json.loads(get(BASE + "/stats")[0].decode())
        print("relay: buffered=%s sinceLastPacket=%s counters=%s" % (
            s.get("segmentsBuffered"), s.get("secondsSinceLastPacket"),
            s.get("counters")))
    except Exception as e:
        print("stats unavailable: %s" % type(e).__name__)
    sys.stdout.flush()


while time.time() < end:
    loop = time.time()
    c["polls"] += 1
    try:
        body, ms = get(BASE + "/main")
        pl_ms.append(ms)
        if outage:
            outages.append(outage)
            max_outage = max(max_outage, outage)
            print("  recovered after %ds outage at t=%.0fs" % (
                outage, loop - t0))
            sys.stdout.flush()
            outage = 0
    except Exception as e:
        c["pl_fail"] += 1
        outage += 1
        max_outage = max(max_outage, outage)
        if outage == 1:
            print("  OUTAGE starts at t=%.0fs (%s)" % (
                loop - t0, type(e).__name__))
            sys.stdout.flush()
        time.sleep(1)
        continue

    text = body.decode("utf-8", "replace")
    seq = None
    uris = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line and not line.startswith("#"):
            uris.append(line)

    if seq is not None:
        if seq != last_seq:
            last_adv = loop
            last_seq = seq
        else:
            freeze = loop - last_adv
            max_freeze = max(max_freeze, freeze)
            if freeze > 5:
                c["frozen"] += 1

    for u in uris:
        if u in fetched:
            continue
        try:
            b, fms = get(BASE + "/" + u.lstrip("/"))
            if not b:
                c["unfetchable"] += 1
                print("  EMPTY %s at t=%.0fs" % (u, loop - t0))
                sys.stdout.flush()
                continue
            fetched.add(u)
            frag_ms.append(fms)
            i = idx(u)
            if i >= 0:
                played.append(i)
        except Exception as e:
            c["unfetchable"] += 1
            print("  UNFETCHABLE %s (%s) at t=%.0fs" % (
                u, type(e).__name__, loop - t0))
            sys.stdout.flush()

    if time.time() >= nxt:
        report("interim")
        nxt = time.time() + EVERY
    time.sleep(max(0, 1.0 - (time.time() - loop)))

report("FINAL")
