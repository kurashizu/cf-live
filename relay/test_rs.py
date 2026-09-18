#!/usr/bin/env python3
"""Tests for the GF(256) erasure codec.

The codec is the foundation of the relay: if it silently corrupts a shard,
the failure surfaces as unplayable video far from the cause. So these check
exact byte equality under every loss pattern the link can produce, not just
that decoding returns something.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rs import Codec, gmul, gdiv, ginv  # noqa: E402

fails = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


print("-- GF(256) field axioms")
check(all(gmul(a, 1) == a for a in range(256)), "1 is the multiplicative identity")
check(all(gmul(a, 0) == 0 for a in range(256)), "0 annihilates")
check(all(gmul(a, b) == gmul(b, a) for a in range(0, 256, 17)
          for b in range(0, 256, 13)), "multiplication commutes")
check(all(gmul(a, ginv(a)) == 1 for a in range(1, 256)), "every nonzero has an inverse")
check(all(gdiv(gmul(a, b), b) == a for a in range(0, 256, 7)
          for b in range(1, 256, 11)), "division undoes multiplication")

print("-- systematic property")
c = Codec(10, 20)
check(all(c.matrix[i][j] == (1 if i == j else 0)
          for i in range(10) for j in range(10)),
      "top k x k block is the identity, so data shards pass through")

print("-- exhaustive recovery, RS(4,8)")
c48 = Codec(4, 8)
data = [bytes([i] * 32) for i in range(4)]
allsh = data + c48.encode(data)
worst = 0
for mask in range(256):
    lost = [i for i in range(8) if mask >> i & 1]
    sh = [None if i in lost else allsh[i] for i in range(8)]
    if len(lost) <= 4:
        try:
            ok = c48.decode(sh) == data
        except ValueError:
            ok = False
        if not ok:
            worst += 1
check(worst == 0, "every loss pattern of <= 4 of 8 shards recovers exactly")

print("-- refuses the impossible")
refused = 0
for mask in range(256):
    lost = [i for i in range(8) if mask >> i & 1]
    if len(lost) <= 4:
        continue
    sh = [None if i in lost else allsh[i] for i in range(8)]
    try:
        c48.decode(sh)
    except ValueError:
        refused += 1
    else:
        refused = -999
check(refused > 0, "raises rather than returning garbage when too few survive")

print("-- randomised, RS(10,20), binary-safe payloads")
c = Codec(10, 20)
bad = 0
for _ in range(400):
    data = [os.urandom(1200) for _ in range(10)]
    allsh = data + c.encode(data)
    lost = random.sample(range(20), random.randint(0, 10))
    sh = [None if i in lost else allsh[i] for i in range(20)]
    if c.decode(sh) != data:
        bad += 1
check(bad == 0, "400 random patterns recover byte-exact")

print("-- the measured loss profile")
# Observed on the real link: 16.5% loss, runs 1:144 2:18 3:2 4:3, max 4.
random.seed(7)
unrecoverable = 0
blocks = 2000
for _ in range(blocks):
    lost = set()
    i = 0
    while i < 20:
        if random.random() < 0.165:
            run = random.choices([1, 2, 3, 4], weights=[144, 18, 2, 3])[0]
            for j in range(run):
                if i + j < 20:
                    lost.add(i + j)
            i += run
        else:
            i += 1
    if len(lost) > 10:
        unrecoverable += 1
rate = unrecoverable * 100.0 / blocks
print("        simulated unrecoverable blocks: %.2f%%" % rate)
check(rate < 1.0, "under 1%% of blocks unrecoverable at the measured profile")

print("-- shard length handling")
try:
    Codec(3, 6).encode([b"aa", b"bbb", b"cc"])
    check(False, "rejects unequal shard lengths")
except ValueError:
    check(True, "rejects unequal shard lengths")
check(Codec(2, 4).encode([b"", b""]) == [b"", b""], "empty shards are legal")
try:
    Codec(5, 5)
    check(False, "rejects k == n")
except ValueError:
    check(True, "rejects k == n")

print()
if fails:
    print("FAILED: " + "; ".join(fails))
    sys.exit(1)
print("all codec tests passed")
