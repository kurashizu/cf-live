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
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wire
from rs import Codec

M3U8 = "application/vnd.apple.mpegurl"
TS_TYPE = "video/MP2T"

STATE = {
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
        # Enough shards: recover the block.
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

    def expire(self, below_seq):
        """Abandon segments older than the live window."""
        for seq in [s for s in list(self.segments) if s < below_seq]:
            STATE["stats"]["segments_abandoned"] += 1
            self.cleanup(seq)
        for key in [k for k in list(self.blocks) if k[0] < below_seq]:
            self.blocks.pop(key, None)


def receiver_loop(args):
    codec_log = []
    asm = Reassembler(args.keep, codec_log)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    sock.bind(("0.0.0.0", args.local_port))
    sock.settimeout(1.0)
    dst = (args.sender, args.sender_port)
    nonce = struct.unpack("!I", os.urandom(4))[0]

    manifest_parts = {}
    manifest_gen = -1
    last_hello = 0.0
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

        if ptype == wire.SHARD:
            p = wire.unpack_shard(buf, off)
            if p["stream_id"] != args.stream_id:
                continue
            if p["seq"] > highest_seq:
                highest_seq = p["seq"]
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

        # Playlist. Never cached: a player given identical bytes concludes
        # there is nothing new and stops fetching fragments.
        if path.endswith(".m3u8") or path in ("/", "/main"):
            with STATE["lock"]:
                body = STATE["playlist"]
            if not body:
                self.fail(503, "no stream yet\n")
                return
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
    ap.add_argument("--keep", type=int, default=12,
                    help="segments to retain for viewers")
    ap.add_argument("--hello-interval", type=float, default=2.0,
                    help="pinhole refresh; must stay below the security "
                         "group's UDP idle timeout")
    args = ap.parse_args()

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
