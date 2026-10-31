"""Bandwidth and load, for the admin page. Stdlib only, Python 3.6 safe.

Two kinds of number, because they answer different questions:

  * NetMeter reads the kernel's interface counters, so it is the machine's
    real traffic whatever generated it -- how close the box is to its limit.
  * RateMeter counts what the application itself moved, so it says which part
    of that traffic is which (ingest, viewers, the UDP tunnel).
"""
import os
import threading
import time
from collections import deque

# Interfaces that only carry a copy of another interface's traffic, or none.
_SKIP = ("lo", "veth", "docker", "br-", "virbr", "cni", "flannel")


class RateMeter(object):
    """Packets and bytes per second over the last few whole seconds."""

    def __init__(self, keep=40):
        self._lock = threading.Lock()
        self._b = {}
        self._keep = keep

    def add(self, nbytes, now=None):
        sec = int(time.time() if now is None else now)
        with self._lock:
            c = self._b.setdefault(sec, [0, 0])
            c[0] += 1
            c[1] += nbytes
            if len(self._b) > self._keep:
                for old in sorted(self._b)[:-self._keep]:
                    del self._b[old]

    def rate(self, span=10, now=None):
        """(events/s, bits/s) over the `span` whole seconds before now."""
        now = int(time.time() if now is None else now)
        with self._lock:
            got = [self._b.get(now - i) for i in range(1, span + 1)]
        ev = sum(c[0] for c in got if c)
        by = sum(c[1] for c in got if c)
        return ev / float(span), by * 8 / float(span)


def read_net_dev(path="/proc/net/dev"):
    """{iface: (rx_bytes, tx_bytes)} for real interfaces."""
    out = {}
    try:
        with open(path) as f:
            lines = f.read().splitlines()[2:]
    except OSError:
        return out
    for line in lines:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name.startswith(_SKIP):
            continue
        f = rest.split()
        if len(f) >= 9:
            out[name] = (int(f[0]), int(f[8]))
    return out


class NetMeter(object):
    """Samples interface counters in the background and reports rates."""

    def __init__(self, path="/proc/net/dev", every=2.0, keep=60):
        self._path = path
        self._every = every
        self._lock = threading.Lock()
        self._s = deque(maxlen=keep)

    def sample(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._s.append((now, read_net_dev(self._path)))

    def start(self):
        self.sample()

        def loop():
            while True:
                time.sleep(self._every)
                self.sample()
        threading.Thread(target=loop, daemon=True).start()

    def rates(self, span=10.0):
        """{"rx_bps", "tx_bps", "ifaces": [...]} over roughly `span` seconds."""
        with self._lock:
            if len(self._s) < 2:
                return {"rx_bps": 0.0, "tx_bps": 0.0, "ifaces": []}
            t1, new = self._s[-1]
            t0, old = self._s[0]
            for t, snap in self._s:
                if t1 - t <= span:
                    t0, old = t, snap
                    break
        dt = max(t1 - t0, 0.001)
        ifaces, rx_all, tx_all = [], 0.0, 0.0
        for name, (rx, tx) in sorted(new.items()):
            if name not in old:
                continue
            # A counter that went backwards was reset (or the box rebooted);
            # report nothing rather than a huge negative or wrapped number.
            drx = max(0, rx - old[name][0])
            dtx = max(0, tx - old[name][1])
            ifaces.append({"name": name, "rx_bps": drx * 8 / dt,
                           "tx_bps": dtx * 8 / dt})
            rx_all += drx * 8 / dt
            tx_all += dtx * 8 / dt
        return {"rx_bps": rx_all, "tx_bps": tx_all, "ifaces": ifaces}


def host_load(meminfo="/proc/meminfo"):
    """Load and memory, which are what a small box runs out of first."""
    out = {"cpus": os.cpu_count() or 1, "load1": 0.0, "mem_used_pct": None}
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    try:
        kv = {}
        with open(meminfo) as f:
            for line in f:
                k, _, v = line.partition(":")
                kv[k] = int(v.split()[0])
        if kv.get("MemTotal") and "MemAvailable" in kv:
            out["mem_used_pct"] = round(
                100.0 * (1 - kv["MemAvailable"] / float(kv["MemTotal"])), 1)
    except (OSError, ValueError, IndexError):
        pass
    return out
