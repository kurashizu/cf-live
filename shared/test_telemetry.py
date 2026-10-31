#!/usr/bin/env python3
"""Tests for the telemetry that rides the UDP tunnel.

The sender's UDP port is open to the internet, so the important cases are the
hostile ones: forged, tampered, replayed and oversized snapshots must all be
refused, and a snapshot must survive the loss the link actually has.
"""
import os
import random
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "relay"))
import telemetry as T  # noqa: E402
import wire  # noqa: E402

fails = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


SECRET = T.derive_secret("some-ingest-key")
DOC = {"viewers": [{"ip": "1.2.3.4", "ua": "x" * 80}] * 40, "tunnel": {"loss_pct": 1.5}}

print("-- sealing")
blob = T.seal(DOC, SECRET, now=1000.0)
got = T.open_sealed(blob, SECRET, now=1001.0)
check(got is not None and got["tunnel"] == {"loss_pct": 1.5}, "a sealed snapshot opens")
check(got["t"] == 1000.0, "carries its timestamp")
check(len(blob) < 1500, "40 viewers compress small enough for a couple of datagrams (%d B)" % len(blob))
check(T.derive_secret("some-ingest-key") == SECRET, "the secret is deterministic")
check(T.derive_secret("another-key") != SECRET, "a different key gives a different secret")
check(b"some-ingest-key" not in SECRET, "the secret does not contain the key")

print("-- hostile input")
check(T.open_sealed(blob, T.derive_secret("wrong"), now=1001.0) is None, "wrong secret is refused")
bad = bytearray(blob); bad[5] ^= 1
check(T.open_sealed(bytes(bad), SECRET, now=1001.0) is None, "a flipped bit is refused")
check(T.open_sealed(blob[:-1], SECRET, now=1001.0) is None, "a truncated snapshot is refused")
check(T.open_sealed(b"", SECRET) is None and T.open_sealed(b"x" * 8, SECRET) is None, "tiny garbage is refused")
check(T.open_sealed(blob, SECRET, now=1000.0 + T.MAX_AGE + 5) is None, "a stale replay is refused")
check(T.open_sealed(blob, SECRET, now=1000.0 - T.MAX_AGE - 5) is None, "a far-future timestamp is refused")
import hashlib, hmac  # noqa: E402
bomb = zlib.compress(b"0" * (T.MAX_JSON * 4), 9)
forged_ok_mac = bomb + hmac.new(SECRET, bomb, hashlib.sha256).digest()[:T.MAC_LEN]
check(T.open_sealed(forged_ok_mac, SECRET, now=1.0) is None,
      "a correctly signed decompression bomb is still refused (%d B -> %d MB)" % (len(bomb), T.MAX_JSON * 4 >> 20))
notjson = zlib.compress(b"not json at all")
check(T.open_sealed(notjson + hmac.new(SECRET, notjson, hashlib.sha256).digest()[:T.MAC_LEN], SECRET) is None,
      "signed non-JSON is refused")

print("-- chunking across the wire")
big = T.seal({"viewers": [{"ip": "10.0.%d.%d" % (i // 250, i % 250), "ua": "ua-%d" % i,
                           "n": random.random()} for i in range(150)]}, SECRET, now=50.0)
pieces = T.chunks(big, wire.SHARD_PAYLOAD)
check(len(pieces) > 1, "a bigger snapshot spans several datagrams (%d)" % len(pieces))


def deliver(pieces, gen, loss, seed):
    random.seed(seed)
    asm = T.Reassembler()
    out = []
    for off, piece in pieces:
        for _copy in range(2):                       # each piece is sent twice
            if random.random() < loss:
                continue
            pkt = wire.pack_stats(1, gen, len(big), off, piece)
            _t, o = wire.parse_header(pkt)
            p = wire.unpack_stats(pkt, o)
            b = asm.add(p["generation"], p["total"], p["offset"], p["chunk"])
            if b is not None:
                out.append(b)
    return out


check(wire.parse_header(wire.pack_stats(1, 1, 1, 0, b"x"))[0] == wire.STATS, "STATS has its own packet type")
r = deliver(pieces, 7, 0.0, 1)
check(len(r) == 1 and r[0] == big, "reassembles exactly, and only once despite the duplicate copies")
ok = sum(1 for seed in range(400) if deliver(pieces, 1, 0.2, seed))
check(ok / 400.0 > 0.8, "survives 20%% loss with double-send often enough (%d%% of snapshots)" % (ok / 4))

asm = T.Reassembler()
check(asm.add(1, 10 ** 9, 0, b"x") is None, "an absurd advertised size is ignored")
asm.add(1, len(big), pieces[0][0], pieces[0][1])
got2 = None
for off, piece in pieces:
    got2 = asm.add(2, len(big), off, piece) or got2
check(got2 == big, "a newer generation replaces a half-built older one")

print()
print("%d failed" % len(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
