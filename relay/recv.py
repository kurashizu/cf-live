#!/usr/bin/env python3
"""KRSZ Live relay receiver: China side.

Runs where the viewers are. Opens a UDP flow to the sender (it must
initiate, because the security group in front of it drops unsolicited
inbound but permits return traffic on a flow it started), reassembles FEC
blocks into HLS segments, and serves plain HLS over HTTP locally.

Design follows the measured link, not general principles:

  RTT 280 ms, jitter 1-4 ms   -> a small jitter buffer suffices
  loss 16-23%, isolated       -> forward correction, never retransmission
  TCP connect jitter 730 ms   -> nothing here may depend on a TCP round trip

A block is playable as soon as any k of its n shards land, so a lost shard
costs nothing rather than a 281 ms recovery round trip. Blocks that lose
more than n-k shards are dropped outright: the playlist simply never
advertises that segment, which a player handles by moving on, whereas
waiting for it would stall the stream.
"""

import argparse
import collections
import binascii
import json
import math
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from http.server import ThreadingHTTPServer
except ImportError:
    # Python 3.6 (Alibaba Cloud Linux 3 ships 3.6.8) has no
    # ThreadingHTTPServer. Compose it the same way 3.7+ does: a slow or
    # stalled viewer must not block the others, and segments are served
    # concurrently.
    import socketserver

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Shared with the origin; a deploy copies viewers.py and sysstats.py alongside.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "shared"))
import wire
from page import PAGE
from rs import Codec
from sysstats import NetMeter, RateMeter, host_load
import telemetry
from viewers import ViewerLog

M3U8 = "application/vnd.apple.mpegurl"
TS_TYPE = "video/MP2T"

CONFIG = {"window": 3, "stats_secret": b""}

VIEWERS = ViewerLog()
NET = NetMeter()

# Offset applied to the published media sequence so it never decreases, even
# when the sender restarts and renumbers from zero.
SEQ_GUARD = {"offset": 0, "last_in": -1, "last_out": -1}

STATE = {
    "rate": RateMeter(),        # UDP arriving from the sender
    "http_tx": RateMeter(),     # HTTP leaving to viewers
    "asm": None,
    "segments": collections.OrderedDict(),  # name -> bytes
    "playlist": b"",
    "lock": threading.Lock(),
    "stats": collections.Counter(),
    "last_packet": 0.0,
    "started": time.time(),
}


class Reassembler(object):
    """Collect shards into blocks, and blocks into segments.

    Held per segment sequence. A segment is emitted once every one of its
    blocks has decoded, and abandoned if it is still incomplete when the
    window moves past it -- late data is worthless for live playback, and
    keeping it would grow without bound.
    """

    def __init__(self, keep_segments, log):
        self.blocks = {}      # (seq, block) -> {"shards": [...], "k","n","len"}
        self.segments = {}    # seq -> {block -> bytes}
        self.expected = {}    # seq -> total block count, once known
        self.done = collections.OrderedDict()
        self.keep = keep_segments
        self.codecs = {}
        self.log = log
        # Per-segment link accounting, kept apart from the decode state so
        # that shards arriving after a segment completes still count. Loss is
        # only meaningful over a whole segment, and a segment is judged once
        # it has fallen out of the live window, when nothing more can arrive.
        self.acct = {}
        self.history = collections.deque(maxlen=120)

    def codec(self, k, n):
        key = (k, n)
        c = self.codecs.get(key)
        if c is None:
            c = Codec(k, n)
            self.codecs[key] = c
        return c

    def add_shard(self, p):
        seq, blk, idx = p["seq"], p["block"], p["shard"]
        k, n = p["k"], p["n"]
        if not 0 < k < n <= 255 or not 0 <= idx < n:
            STATE["stats"]["bad_header"] += 1
            return None
        # Every shard carries the segment's block count, so a segment can
        # complete no matter which packets were lost.
        total = p.get("blocks")
        if total:
            self.expected[seq] = total
        a = self.acct.get(seq)
        if a is None:
            a = self.acct[seq] = {"rx": 0, "exp": 0, "blocks": 0, "k": k,
                                  "n": n, "first": time.time(), "done": None,
                                  "blk": {}}
        a["rx"] += 1
        if total:
            a["exp"] = total * n
            a["blocks"] = total
        a["blk"][blk] = a["blk"].get(blk, 0) + 1
        key = (seq, blk)
        rec = self.blocks.get(key)
        if rec is None:
            if seq in self.done:
                return None      # already emitted; a straggler
            rec = {"shards": [None] * n, "k": k, "n": n,
                   "len": p["block_len"], "decoded": False}
            self.blocks[key] = rec
        if rec["decoded"]:
            return None
        payload = p["payload"]
        if rec["shards"][idx] is None:
            rec["shards"][idx] = payload
        have = sum(1 for s in rec["shards"] if s is not None)
        if have < k:
            return None
        # Enough shards: recover the block. A block that arrived with every
        # data shard needed no parity; one missing some was saved by it.
        missing = sum(1 for sh in rec["shards"][:k] if sh is None)
        try:
            data = self.codec(k, n).decode(rec["shards"])
        except ValueError:
            return None
        except Exception as e:
            STATE["stats"]["decode_error"] += 1
            return None
        rec["decoded"] = True
        rec["shards"] = None          # free the shards immediately
        blob = b"".join(data)[:rec["len"]]
        STATE["stats"]["blocks_ok"] += 1
        if missing:
            STATE["stats"]["blocks_recovered"] += 1
            STATE["stats"]["data_shards_recovered"] += missing
        self.segments.setdefault(seq, {})[blk] = blob
        return self.try_complete(seq)

    def note_total(self, seq, total):
        self.expected[seq] = total
        return self.try_complete(seq)

    def try_complete(self, seq):
        total = self.expected.get(seq)
        if total is None:
            return None
        parts = self.segments.get(seq)
        if not parts or len(parts) < total:
            return None
        if any(i not in parts for i in range(total)):
            return None
        body = b"".join(parts[i] for i in range(total))
        a = self.acct.get(seq)
        if a is not None and a["done"] is None:
            a["done"] = time.time() - a["first"]
        self.cleanup(seq)
        self.done[seq] = True
        while len(self.done) > self.keep * 4:
            self.done.popitem(last=False)
        return seq, body

    def cleanup(self, seq):
        self.segments.pop(seq, None)
        self.expected.pop(seq, None)
        for key in [k for k in self.blocks if k[0] == seq]:
            self.blocks.pop(key, None)

    def finalize(self, below_seq):
        """Judge segments that have left the window: nothing more can arrive."""
        for seq in [s for s in list(self.acct) if s < below_seq]:
            a = self.acct.pop(seq)
            if not a["exp"]:
                continue
            worst = max((a["n"] - a["blk"].get(b, 0)
                         for b in range(a["blocks"])), default=0)
            self.history.append({
                "rx": a["rx"], "exp": a["exp"],
                "loss": round(1.0 - a["rx"] / float(a["exp"]), 4),
                "worst_lost": worst, "parity": a["n"] - a["k"],
                "recovery_ms": (round(a["done"] * 1000)
                                if a["done"] is not None else None),
                "ok": a["done"] is not None,
            })

    def expire(self, below_seq):
        """Abandon segments older than the live window."""
        self.finalize(below_seq)
        for seq in [s for s in list(self.segments) if s < below_seq]:
            STATE["stats"]["segments_abandoned"] += 1
            self.cleanup(seq)
        for key in [k for k in list(self.blocks) if k[0] < below_seq]:
            self.blocks.pop(key, None)


def receiver_loop(args):
    codec_log = []
    asm = Reassembler(args.keep, codec_log)
    STATE["asm"] = asm
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    sock.bind(("0.0.0.0", args.local_port))
    sock.settimeout(1.0)
    dst = (args.sender, args.sender_port)
    nonce = struct.unpack("!I", os.urandom(4))[0]

    manifest_parts = {}
    manifest_gen = -1
    last_hello = 0.0
    last_stats = 0.0
    stats_gen = 0
    highest_seq = 0

    print("receiver: %s:%d -> serving on :%d" %
          (args.sender, args.sender_port, args.http_port), flush=True)

    while True:
        now = time.time()
        # Keep the pinhole open. Measured: the flow survives as long as
        # traffic continues, and a 62 s run with 5 s keepalives held fine.
        if now - last_hello >= args.hello_interval:
            try:
                sock.sendto(wire.pack_hello(args.stream_id, nonce), dst)
            except OSError:
                pass
            last_hello = now

        # Telemetry goes back over the tunnel's own pinhole, not a TCP
        # connection across the border. Each piece is sent twice, as
        # playlists are, because a snapshot is useless unless all of it lands
        # and the next one is only seconds away.
        if CONFIG["stats_secret"] and now - last_stats >= args.stats_interval:
            last_stats = now
            try:
                blob = telemetry.seal(telemetry_snapshot(), CONFIG["stats_secret"])
                stats_gen += 1
                for off, piece in telemetry.chunks(blob, wire.SHARD_PAYLOAD):
                    pkt = wire.pack_stats(args.stream_id, stats_gen,
                                          len(blob), off, piece)
                    sock.sendto(pkt, dst)
                    sock.sendto(pkt, dst)
            except Exception:
                STATE["stats"]["telemetry_error"] += 1

        try:
            buf, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            continue

        ptype, off = wire.parse_header(buf)
        if ptype is None:
            STATE["stats"]["foreign"] += 1
            continue
        STATE["last_packet"] = now
        STATE["stats"]["packets"] += 1
        STATE["rate"].add(len(buf), now)

        if ptype == wire.SHARD:
            p = wire.unpack_shard(buf, off)
            if p["stream_id"] != args.stream_id:
                continue
            if p["seq"] > highest_seq:
                highest_seq = p["seq"]
                # A segment's shards all land within a second or so, so four
                # segments on it is settled: judge its loss now rather than
                # when it finally leaves the window, which would leave the
                # admin page empty for the first half-minute of a broadcast.
                asm.finalize(highest_seq - 4)
                asm.expire(highest_seq - args.keep * 2)
            out = asm.add_shard(p)
            if out:
                seq, body = out
                name = "seg%05d.ts" % (seq % 100000)
                with STATE["lock"]:
                    STATE["segments"][name] = body
                    while len(STATE["segments"]) > args.keep:
                        STATE["segments"].popitem(last=False)
                STATE["stats"]["segments_ok"] += 1

        elif ptype == wire.MANIFEST:
            p = wire.unpack_manifest(buf, off)
            if p["stream_id"] != args.stream_id:
                continue
            if p["generation"] != manifest_gen:
                manifest_gen = p["generation"]
                manifest_parts = {}
            manifest_parts[p["offset"]] = p["chunk"]
            got = sum(len(c) for c in manifest_parts.values())
            if got >= p["total"]:
                body = b"".join(manifest_parts[o]
                                for o in sorted(manifest_parts))[:p["total"]]
                with STATE["lock"]:
                    STATE["playlist"] = body
                STATE["stats"]["manifests"] += 1

        elif ptype == wire.BYE:
            STATE["stats"]["bye"] += 1


def monotonic_seq(raw):
    """Map the sender's sequence onto one that never goes backwards.

    A restarted sender renumbers from zero. Publishing that verbatim reads
    to a player as a different stream, and it responds by never loading
    another fragment -- a permanent stall, not a glitch.
    """
    g = SEQ_GUARD
    if g["last_in"] >= 0 and raw < g["last_in"]:
        # Resume just past whatever viewers were last shown.
        g["offset"] = g["last_out"] + 1 - raw
    g["last_in"] = raw
    g["last_out"] = raw + g["offset"]
    return g["last_out"]


def available_playlist(body, have, window=0):
    """Trim a playlist to the fragments this relay actually holds.

    The sender advertises a segment once it has sent the shards; recovery
    here lags that by however long the missing shards take to arrive. Any
    fragment still in flight is dropped from the tail, and EXT-X-MEDIA-
    SEQUENCE is advanced by however many were dropped from the *front*, so a
    player's idea of the live edge stays consistent with what it can fetch.

    Returns None when nothing is available, so the caller can answer 503
    rather than publish an empty playlist -- an empty one reads as a stream
    that exists but has no content, and players handle it poorly.
    """
    header, entries = [], []
    pending = None
    pending_disc = False
    seq = 0
    longest = 0.0
    for line in body.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1])
            except ValueError:
                pass
            header.append(line)
        elif line == "#EXT-X-DISCONTINUITY":
            # Belongs to the fragment that follows, not the header. Treating
            # it as a header line hoisted every marker to the top of the
            # playlist, which tells a player the break happens somewhere it
            # does not and leaves the real breaks unmarked.
            pending_disc = True
        elif line.startswith("#EXTINF:"):
            pending = line
            try:
                longest = max(longest,
                              float(line.split(":", 1)[1].split(",")[0]))
            except ValueError:
                pass
        elif line and not line.startswith("#"):
            entries.append((pending_disc, pending, line))
            pending = None
            pending_disc = False
        elif line:
            header.append(line)

    kept = [e for e in entries if e[2] in have]
    if not kept:
        return None
    # The window is the origin's, not trimmed here. The origin already
    # advertises only a few entries, and when it advertises more it is on
    # purpose: across a slate -> live seam it holds the slate entries a few
    # seconds so a player still polling at the slate cadence finds an entry
    # it knows in every playlist. Cutting that back to the newest few
    # reintroduced the failure on this side: two consecutive polls with no
    # entry in common, and the player guessing the gap with the wrong
    # segment length. `window` is accepted for compatibility and ignored.
    # The sequence must match the first fragment actually published, whether
    # it was dropped for being unrecovered or for being outside the window.
    first_uri = kept[0][2]
    dropped_front = 0
    for _disc, _inf, uri in entries:
        if uri == first_uri:
            break
        dropped_front += 1

    published = monotonic_seq(seq + dropped_front)
    # Every discontinuity tag dropped from the front is one more timeline
    # that has scrolled out of view, and EXT-X-DISCONTINUITY-SEQUENCE has to
    # say so (RFC 8216 6.2.2) or the player numbers the remaining timelines
    # one too low and applies a stale timestamp offset to the wrong media.
    dropped_discs = sum(1 for disc, _inf, _uri in entries[:dropped_front] if disc)
    out = []
    for line in header:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            out.append("#EXT-X-MEDIA-SEQUENCE:%d" % published)
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            try:
                base = int(line.split(":", 1)[1])
            except ValueError:
                base = 0
            out.append("#EXT-X-DISCONTINUITY-SEQUENCE:%d" % (base + dropped_discs))
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            # ffmpeg rounds to an integer and writes 0 for sub-second
            # segments. RFC 8216 requires a positive integer that is at
            # least the longest EXTINF, and a 0 makes players reject the
            # playlist outright. Rounded up, not to nearest: 2.133 s
            # entries need a 3, and round() would say 2.
            out.append("#EXT-X-TARGETDURATION:%d" % max(1, int(math.ceil(longest))))
        else:
            out.append(line)
    for disc, inf, uri in kept:
        if disc:
            out.append("#EXT-X-DISCONTINUITY")
        if inf:
            out.append(inf)
        out.append(uri)
    return ("\n".join(out) + "\n").encode()


def _pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))] if values else None


def tunnel_overview():
    """How the FEC tunnel is doing, measured where it can be: here.

    The sender has no feedback channel, so only the receiving end knows how
    many shards actually arrived. Everything is over the last 60 judged
    segments, which is about a minute of broadcast.
    """
    asm = STATE["asm"]
    hist = list(asm.history)[-60:] if asm else []
    rx = sum(h["rx"] for h in hist)
    exp = sum(h["exp"] for h in hist)
    parity = hist[-1]["parity"] if hist else 0
    worst = max((h["worst_lost"] for h in hist), default=0)
    rec = [h["recovery_ms"] for h in hist if h["recovery_ms"] is not None]
    st = STATE["stats"]
    pps, bps = STATE["rate"].rate()
    age = time.time() - STATE["last_packet"] if STATE["last_packet"] else None
    return {
        "since_last_packet": None if age is None else round(age, 2),
        "uptime": round(time.time() - STATE["started"]),
        "rx_pps": round(pps, 1), "rx_bps": round(bps),
        "segments_judged": len(hist),
        "loss_pct": round(100.0 * (1 - rx / float(exp)), 2) if exp else None,
        # Parity shards per block minus the most any one block lost: how many
        # more could have gone missing before a segment failed to decode.
        "parity": parity, "worst_block_lost": worst,
        "headroom": (parity - worst) if hist else None,
        "recovery_ms": {"p50": _pct(rec, 0.5), "p95": _pct(rec, 0.95),
                        "max": max(rec) if rec else None},
        "segments_ok": st["segments_ok"],
        "segments_abandoned": st["segments_abandoned"],
        "segments_failed_pct": round(
            100.0 * sum(1 for h in hist if not h["ok"]) / len(hist), 2) if hist else None,
        "blocks_ok": st["blocks_ok"],
        "blocks_recovered": st["blocks_recovered"],
        "data_shards_recovered": st["data_shards_recovered"],
        "history": [{"loss": h["loss"], "worst": h["worst_lost"],
                     "ms": h["recovery_ms"], "ok": h["ok"]} for h in hist],
    }


def telemetry_snapshot():
    """Everything the admin page wants from this side, in one document."""
    _pps, http_bps = STATE["http_tx"].rate()
    _pps, udp_bps = STATE["rate"].rate()
    return {
        "viewers": VIEWERS.snapshot()[:150],
        "tunnel": tunnel_overview(),
        "node": {"nic": NET.rates(), "load": host_load(),
                 "app": {"udp_rx_bps": round(udp_bps),
                         "http_tx_bps": round(http_bps)}},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def send_payload(self, body, ctype, cache):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            STATE["http_tx"].add(len(body))
        track = getattr(self, "_track", None)
        if track:
            self._track = None
            VIEWERS.record(self.client_ip(), self.headers.get("User-Agent", ""),
                           "cn", self.headers.get("X-Krsz-Via") or "direct",
                           track[1], track[0],
                           0 if self.command == "HEAD" else len(body))

    def client_ip(self):
        """The viewer's address. Both fronts (nginx, tlsproxy) overwrite
        X-Real-IP with the peer they saw, and this port is loopback only, so
        the header cannot be forged by a viewer."""
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def fail(self, code, text="not found\n"):
        b = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self._track = None      # a kept-alive connection reuses this object


        if path in ("/healthz", "/health"):
            self.send_payload(b"ok\n", "text/plain", "no-store")
            return

        if path == "/stats":
            import json
            with STATE["lock"]:
                segs = len(STATE["segments"])
                pl = len(STATE["playlist"])
            age = time.time() - STATE["last_packet"] if STATE["last_packet"] else -1
            body = json.dumps({
                "segmentsBuffered": segs,
                "playlistBytes": pl,
                "secondsSinceLastPacket": round(age, 2),
                "uptimeSeconds": round(time.time() - STATE["started"], 1),
                "counters": dict(STATE["stats"]),
            }, indent=1).encode()
            self.send_payload(body, "application/json", "no-store")
            return

        # This host's own page. Not the origin's: that one documents an
        # ingest endpoint and OBS settings which do not exist here.
        if path in ("/", "/index.html"):
            self.send_payload(PAGE.encode("utf-8"),
                              "text/html; charset=utf-8", "no-store")
            return

        # Playlist. Never cached: a player given identical bytes concludes
        # there is nothing new and stops fetching fragments.
        if path.endswith(".m3u8") or path == "/main":
            with STATE["lock"]:
                body = STATE["playlist"]
                have = set(STATE["segments"])
            if not body:
                self.fail(503, "no stream yet\n")
                return
            body = available_playlist(body, have, CONFIG["window"])
            if body is None:
                self.fail(503, "no fragments recovered yet\n")
                return
            self._track = ("playlist", "main")
            self.send_payload(body, M3U8, "no-store")
            return

        name = path.rsplit("/", 1)[-1]
        if name.endswith(".ts"):
            with STATE["lock"]:
                body = STATE["segments"].get(name)
            if body is None:
                self.fail(404)
                return
            # Segments are immutable once recovered.
            self._track = ("segment", "main")
            self.send_payload(body, TS_TYPE, "public, max-age=30")
            return

        self.fail(404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sender", required=True, help="VPS public IP")
    ap.add_argument("--sender-port", type=int, default=39997)
    ap.add_argument("--local-port", type=int, default=45010,
                    help="local UDP port; the pinhole is opened from here")
    ap.add_argument("--http-port", type=int, default=9990)
    ap.add_argument("--stream-id", type=int, default=1)
    ap.add_argument("--window", type=int, default=3,
                    help="fragments to advertise; a player starts at the "
                         "oldest, so this is the dominant latency term")
    ap.add_argument("--keep", type=int, default=12,
                    help="segments to retain for viewers")
    ap.add_argument("--hello-interval", type=float, default=2.0,
                    help="pinhole refresh; must stay below the security "
                         "group's UDP idle timeout")
    ap.add_argument("--stats-secret", default=os.environ.get("KRSZ_STATS_SECRET", ""),
                    help="hex secret that signs telemetry sent back to the "
                         "sender (also read from KRSZ_STATS_SECRET). Derived "
                         "from the ingest key, so this box never holds the key; "
                         "empty disables telemetry")
    ap.add_argument("--stats-interval", type=float, default=2.0,
                    help="seconds between telemetry snapshots")
    args = ap.parse_args()

    CONFIG["window"] = args.window
    secret = args.stats_secret.strip()
    CONFIG["stats_secret"] = binascii.unhexlify(secret) if secret else b""
    NET.start()

    t = threading.Thread(target=receiver_loop, args=(args,), daemon=True)
    t.start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    srv.daemon_threads = True
    print("http on 127.0.0.1:%d" % args.http_port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
