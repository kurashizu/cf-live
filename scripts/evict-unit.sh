#!/usr/bin/env bash
# Tests for segment eviction in vps/cf-live.py.
#
# Eviction orders by the counter in the filename, which is right while an
# encoder runs and wrong the moment it restarts: ffmpeg renumbers from zero,
# so a fresh seg00000 sorts below the leftovers and gets deleted on arrival.
# That failure is silent -- the encoder reports success, ingest just never
# goes live -- so it needs a test rather than vigilance.
set -uo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import importlib.util, pathlib, shutil, tempfile
spec = importlib.util.spec_from_file_location("cl", "vps/cf-live.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CONFIG.update({"keep_segments": 12})

class H:
    evict = m.Handler.evict
h = H()
fails = []

def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)

def setup(names):
    d = pathlib.Path(tempfile.mkdtemp())
    for n in names:
        (d / n).write_bytes(b"x")
    return d
import os
def touch_order(d, names):
    """Give files strictly increasing mtimes in the given order."""
    base = 1_700_000_000
    for i, n in enumerate(names):
        os.utime(d / n, ns=((base + i) * 10**9, (base + i) * 10**9))

print("-- encoder restart after a long run")
d = setup(["seg%05d.ts" % i for i in range(4787, 4799)])
touch_order(d, ["seg%05d.ts" % i for i in range(4787, 4799)])
(d / "seg00000.ts").write_bytes(b"new")
h.evict(d, "seg00000.ts")
left = sorted(p.name for p in d.iterdir())
check("seg00000.ts" in left, "the newly written segment survives")
for i in range(1, 12):
    (d / ("seg%05d.ts" % i)).write_bytes(b"new")
    h.evict(d, "seg%05d.ts" % i)
left = sorted(p.name for p in d.iterdir())
check(not any(n.startswith("seg047") for n in left) and len(left) == 12,
      "stale timeline displaced by the new one: %s..%s" % (left[0], left[-1]))
shutil.rmtree(d)

print("-- encoder restart shortly after a short run")
# Leftovers numbered within a few dozen of zero: the case a threshold on
# the counter cannot catch.
d = setup(["seg%05d.ts" % i for i in range(29, 40)] + ["seg00046.ts", "seg00047.ts"])
import time; time.sleep(0.01)
for i in range(3):
    (d / ("seg%05d.ts" % i)).write_bytes(b"new")
    h.evict(d, "seg%05d.ts" % i)
left = sorted(p.name for p in d.iterdir())
check(all(("seg%05d.ts" % i) in left for i in range(3)),
      "every new segment survives: %s" % left[:4])
shutil.rmtree(d)

print("-- steady state trims to keep_segments")
d = setup(["seg%05d.ts" % i for i in range(100)])
h.evict(d, "seg00099.ts")
left = sorted(p.name for p in d.iterdir())
check(len(left) == 12, "12 kept, got %d" % len(left))
check(left[-1] == "seg00099.ts" and left[0] == "seg00088.ts",
      "keeps the newest contiguous tail")
shutil.rmtree(d)

print("-- a late arrival is not mistaken for a restart")
d = setup(["seg%05d.ts" % i for i in range(50, 63)])
h.evict(d, "seg00058.ts")
check("seg00062.ts" in [p.name for p in d.iterdir()],
      "newer segments are not discarded")
shutil.rmtree(d)

print("-- non-segment files are untouched")
d = setup(["seg%05d.ts" % i for i in range(20)])
(d / "live.m3u8").write_bytes(b"#EXTM3U")
(d / "init.mp4").write_bytes(b"x")
h.evict(d, "seg00019.ts")
names = [p.name for p in d.iterdir()]
check("live.m3u8" in names and "init.mp4" in names, "playlist and init survive")
shutil.rmtree(d)

print()
if fails:
    raise SystemExit("FAILED: " + "; ".join(fails))
print("all eviction tests passed")
PY
