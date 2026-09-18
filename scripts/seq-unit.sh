#!/usr/bin/env bash
# Unit tests for the playlist rewriting in vps/cf-live.py.
#
# These cover two bugs that only appear after an encoder restart, which is
# easy to miss in a happy-path test and stalls real players when it regresses.
set -uo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("cl", "vps/cf-live.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CONFIG.update({"window": 3, "slate_version": "deadbeef",
                 "slate_duration": 2.0, "slate_window": 3})

class H:
    trim_window = m.Handler.trim_window
    monotonic_sequence = m.Handler.monotonic_sequence
    offline_playlist = m.Handler.offline_playlist
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

print("── the slate playlist references the fingerprinted path")
sl = h.offline_playlist()
check("/live/_offline.deadbeef.ts" in sl, "slate URI carries the fingerprint")
check("#EXT-X-ENDLIST" not in sl, "slate playlist never ends")

print()
if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails))
    raise SystemExit(1)
print("all playlist unit tests passed")
PY
