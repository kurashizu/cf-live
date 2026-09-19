#!/usr/bin/env bash
# Unit tests for the playlist rewriting in vps/cf-live.py.
#
# These cover two bugs that only appear after an encoder restart, which is
# easy to miss in a happy-path test and stalls real players when it regresses.
set -uo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import importlib.util, pathlib, shutil
spec = importlib.util.spec_from_file_location("cl", "vps/cf-live.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CONFIG.update({"window": 3, "slate_version": "deadbeef",
                 "slate_duration": 2.0, "slate_window": 3})

class H:
    trim_window = m.Handler.trim_window
    monotonic_sequence = m.Handler.monotonic_sequence
h = H()

def playlist(seq, n, start):
    body = ["#EXTM3U", "#EXT-X-VERSION:3",
            f"#EXT-X-MEDIA-SEQUENCE:{seq}", "#EXT-X-TARGETDURATION:1"]
    for i in range(n):
        body += ["#EXTINF:1.000, no desc", f"seg{start+i:05d}.ts"]
    return ("\n".join(body) + "\n").encode()

def seq_of(text):
    for line in text.splitlines():
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            return int(line.split(":", 1)[1])
    raise AssertionError("no media sequence")

fails = []
def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: fails.append(label)

print("── sequence is monotonic across a restart")
m.SEQ_STATE.clear()
served = []
# A normal run, then ffmpeg relaunches and restarts at zero.
for raw, start in [(10, 10), (11, 11), (12, 12), (0, 0), (1, 1), (2, 2)]:
    out = h.trim_window(playlist(raw, 6, start), "s").decode()
    served.append((seq_of(out), "#EXT-X-DISCONTINUITY" in out))
seqs = [s for s, _ in served]
check(all(b > a for a, b in zip(seqs, seqs[1:])),
      f"published sequence only increases: {seqs}")
check(sum(1 for _, d in served if d) == 1,
      f"exactly one DISCONTINUITY: {[d for _, d in served]}")
check(served[3][1], "the tag lands on the first playlist of the new timeline")

print("── repeated restarts each stay monotonic")
m.SEQ_STATE.clear()
seqs = []
for raw, start in [(5,5),(6,6),(0,0),(1,1),(0,0),(1,1),(2,2)]:
    seqs.append(seq_of(h.trim_window(playlist(raw, 6, start), "s").decode()))
check(all(b > a for a, b in zip(seqs, seqs[1:])),
      f"two restarts, still increasing: {seqs}")

print("── an unchanged playlist keeps its sequence")
m.SEQ_STATE.clear()
a = seq_of(h.trim_window(playlist(7, 6, 7), "s").decode())
b = seq_of(h.trim_window(playlist(7, 6, 7), "s").decode())
check(a == b, f"same input, same sequence ({a} == {b})")

print("── DISCONTINUITY is placed before the segment it applies to")
m.SEQ_STATE.clear()
h.trim_window(playlist(9, 6, 9), "s")
out = h.trim_window(playlist(0, 6, 0), "s").decode().splitlines()
i = out.index("#EXT-X-DISCONTINUITY")
check(out[i+1].startswith("#EXTINF:"),
      f"tag precedes an EXTINF: {out[i:i+3]}")
check(not out[i-1].startswith("#EXTINF:"),
      "tag does not split an EXTINF from its URI")

print("── streams keep independent sequence state")
m.SEQ_STATE.clear()
h.trim_window(playlist(50, 6, 50), "a")
b_seq = seq_of(h.trim_window(playlist(3, 6, 3), "b").decode())
check(b_seq == 6, f"stream b unaffected by stream a (got {b_seq})")

print("── the window is trimmed to --window entries")
m.SEQ_STATE.clear()
out = h.trim_window(playlist(0, 6, 0), "s").decode()
n = sum(1 for l in out.splitlines() if l and not l.startswith("#"))
check(n == 3, f"6 entries trimmed to 3 (got {n})")

print("── a finished playlist is left alone")
m.SEQ_STATE.clear()
done = playlist(0, 6, 0).decode() + "#EXT-X-ENDLIST\n"
check(h.trim_window(done.encode(), "s") == done.encode(),
      "ENDLIST playlist passes through untouched")

print("── idle slate advances on the wall clock, not on polls")
# The continuous playlist is what viewers actually receive. Drive it with a
# fake clock and irregular polls: the number of slate entries must depend
# only on elapsed time, or a slow poller's timeline slips behind real time
# and its player drains and stalls once per fragment.
import time as _time
class C(H):
    continuous_playlist = m.Handler.continuous_playlist
    slate_uri = m.Handler.slate_uri
    def __init__(self): self.fresh = []
    def live_segments(self, stream): return list(self.fresh)
c = C()
clock = [1000.0]
_real = _time.time
_time.time = lambda: clock[0]
try:
    dur = m.CONFIG["slate_duration"]
    m.SEQ_STATE.clear()
    def entries(body):
        return [l for l in body.decode().splitlines() if l and not l.startswith("#")]
    first = c.continuous_playlist("s")
    check(len(entries(first)) >= 3, "a new idle stream starts with a full window")
    hold = dur * 2 + 1.0
    def discs(body):
        return sum(1 for l in body.splitlines() if l == "#EXT-X-DISCONTINUITY")
    check(discs(first.decode()) == 0,
          "no discontinuity inside a pure slate timeline")
    # Poll at awkward moments: late, then in a burst, then not at all.
    seen = list(entries(first))
    for dt in (0.9 * dur, 2.5 * dur, 0.1, 0.1, 0.1, 3.0 * dur):
        clock[0] += dt
        for u in entries(c.continuous_playlist("s")):
            if u not in seen: seen.append(u)
    elapsed = clock[0] - 1000.0
    expected = len(entries(first)) + int(elapsed / dur)
    check(len(seen) == expected,
          f"{elapsed/dur:.1f} durations elapsed -> {expected} entries (got {len(seen)})")
    pos = [int(u.rsplit(".", 2)[1]) for u in seen]
    check(pos == list(range(pos[0], pos[0] + len(pos))),
          f"positions climb by one and never repeat: {pos[:6]}...")
    check(all("/live/_offline.deadbeef." in u for u in seen),
          "slate URIs carry the fingerprint")

    print("── a long unpolled idle skips ahead instead of replaying the gap")
    clock[0] += 600
    body = c.continuous_playlist("s").decode()
    tail = seq_of(body)
    check(tail < 3 + 600 / dur, f"sequence did not replay ten minutes of slate (seq {tail})")

    def discs_of(body):
        return sum(1 for l in body.splitlines() if l == "#EXT-X-DISCONTINUITY")
    print("── live -> slate is exactly one seam")
    m.SEQ_STATE.clear()
    c.fresh = [("seg00000.ts", 0.5), ("seg00001.ts", 0.5)]
    c.continuous_playlist("s")
    clock[0] += hold   # let the opening slate entries age out of the window
    c.fresh = [("seg00000.ts", 0.5), ("seg00001.ts", 0.5), ("seg00002.ts", 0.5)]
    c.continuous_playlist("s")
    c.fresh = []
    discs = 0
    for _ in range(4):
        clock[0] += dur
        discs += discs_of(c.continuous_playlist("s").decode())
    check(discs >= 1, "the first slate entry after live carries a discontinuity")
    body = c.continuous_playlist("s").decode()
    check(discs_of(body) <= 1, "later slate entries carry none")
    print("── DISCONTINUITY-SEQUENCE counts the tags that scrolled away")
    def dseq(body):
        for l in body.splitlines():
            if l.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                return int(l.split(":")[1])
    m.SEQ_STATE.clear()
    c.continuous_playlist("s")
    clock[0] += hold
    c.fresh = [("seg00000.ts", 0.5), ("seg00001.ts", 0.5), ("seg00002.ts", 0.5)]
    c.continuous_playlist("s")
    c.fresh = []
    clock[0] += dur
    body = c.continuous_playlist("s").decode()
    at_seam = dseq(body)
    check(discs_of(body) == 1, "the seam tag is in the window")
    for _ in range(4):
        clock[0] += dur
        body = c.continuous_playlist("s").decode()
    check(discs_of(body) == 0 and dseq(body) == at_seam + 1,
          f"tag scrolled out, sequence rose by one ({at_seam} -> {dseq(body)})")

    print("── slate entries are held across the slate -> live seam")
    # A player still polling at the slate cadence must find an entry it
    # already knows in every playlist, or it misplaces the live timeline.
    m.SEQ_STATE.clear()
    c.fresh = []
    c.continuous_playlist("s")
    clock[0] += hold + dur
    before = entries(c.continuous_playlist("s"))
    segs = []
    for i in range(6):
        segs.append(("seg%05d.ts" % i, 0.5))
        c.fresh = list(segs[-6:])
        clock[0] += 0.5
        now_entries = entries(c.continuous_playlist("s"))
        check(set(before) & set(now_entries),
              f"+{(i+1)*0.5:.1f}s: overlaps the pre-seam window ({len(now_entries)} entries)")
    clock[0] += hold
    c.fresh = list(segs[-6:])
    now_entries = entries(c.continuous_playlist("s"))
    check(len(now_entries) == 3 and not any("_offline" in e for e in now_entries),
          "after the hold the window is back to three live entries")
    print("── an encoder restart while live is a seam")
    c.fresh = [("seg00000.ts", 0.5)]
    body = c.continuous_playlist("s").decode().splitlines()
    i = body.index("#EXT-X-DISCONTINUITY") if "#EXT-X-DISCONTINUITY" in body else -1
    check(i >= 0 and body[i + 2] == "seg00000.ts",
          f"renumbered segments start with a discontinuity: {body[i:i+3]}")

    print("── slate -> live is exactly one seam")
    c.fresh = []
    for _ in range(5):
        clock[0] += dur
        c.continuous_playlist("s")
    c.fresh = [("seg00000.ts", 0.5)]
    body = c.continuous_playlist("s").decode().splitlines()
    i = body.index("#EXT-X-DISCONTINUITY") if "#EXT-X-DISCONTINUITY" in body else -1
    check(i >= 0 and body[i + 2] == "seg00000.ts",
          f"discontinuity sits on the first live segment: {body[i:i+3]}")
finally:
    _time.time = _real

print("── shifted slate timestamps move by exactly the position offset")
raw = pathlib.Path("vps/offline.ts").read_bytes()
import subprocess, json
def starts(data):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=start_time", "-of", "json", "-"],
                       input=data, capture_output=True)
    return [float(x["start_time"]) for x in json.loads(r.stdout)["streams"]]
if shutil.which("ffprobe"):
    s0, s7 = starts(raw), starts(m.shift_timestamps(raw, 7 * dur))
    check(all(abs((b - a) - 7 * dur) < 0.001 for a, b in zip(s0, s7)),
          f"every track advanced by 7 x {dur:.4f}s: {s0} -> {s7}")
    check(len(m.shift_timestamps(raw, 1.0)) == len(raw), "length unchanged")
else:
    print("  SKIP  ffprobe not installed")

print()
if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails))
    raise SystemExit(1)
print("all playlist unit tests passed")
PY
