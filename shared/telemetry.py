"""Telemetry that rides the UDP tunnel instead of crossing the border by TCP.

The receiver (China) builds a snapshot of viewers, node load and tunnel
health once every couple of seconds and sends it back to the sender over the
same pinhole HELLO uses. Nothing here opens a TCP connection across the
link, which is the path README measured as unusable.

Snapshot wire form: zlib(JSON) + HMAC-SHA256[:16]. The secret is derived
from the ingest key, so the China box never holds the key itself. Stdlib
only, Python 3.6 safe.
"""
import hashlib
import hmac
import json
import time
import zlib

MAC_LEN = 16
# A decompressed snapshot far larger than this is not ours; refuse it before
# inflating, so a crafted packet cannot balloon memory.
MAX_JSON = 512 * 1024
# A snapshot older than this (by the signer's clock) is a replay.
MAX_AGE = 60.0


def derive_secret(ingest_key):
    """The stats secret for an ingest key. One-way: it does not reveal the key."""
    return hmac.new(ingest_key.encode(), b"krsz-stats-v1", hashlib.sha256).digest()


def seal(doc, secret, now=None):
    doc = dict(doc, t=time.time() if now is None else now)
    raw = zlib.compress(json.dumps(doc, separators=(",", ":")).encode(), 6)
    return raw + hmac.new(secret, raw, hashlib.sha256).digest()[:MAC_LEN]


def open_sealed(blob, secret, now=None):
    """The snapshot dict, or None if it is forged, stale or malformed."""
    if len(blob) <= MAC_LEN:
        return None
    raw, mac = blob[:-MAC_LEN], blob[-MAC_LEN:]
    want = hmac.new(secret, raw, hashlib.sha256).digest()[:MAC_LEN]
    if not hmac.compare_digest(mac, want):
        return None
    try:
        d = zlib.decompressobj()
        text = d.decompress(raw, MAX_JSON)
        if d.unconsumed_tail:
            return None
        doc = json.loads(text.decode())
    except (zlib.error, ValueError):
        return None
    now = time.time() if now is None else now
    if not isinstance(doc, dict) or abs(now - doc.get("t", 0)) > MAX_AGE:
        return None
    return doc


def chunks(blob, size):
    """(offset, piece) pairs covering the blob."""
    return [(o, blob[o:o + size]) for o in range(0, max(len(blob), 1), size)]


class Reassembler(object):
    """Collects the pieces of the newest snapshot generation."""

    def __init__(self):
        self.gen = None
        self.parts = {}
        self.done = None

    def add(self, generation, total, offset, chunk):
        """The complete blob once every piece of a generation has arrived."""
        if total <= 0 or total > 256 * 1024:
            return None
        if generation == self.done:
            return None       # the second copy of a piece we already used
        if generation != self.gen:
            self.gen, self.parts = generation, {}
        self.parts[offset] = chunk
        if sum(len(c) for c in self.parts.values()) < total:
            return None
        blob = b"".join(self.parts[o] for o in sorted(self.parts))[:total]
        self.parts = {}
        self.gen = None
        self.done = generation
        return blob if len(blob) == total else None
