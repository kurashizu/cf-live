#!/usr/bin/env python3
"""KRSZ Live relay sender: VPS side.

Reads the HLS output the existing relay already writes to tmpfs, encodes
each segment into FEC blocks, and pushes them over UDP to whichever receiver
has said HELLO.

It does not initiate. The receiver's network drops unsolicited inbound UDP
but allows return traffic on a flow the receiver opened, so this side waits
to learn the peer address and then streams into the pinhole the receiver
keeps alive.

Parity is sent for every block whether or not it is needed, because there is
no feedback channel worth using: a NACK would cost a 281 ms round trip, by
which time the segment has left the live window. Bandwidth is the cheap
resource on this link and latency is the scarce one, so the trade is
deliberate.
"""

import argparse
import collections
import os
import re
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wire
from rs import Codec

PEERS = {}          # addr -> last HELLO time
PEERS_LOCK = threading.Lock()
STATS = collections.Counter()


def hello_listener(sock, stream_id, ttl):
    """Track which receivers are currently asking for the stream."""
    while True:
        try:
            buf, addr = sock.recvfrom(2048)
        except OSError:
            time.sleep(0.1)
            continue
        ptype, off = wire.parse_header(buf)
        if ptype != wire.HELLO:
            continue
        p = wire.unpack_hello(buf, off)
        if p["stream_id"] != stream_id:
            continue
        with PEERS_LOCK:
            new = addr not in PEERS
            PEERS[addr] = time.time()
        if new:
            print("peer joined %s:%d" % addr, flush=True)
        STATS["hellos"] += 1


def live_peers(ttl):
    now = time.time()
    with PEERS_LOCK:
        for addr in [a for a, t in PEERS.items() if now - t > ttl]:
            PEERS.pop(addr, None)
            print("peer timed out %s:%d" % addr, flush=True)
        return list(PEERS.keys())


def send_segment(sock, peers, stream_id, seq, data, k, n, codec, pace):
    """Split one segment into FEC blocks and transmit every shard.

    Shards of a block are interleaved across the send order so that a short
    burst of loss lands in different blocks rather than wiping one block's
    parity. The measured loss is mostly isolated singles, but runs of up to
    4 were observed, and interleaving costs nothing.
    """
    block_size = k * wire.SHARD_PAYLOAD
    blocks = [data[i:i + block_size]
              for i in range(0, len(data), block_size)] or [b""]
    packets = []
    for bi, blob in enumerate(blocks):
        shards, real_len = wire.shards_for(blob, k)
        parity = codec.encode(shards)
        allsh = shards + parity
        for si, sh in enumerate(allsh):
            packets.append(wire.pack_shard(stream_id, seq, bi, len(blocks),
                                           si, k, n, real_len, sh))
    # Interleave: send shard i of every block before shard i+1 of any.
    per_block = n
    ordered = []
    for si in range(per_block):
        for bi in range(len(blocks)):
            idx = bi * per_block + si
            if idx < len(packets):
                ordered.append(packets[idx])

    sent = 0
    gap = pace / max(len(ordered), 1) if pace > 0 else 0
    for pkt in ordered:
        for addr in peers:
            try:
                sock.sendto(pkt, addr)
                sent += 1
            except OSError:
                STATS["send_error"] += 1
        if gap:
            time.sleep(gap)
    STATS["shards_sent"] += sent
    STATS["blocks_sent"] += len(blocks)
    return len(blocks)


def send_manifest(sock, peers, stream_id, gen, body):
    chunk = wire.SHARD_PAYLOAD
    total = len(body)
    for off in range(0, max(total, 1), chunk):
        pkt = wire.pack_manifest(stream_id, gen, total, off,
                                 body[off:off + chunk])
        # Playlists are small and matter more than any single segment, so
        # send each piece twice rather than FEC-coding them. At ~20% loss the
        # chance both copies vanish is ~4%, and the next poll carries a fresh
        # one a second later.
        for _ in range(2):
            for addr in peers:
                try:
                    sock.sendto(pkt, addr)
                except OSError:
                    STATS["send_error"] += 1
    STATS["manifests_sent"] += 1


def rewrite_playlist(body, mapping):
    """Point playlist entries at the names the receiver will serve.

    The receiver names recovered segments by sequence, so the playlist it
    publishes has to use those names rather than the origin's. Slate entries
    are root-absolute and belong to the origin, so they are dropped: the
    receiver has no copy of them, and advertising an unfetchable URI would
    stall a player.
    """
    out = []
    pending = None
    pending_disc = False
    for line in body.decode("utf-8", "replace").splitlines():
        s = line.strip()
        if s == "#EXT-X-DISCONTINUITY":
            # Held until its fragment is known to be relayed: emitting it
            # before a dropped entry would apply the break to the wrong
            # fragment.
            pending_disc = True
            continue
        if s.startswith("#EXTINF:"):
            pending = s
            continue
        if s and not s.startswith("#"):
            new = mapping.get(s)
            if new is None:
                pending = None
                continue
            if pending_disc:
                out.append("#EXT-X-DISCONTINUITY")
                pending_disc = False
            if pending:
                out.append(pending)
                pending = None
            out.append(new)
            continue
        if pending:
            out.append(pending)
            pending = None
        out.append(s)
    return ("\n".join(out) + "\n").encode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/dev/shm/cf-live")
    ap.add_argument("--stream", default="main")
    ap.add_argument("--port", type=int, default=39997)
    ap.add_argument("--stream-id", type=int, default=1)
    ap.add_argument("--k", type=int, default=40)
    ap.add_argument("--n", type=int, default=80,
                    help="total shards; n-k parity. Loss on this link is "
                         "16-23%% in isolated singles. RS(40,80) keeps the "
                         "same 100%% redundancy as a smaller block but puts "
                         "the failure threshold 5.9 sigma out instead of "
                         "4.3, because a larger sample deviates less: "
                         "simulated segment loss falls from 3.7%% to under "
                         "0.001%% at the worst measured loss rate.")
    ap.add_argument("--origin", default="http://127.0.0.1:9999",
                    help="where to read the continuous playlist from; the "
                         "local relay, not the public hostname")
    ap.add_argument("--peer-ttl", type=float, default=15.0)
    ap.add_argument("--pace", type=float, default=0.25,
                    help="seconds to spread one segment's shards over; "
                         "smoothing the burst avoids queue drops")
    args = ap.parse_args()

    if not 0 < args.k < args.n <= 255:
        sys.exit("need 0 < k < n <= 255")

    codec = Codec(args.k, args.n)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    sock.bind(("0.0.0.0", args.port))
    print("sender on :%d  RS(%d,%d)  stream=%s" %
          (args.port, args.k, args.n, args.stream), flush=True)

    threading.Thread(target=hello_listener,
                     args=(sock, args.stream_id, args.peer_ttl),
                     daemon=True).start()

    d = Path(args.root) / args.stream
    slate_dir = Path(args.root) / "offline"
    # The continuous playlist is generated per request, not stored: when
    # nobody is broadcasting there is no live.m3u8 on disk at all. Reading
    # the file would therefore relay nothing during exactly the period the
    # slate exists to cover, so fetch the served playlist over HTTP instead.
    # It is the one that splices slate and live into a single timeline whose
    # sequence never rewinds.
    pl_url = "%s/live/%s/index.m3u8" % (args.origin.rstrip("/"), args.stream)
    sent_names = collections.OrderedDict()   # origin name -> relay name
    # The relay's own monotonic counter. Neither source can supply this: the
    # encoder restarts at zero on every relaunch, and slate URIs rotate over
    # a small set, so both would rewind a viewer's timeline and force the
    # player to rebuild -- the stall this whole path exists to avoid.
    next_seq = 0
    gen = 0
    last_pl = b""

    while True:
        peers = live_peers(args.peer_ttl)
        if not peers:
            time.sleep(0.5)
            continue
        try:
            req = urllib.request.Request(pl_url)
            req.add_header("User-Agent", "krsz-send/1")
            req.add_header("Cache-Control", "no-cache")
            f = urllib.request.urlopen(req, timeout=5)
            try:
                body = f.read()
            finally:
                f.close()
        except Exception:
            STATS["playlist_error"] += 1
            time.sleep(0.5)
            continue

        names = [l.strip() for l in body.decode("utf-8", "replace").splitlines()
                 if l.strip() and not l.startswith("#")]
        for name in names:
            if name in sent_names:
                continue
            # A slate entry is root-absolute and shared by every idle stream;
            # a live one is relative to this stream's directory. Both must be
            # relayed, or a viewer loses the picture whenever ingest pauses.
            if name.startswith("/"):
                path = slate_dir / "offline.ts"
            else:
                path = d / name
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if not data:
                continue
            send_segment(sock, peers, args.stream_id, next_seq, data,
                         args.k, args.n, codec, args.pace)
            sent_names[name] = "seg%05d.ts" % (next_seq % 100000)
            next_seq += 1
            while len(sent_names) > 60:
                sent_names.popitem(last=False)

        new_pl = rewrite_playlist(body, sent_names)
        if new_pl != last_pl:
            gen += 1
            send_manifest(sock, peers, args.stream_id, gen, new_pl)
            last_pl = new_pl
        time.sleep(0.3)


if __name__ == "__main__":
    main()
