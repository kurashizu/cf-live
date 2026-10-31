#!/usr/bin/env python3
"""The admin page's tunnel numbers must match what was actually injected.

Pushes real RS(40,80) segments through the receiver's reassembler while
dropping a known fraction of shards, then checks the loss, headroom and
recovery figures the admin API would report.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
import recv  # noqa: E402
import wire  # noqa: E402
from rs import Codec  # noqa: E402

fails = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


K, N = 40, 80
codec = Codec(K, N)


def run(loss, segments=30, seed=1, size=150000):
    random.seed(seed)
    recv.STATE["stats"].clear()
    asm = recv.Reassembler(12, [])
    recv.STATE["asm"] = asm
    recv.STATE["last_packet"] = 0
    block_size = K * wire.SHARD_PAYLOAD
    for seq in range(segments):
        data = os.urandom(size)
        blocks = [data[i:i + block_size] for i in range(0, len(data), block_size)]
        for bi, blob in enumerate(blocks):
            shards, real = wire.shards_for(blob, K)
            allsh = shards + codec.encode(shards)
            for si, sh in enumerate(allsh):
                if random.random() < loss:
                    continue
                pkt = wire.pack_shard(1, seq, bi, len(blocks), si, K, N, real, sh)
                _t, off = wire.parse_header(pkt)
                asm.add_shard(wire.unpack_shard(pkt, off))
    asm.expire(segments + 100)     # everything has left the window
    return asm


print("-- clean link")
asm = run(0.0)
t = recv.tunnel_overview()
check(t["loss_pct"] == 0.0, "no loss reads as 0%%  (got %s)" % t["loss_pct"])
check(t["segments_failed_pct"] == 0.0, "every segment recovered")
check(t["headroom"] == 40, "full headroom of 40 parity shards (got %s)" % t["headroom"])
check(t["blocks_recovered"] == 0, "no block needed parity")

print("-- 20%% loss")
asm = run(0.20)
t = recv.tunnel_overview()
check(abs(t["loss_pct"] - 20.0) < 2.5, "measures ~20%% loss (got %s)" % t["loss_pct"])
check(t["segments_failed_pct"] == 0.0, "RS(40,80) still recovers every segment at 20%%")
check(0 < t["headroom"] < 40, "headroom shrinks under loss (got %s)" % t["headroom"])
check(t["worst_block_lost"] + t["headroom"] == t["parity"], "headroom = parity - worst block loss")
check(t["blocks_recovered"] > 0 and t["data_shards_recovered"] > 0, "counts blocks saved by parity")
check(t["recovery_ms"]["p50"] is not None, "reports recovery time")
check(len(t["history"]) == 30, "keeps one history entry per segment")

print("-- beyond the cliff")
asm = run(0.55)
t = recv.tunnel_overview()
check(t["segments_failed_pct"] > 50, "55%% loss fails most segments (got %s)" % t["segments_failed_pct"])
check(t["headroom"] is not None and t["headroom"] <= 0, "headroom is exhausted (got %s)" % t["headroom"])

print("-- telemetry snapshot")
check(recv.CONFIG["stats_secret"] == b"", "telemetry is off until a secret is configured")
snap = recv.telemetry_snapshot()
check(set(snap) == {"viewers", "closed", "tunnel", "node"},
      "snapshot carries viewers, closed sessions, tunnel and node")
check(snap["tunnel"]["headroom"] is not None or snap["tunnel"]["segments_judged"] == 0,
      "snapshot includes the tunnel figures")

print()
print("%d failed" % len(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
