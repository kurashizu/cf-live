#!/usr/bin/env python3
"""Track end-to-end latency and its stability from a viewer's vantage point.

Latency alone is a snapshot; what decides whether a broadcast is watchable
is whether it *stays* put. A stream that sits at 3 s and occasionally jumps
to 12 s is worse than one steady at 5 s, because every jump is a rebuffer.

Measures, once per interval: how far the viewer's live edge trails the
origin's, in seconds of media. Reports the distribution and, more
importantly, the drift and the jumps.
"""
import statistics as st
import sys
import time
import urllib.request

CN = sys.argv[1] if len(sys.argv) > 1 else "http://47.116.180.38"
ORIGIN = sys.argv[2] if len(sys.argv) > 2 else "https://live.krsz.in"
MINUTES = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
EVERY = float(sys.argv[4]) if len(sys.argv) > 4 else 120.0


def get(url, timeout=12):
    r = urllib.request.Request(url)
    r.add_header("User-Agent", "krsz-e2e/1")
    r.add_header("Cache-Control", "no-cache")
    f = urllib.request.urlopen(r, timeout=timeout)
    try:
        return f.read().decode("utf-8", "replace")
    finally:
        f.close()


def newest_live(text):
    """The newest fragment that is real content, not the shared slate."""
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("/"):
            return s
    return None


def digest(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def get_bytes(url, timeout=15):
    r = urllib.request.Request(url)
    r.add_header("User-Agent", "krsz-e2e/1")
    r.add_header("Cache-Control", "no-cache")
    f = urllib.request.urlopen(r, timeout=timeout)
    try:
        return f.read()
    finally:
        f.close()


lags = []
errs = 0
samples = 0
t0 = time.time()
nxt = t0 + EVERY
end = t0 + MINUTES * 60


def report(tag):
    print("")
    print("=== %s  t=%.1f min  samples=%d errors=%d ===" % (
        tag, (time.time() - t0) / 60, samples, errs))
    if not lags:
        print("  no measurements")
        sys.stdout.flush()
        return
    q = sorted(lags)
    print("  lag behind origin: med=%.2fs p95=%.2fs min=%.2fs max=%.2fs" % (
        st.median(q), q[min(len(q) - 1, int(len(q) * .95))], q[0], q[-1]))
    print("  stability: stdev=%.2fs  spread(max-min)=%.2fs" % (
        st.pstdev(lags) if len(lags) > 1 else 0.0, q[-1] - q[0]))
    # Drift: is it getting steadily worse, or holding?
    if len(lags) >= 8:
        half = len(lags) // 2
        early = st.median(lags[:half])
        late = st.median(lags[half:])
        print("  drift: first half %.2fs -> second half %.2fs (%+.2fs)" % (
            early, late, late - early))
    jumps = [abs(b - a) for a, b in zip(lags, lags[1:]) if abs(b - a) > 2.0]
    print("  jumps >2s between samples: %d%s" % (
        len(jumps), ("  sizes=%s" % [round(j, 1) for j in jumps[:6]]) if jumps else ""))
    sys.stdout.flush()


while time.time() < end:
    loop = time.time()
    try:
        o = get(ORIGIN + "/live/main/index.m3u8")
        target = newest_live(o)
        if target:
            # The relay renumbers fragments with its own counter so a
            # viewer's timeline survives restarts, so names never match
            # across the two. Compare content instead.
            want = digest(get_bytes(ORIGIN + "/live/main/" + target))
            t_start = time.time()
            found = None
            seen = set()
            while time.time() - t_start < 30:
                c = get(CN + "/main")
                names = [l.strip() for l in c.splitlines()
                         if l.strip() and not l.strip().startswith("#")]
                hit = False
                for n in names:
                    if n in seen:
                        continue
                    seen.add(n)
                    try:
                        if digest(get_bytes(CN + "/" + n.lstrip("/"))) == want:
                            hit = True
                            break
                    except Exception:
                        pass
                if hit:
                    found = time.time() - t_start
                    break
                time.sleep(0.25)
            if found is not None:
                lags.append(found)
                samples += 1
            else:
                errs += 1
                print("  NEVER ARRIVED: %s (>30s) at t=%.0fs" % (
                    target, loop - t0))
                sys.stdout.flush()
    except Exception as e:
        errs += 1
        print("  error %s at t=%.0fs" % (type(e).__name__, loop - t0))
        sys.stdout.flush()
    if time.time() >= nxt:
        report("interim")
        nxt = time.time() + EVERY
    time.sleep(max(0, 3.0 - (time.time() - loop)))

report("FINAL")
