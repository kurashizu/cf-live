#!/usr/bin/env python3
"""Tests for the bandwidth meters."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sysstats import NetMeter, RateMeter, host_load, read_net_dev  # noqa: E402

fails = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


HEAD = ("Inter-|   Receive                            |  Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast"
        "|bytes packets errs drop fifo colls carrier compressed\n")


def dev(eth_rx, eth_tx, lo=999999):
    return (HEAD +
            "    lo: %d 1 0 0 0 0 0 0 %d 1 0 0 0 0 0 0\n" % (lo, lo) +
            "  eth0: %d 1 0 0 0 0 0 0 %d 1 0 0 0 0 0 0\n" % (eth_rx, eth_tx) +
            "docker0: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n")


print("-- interface counters")
tmp = tempfile.NamedTemporaryFile("w", delete=False)
tmp.write(dev(1000, 2000)); tmp.close()
d = read_net_dev(tmp.name)
check(d == {"eth0": (1000, 2000)}, "loopback and bridges are left out")
check(read_net_dev("/nonexistent") == {}, "a missing file is an empty reading")

m = NetMeter(path=tmp.name)
m.sample(now=100.0)
open(tmp.name, "w").write(dev(1000 + 125000, 2000 + 250000))
m.sample(now=101.0)
r = m.rates(span=10)
check(abs(r["rx_bps"] - 1_000_000) < 1, "125000 bytes in 1s is 1 Mbit/s down")
check(abs(r["tx_bps"] - 2_000_000) < 1, "250000 bytes in 1s is 2 Mbit/s up")
open(tmp.name, "w").write(dev(10, 10))
m.sample(now=102.0)
r = m.rates(span=1.5)
check(r["rx_bps"] == 0 and r["tx_bps"] == 0, "a counter reset reads as zero, not negative")
check(NetMeter(path=tmp.name).rates()["rx_bps"] == 0, "no samples yet reads as zero")
os.unlink(tmp.name)

print("-- application meter")
rm = RateMeter()
for i in range(10):
    rm.add(1250, now=200.0 + i)
pps, bps = rm.rate(span=10, now=210.0)
check(abs(pps - 1.0) < 1e-9 and abs(bps - 10000) < 1e-6, "10 x 1250 B over 10s is 10 kbit/s")
rm.add(10 ** 6, now=500.0)
check(len(rm._b) <= 41, "old seconds are dropped")

print("-- host load")
h = host_load()
check(h["cpus"] >= 1 and h["load1"] >= 0, "reports cpus and load")

print()
print("%d failed" % len(fails) if fails else "all passed")
sys.exit(1 if fails else 0)
