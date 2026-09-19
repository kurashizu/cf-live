#!/usr/bin/env python3
"""KRSZ Live: an HLS relay in the standard library.

Receives HLS segments from OBS over HTTP PUT and serves them to players.
Written against Python 3.9 with no third-party packages, because the target
box cannot run a package manager without falling over.

Replaces the Cloudflare Worker version, which was limited by request quota
rather than by capability. The playlist format, cache headers and offline
behaviour are carried over from that build, where they were established by
comparing against SRS — a stack known to play in VRChat.

Segments live under a tmpfs directory: written once, read for a few seconds,
then deleted. Disk would only add wear.
"""

import argparse
import hashlib
import math
import hmac
import os
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from http.server import ThreadingHTTPServer
except ImportError:
    # Python 3.6 (Alibaba Cloud Linux 3 ships 3.6.8) predates
    # ThreadingHTTPServer. Compose it exactly as 3.7+ does: a stalled viewer
    # must not block ingest, and segments are served concurrently.
    import socketserver

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True
from pathlib import Path

# Names that must never be treated as a stream, or a stream could shadow an
# endpoint.
RESERVED = {
    "healthz", "health", "live", "ingest", "index.html",
    "favicon.ico", "robots.txt", "sitemap.xml",
    # Endpoint prefixes: /status is the status API, and "offline" would
    # collide with the shared slate.
    "status", "offline",
}

SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
M3U8 = "application/vnd.apple.mpegurl"
# SRS serves exactly this, casing included.
TS_TYPE = "video/MP2T"

CONFIG = {}

# Per-stream timeline state, keyed by stream name.
#
# A viewer must see ONE continuous stream whether or not anybody is
# broadcasting. Serving ffmpeg's playlist while live and a separate slate
# playlist while idle gives two unrelated timelines, and crossing between
# them forces the player to tear down and rebuild — the visible stall this
# state exists to remove.
#
# So the playlist is built here instead of passed through: a monotonic
# sequence, a rolling window of entries, and a discontinuity marker emitted
# only at a real seam (slate -> live, live -> slate, encoder restart).
#
#   {"seq": int,            # media sequence of window[0]
#    "window": [(uri, dur, disc, published)],
#    "live": bool,          # was the last appended entry live content
#    "last_raw": str,       # newest ffmpeg segment already appended
#    "next_slate": float,   # wall-clock time the next slate entry is due
#    "slate_pos": int,      # timeline position of the next slate entry
#    "disc_seq": int}       # discontinuities that have scrolled out of view
#
# Slate entries are scheduled against the wall clock, never against the
# polls that happen to arrive. The first version appended one entry per poll
# once the previous had "played out", which let every poll's network latency
# slip the timeline a little further behind real time. The player, playing
# at real time, drained its buffer and stalled once per fragment — the
# stutter that looked like a broken slate file and was not.
SEQ_STATE = {}
SEQ_LOCK = threading.Lock()

# ---- slate timestamps -------------------------------------------------------
#
# The slate is one short file, but it is served as an endless live stream.
# Repeating the same bytes would repeat the same timestamps: every fragment
# would restart at PTS 0 while the playlist claims time keeps moving. A
# player can only reconcile that with a discontinuity per fragment, and each
# one is a seam where decoders reset and buffers can develop holes.
#
# So the slate is served per position instead: /live/_offline.<ver>.<n>.ts
# carries the same media with every timestamp advanced by n x duration. The
# result is a genuinely continuous stream, no different from a live one, and
# the only discontinuities left are the real seams (slate <-> live, encoder
# restart). Shifting is a byte-level rewrite of the MPEG-TS headers, no
# re-encoding, and cheap enough to do per request; a small cache covers the
# window every viewer is polling anyway.

TS_PACKET = 188
TS_SYNC = 0x47
TS_CLOCK = 90000       # PTS/DTS/PCR ticks per second
TS_WRAP = 1 << 33      # timestamps are 33-bit; they roll over, players expect it


def _ts_read(buf, i):
    """Decode the 33-bit timestamp at buf[i:i+5] (marker bits interleaved)."""
    return (((buf[i] >> 1) & 0x07) << 30 |
            buf[i + 1] << 22 |
            ((buf[i + 2] >> 1) & 0x7F) << 15 |
            buf[i + 3] << 7 |
            (buf[i + 4] >> 1) & 0x7F)


def _ts_write(buf, i, value, prefix):
    value %= TS_WRAP
    buf[i] = (prefix << 4) | ((value >> 30) & 0x07) << 1 | 1
    buf[i + 1] = (value >> 22) & 0xFF
    buf[i + 2] = ((value >> 15) & 0x7F) << 1 | 1
    buf[i + 3] = (value >> 7) & 0xFF
    buf[i + 4] = (value & 0x7F) << 1 | 1


def shift_timestamps(data: bytes, seconds: float) -> bytes:
    """Copy of an MPEG-TS segment with every PTS, DTS and PCR moved forward.

    Touches only the fields that carry time: the PCR in adaptation fields
    and the PTS/DTS in PES headers. Everything else, including continuity
    counters and the payload, is passed through untouched.
    """
    delta = int(round(seconds * TS_CLOCK))
    out = bytearray(data)
    for base in range(0, len(out) - TS_PACKET + 1, TS_PACKET):
        if out[base] != TS_SYNC:
            continue
        afc = (out[base + 3] >> 4) & 0x03
        pos = base + 4
        if afc in (2, 3):                       # adaptation field present
            af_len = out[pos]
            if af_len and (out[pos + 1] & 0x10) and af_len >= 7:   # PCR
                q = pos + 2
                pcr = (out[q] << 25 | out[q + 1] << 17 | out[q + 2] << 9 |
                       out[q + 3] << 1 | out[q + 4] >> 7)
                ext = ((out[q + 4] & 0x01) << 8) | out[q + 5]
                pcr = (pcr + delta) % TS_WRAP
                out[q] = (pcr >> 25) & 0xFF
                out[q + 1] = (pcr >> 17) & 0xFF
                out[q + 2] = (pcr >> 9) & 0xFF
                out[q + 3] = (pcr >> 1) & 0xFF
                out[q + 4] = ((pcr & 0x01) << 7) | 0x7E | (ext >> 8)
                out[q + 5] = ext & 0xFF
            pos += 1 + af_len
        if afc in (0, 2):                       # no payload
            continue
        if not (out[base + 1] & 0x40):          # not the start of a PES
            continue
        if pos + 9 > base + TS_PACKET or out[pos:pos + 3] != b"\x00\x00\x01":
            continue
        flags = out[pos + 7]
        opt = pos + 9
        if flags & 0x80 and opt + 5 <= base + TS_PACKET:            # PTS
            _ts_write(out, opt, _ts_read(out, opt) + delta,
                      0x3 if flags & 0x40 else 0x2)
            if flags & 0x40 and opt + 10 <= base + TS_PACKET:       # DTS
                _ts_write(out, opt + 5, _ts_read(out, opt + 5) + delta, 0x1)
    return bytes(out)


SLATE_CACHE = {}           # position -> shifted bytes
SLATE_CACHE_LOCK = threading.Lock()
SLATE_CACHE_MAX = 64


def slate_at(position) -> bytes:
    """The slate segment for one timeline position, or the raw file."""
    raw = (Path(CONFIG["root"]) / "offline" / "offline.ts").read_bytes()
    if position is None:
        return raw
    with SLATE_CACHE_LOCK:
        hit = SLATE_CACHE.get(position)
    if hit is not None:
        return hit
    data = shift_timestamps(raw, position * CONFIG["slate_duration"])
    with SLATE_CACHE_LOCK:
        if len(SLATE_CACHE) >= SLATE_CACHE_MAX:
            # Positions only ever climb, so the smallest are the stale ones.
            for old in sorted(SLATE_CACHE)[:SLATE_CACHE_MAX // 2]:
                SLATE_CACHE.pop(old, None)
        SLATE_CACHE[position] = data
    return data


def safe(name: str) -> bool:
    return bool(SAFE_NAME.match(name)) and ".." not in name


def stream_dir(stream: str) -> Path:
    return Path(CONFIG["root"]) / stream


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_expect_100(self):
        # ffmpeg sends "Expect: 100-continue" on uploads. The default
        # implementation is fine, but being explicit documents that ingest
        # depends on it working.
        self.send_response_only(100)
        self.end_headers()
        return True
    # Quieter than the default, which logs every segment.
    def log_message(self, fmt, *args):
        if CONFIG["verbose"]:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ---------------------------------------------------------
    def send_body(self, body: bytes, ctype: str, cache: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, text: str, status: int = 200):
        self.send_body(text.encode(), "text/plain; charset=utf-8", "no-store", status)

    def send_file(self, path: Path, ctype: str, cache: str):
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            self.send_text("not found\n", 404)
            return
        self.send_body(data, ctype, cache)

    # ---- routing ---------------------------------------------------------
    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def route(self):
        path = self.path.split("?", 1)[0]

        if path in ("/healthz", "/health"):
            self.send_text("ok\n")
            return

        if path in ("/", "/index.html"):
            page = Path(CONFIG["www"]) / "index.html"
            # The page embeds its own script, so a cached copy means stale
            # player logic and a reload that appears to do nothing.
            self.send_file(page, "text/html; charset=utf-8", "no-store")
            return

        # The offline slate. Shared by every idle stream and cached hard —
        # but only at the fingerprinted path. Regenerating the slate changes
        # the fingerprint, so a new slate is never masked by an old cached
        # copy (that bug cost hours once already).
        m = re.match(r"^/live/_offline(?:\.([0-9a-f]{8}))?(?:\.(\d+))?\.ts$", path)
        if m:
            fresh = m.group(1) == CONFIG.get("slate_version")
            position = int(m.group(2)) if m.group(2) is not None else None
            try:
                body = slate_at(position)
            except OSError:
                self.send_text("not found\n", 404)
                return
            # A position's bytes are a pure function of (slate, position),
            # so they are immutable for as long as the fingerprint matches.
            self.send_body(
                body, TS_TYPE,
                "public, max-age=31536000, immutable" if fresh
                else "public, max-age=60",
            )
            return
        # The unversioned path stays for anything holding an old playlist,
        # with a short TTL so it self-heals.
        if path == "/live/offline.ts":
            self.send_file(
                Path(CONFIG["root"]) / "offline" / "offline.ts",
                TS_TYPE, "public, max-age=60",
            )
            return

        # /live/<stream>/index.m3u8 — the media playlist.
        m = re.match(r"^/live/([^/]+)/index\.m3u8$", path)
        if m and safe(m.group(1)):
            self.serve_media_playlist(m.group(1))
            return

        # /live/<stream>/<file> — a segment.
        m = re.match(r"^/live/([^/]+)/([^/]+)$", path)
        if m and safe(m.group(1)) and safe(m.group(2)):
            self.serve_segment(m.group(1), m.group(2))
            return

        # /status/<stream>
        m = re.match(r"^/status/([^/]+)$", path)
        if m and safe(m.group(1)):
            self.serve_status(m.group(1))
            return

        # /<stream> and /<stream>.m3u8 — the master playlist. Checked last so
        # real endpoints always win.
        m = re.match(r"^/([^/]+?)(\.m3u8)?$", path)
        if m:
            name = m.group(1)
            # Only .m3u8 is stripped; a path carrying another extension was
            # meant for something else, not a stream called "x.mpd".
            if (name.lower() not in RESERVED and safe(name)
                    and not (m.group(2) is None and re.search(r"\.[A-Za-z0-9]+$", name))):
                self.serve_master(name)
                return

        self.send_text("not found\n", 404)

    # ---- playback --------------------------------------------------------
    def serve_master(self, stream: str):
        # Players in the VRChat ecosystem expect a master: AVPro reads codec
        # hints from EXT-X-STREAM-INF before committing to a rendition, and
        # given a bare media playlist some builds sit in a loading state.
        #
        # BANDWIDTH=1 and no CODECS mirrors SRS. Declaring codecs invites a
        # player to decide up front whether it can decode the stream, and a
        # declaration that disagrees with the bitstream shows up as a black
        # picture with otherwise healthy playback.
        body = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1,AVERAGE-BANDWIDTH=1\n"
            f"/live/{stream}/index.m3u8\n"
        )
        self.send_body(body.encode(), M3U8, "no-store")

    def serve_media_playlist(self, stream: str):
        # Never cache a live playlist: a player reusing a stale copy sees the
        # stream frozen. Plain no-store, not no-cache + must-revalidate —
        # Cloudflare strips that combination, leaving no directive at all, at
        # which point a client caches anyway.
        self.send_body(self.continuous_playlist(stream), M3U8, "no-store")

    def continuous_playlist(self, stream: str) -> bytes:
        """Build one unbroken timeline for a stream, live or not.

        The viewer's player is never told the broadcast stopped. When ingest
        is flowing, live segments are appended; when it is not, slate
        segments are, and the seam carries #EXT-X-DISCONTINUITY so the
        decoder resets without the stream itself ending. The media sequence
        only ever climbs, so no poll ever looks like a playlist reset — which
        is what previously forced the client to destroy and rebuild the
        player, the stall this removes.
        """
        import time
        now = time.time()
        dur = CONFIG["slate_duration"]
        keep = max(CONFIG["window"], 1)

        with SEQ_LOCK:
            st = SEQ_STATE.get(stream)
            if st is None:
                st = {"seq": 0, "window": [], "live": False,
                      "last_raw": "", "next_slate": now,
                      "slate_pos": 0, "disc_seq": 0, "touched": now}
                SEQ_STATE[stream] = st
                # Start with a full window rather than one entry that grows
                # over the next few seconds. A player handed a single 2s
                # fragment has nothing buffered ahead and stalls on the first
                # jitter. One more is due immediately: see the lead below.
                for _ in range(keep):
                    st["window"].append((self.slate_uri(st), dur, False, now))

            fresh = self.live_segments(stream)
            if fresh:
                # Append only what has not been published yet, preserving
                # ffmpeg's order.
                new_entries = []
                seam = not st["live"]
                if st["last_raw"]:
                    try:
                        after = [n for n, _ in fresh]
                        idx = after.index(st["last_raw"])
                        pending = fresh[idx + 1:]
                    except ValueError:
                        # The name is gone from disk: the encoder restarted
                        # and renumbered. Everything on offer is new, and
                        # its timestamps start over, so this is a seam even
                        # though the stream never went idle.
                        pending = fresh
                        seam = True
                else:
                    pending = fresh
                for name, d in pending:
                    new_entries.append((f"{name}", d, seam, now))
                    seam = False
                    st["live"] = True
                if new_entries:
                    st["last_raw"] = pending[-1][0]
                    st["window"].extend(new_entries)
                    # Slate resumes only after ingest has actually stopped.
                    st["next_slate"] = now + dur
            else:
                # Idle. Slate entries are due on a fixed wall-clock cadence,
                # one per slate duration, independent of when polls arrive.
                if st["live"]:
                    # The broadcast just stopped: the slate timeline starts
                    # here, and its first entry is a real seam. Two entries
                    # are due at once, so the slate runs one entry ahead of
                    # the clock from the outset. The slate is synthetic, so
                    # "ahead" costs nothing, and it is what keeps a viewer's
                    # buffer from running dry: a live viewer sits about a
                    # second behind the edge, and their picture has already
                    # frozen by the time the encoder is known to be gone.
                    # Every entry after these lands a full duration before
                    # the player needs it, which also removes the race
                    # between the entry cadence and the poll cadence that
                    # otherwise leaves a poll just missing the next entry.
                    st["next_slate"] = now - dur
                    st["live"] = False
                    st["last_raw"] = ""
                    seam = True
                else:
                    seam = False
                if now - st["next_slate"] > dur * keep:
                    # Nobody polled for a while. Skip ahead rather than
                    # replaying the gap as a burst of entries; a returning
                    # viewer wants the present, not a backlog.
                    st["next_slate"] = now - dur * (keep - 1)
                while now >= st["next_slate"]:
                    st["window"].append((self.slate_uri(st), dur, seam, now))
                    seam = False
                    st["next_slate"] += dur

            # Roll the window, advancing the sequence by whatever is dropped.
            # The count is the normal limit, with one exception: a slate
            # entry stays a while after the count would drop it. When a
            # broadcast starts, the window turns over one 0.5 s segment
            # every 0.5 s -- faster than a player still polling at the
            # slate's 2 s cadence -- so two consecutive polls could share no
            # entry at all. hls.js then has to guess how much time the
            # missed entries covered, and guesses with the *new* target
            # duration (1 s) rather than the slate's 2.1 s, placing live
            # content four seconds too early: over media already buffered,
            # behind the playhead, and the player stalls and seeks. Holding
            # each slate entry for two slate durations plus a second keeps
            # an overlap for any poll interval the player actually uses.
            hold = CONFIG["slate_duration"] * 2 + 1.0
            while len(st["window"]) > keep:
                uri, _, disc, published = st["window"][0]
                if uri.startswith("/live/_offline.") and now - published < hold:
                    break
                # RFC 8216 6.2.2: when a DISCONTINUITY tag leaves the
                # window, EXT-X-DISCONTINUITY-SEQUENCE must go up by one.
                # A player numbers timelines by counting the tags it can
                # see; without this the numbering slides back as tags
                # scroll off, a new timeline can reuse the number of an
                # old one, and the player applies the old timeline's
                # timestamp offset to the new content.
                if disc:
                    st["disc_seq"] += 1
                del st["window"][0]
                st["seq"] += 1
            # A discontinuity that scrolled to the front of the window is
            # still meaningful, but one on the very first entry of a brand
            # new timeline is not — there is nothing before it to break from.
            st["touched"] = now
            entries = list(st["window"])
            seq = st["seq"]
            disc_seq = st.get("disc_seq", 0)

        # Rounded up, not to nearest: EXT-X-TARGETDURATION must be at least
        # the longest EXTINF, and round() would report 2 for a 2.133 s slate.
        target = max(int(math.ceil(max((e[1] for e in entries), default=dur))), 1)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f"#EXT-X-MEDIA-SEQUENCE:{seq}",
                 f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc_seq}",
                 f"#EXT-X-TARGETDURATION:{target}"]
        for uri, d, disc, _ in entries:
            if disc:
                lines.append("#EXT-X-DISCONTINUITY")
            # Three decimals and the ", no desc" title are SRS's exact output.
            lines.append(f"#EXTINF:{d:.3f}, no desc")
            lines.append(uri)
        return ("\n".join(lines) + "\n").encode()

    def slate_uri(self, st) -> str:
        """Mint the next slate entry for a stream's timeline.

        Positions climb forever and never wrap: position n carries
        timestamps starting at n x duration, so a wrap would be a rewind
        and need a discontinuity to hide. The cost is one immutable edge
        cache entry per idle slate duration, for a 37 KB object.
        """
        n = st["slate_pos"]
        st["slate_pos"] = n + 1
        return f"/live/_offline.{CONFIG['slate_version']}.{n}.ts"

    def live_segments(self, stream: str):
        """The segments ffmpeg is currently advertising, oldest first.

        Read from ffmpeg's own playlist rather than the directory listing:
        the playlist is what the encoder considers complete, and a segment
        still being written is not in it yet.
        """
        if self.stream_stale(stream):
            return []
        try:
            body = (stream_dir(stream) / "live.m3u8").read_text("utf-8", "replace")
        except OSError:
            return []
        out, pending = [], None
        for line in body.splitlines():
            if line.startswith("#EXTINF:"):
                try:
                    pending = float(line.split(":", 1)[1].split(",")[0])
                except ValueError:
                    pending = CONFIG["slate_duration"]
            elif line and not line.startswith("#"):
                name = line.strip()
                if safe(name) and (stream_dir(stream) / name).is_file():
                    out.append((name, pending or 1.0))
                pending = None
        return out

    def ingest_grace(self, stream: str) -> float:
        """How long ingest may go quiet before the stream counts as stopped.

        --stale-after is the floor, tuned for 0.5 s segments. An encoder
        sending 2 s segments is silent for 2 s between uploads by design, so
        the grace scales with the segment length it is actually producing:
        two segments' worth, read from the encoder's own playlist.
        """
        grace = float(CONFIG.get("stale_after", 15))
        if grace <= 0:
            return grace
        longest = 0.0
        try:
            text = (stream_dir(stream) / "live.m3u8").read_text("utf-8", "replace")
            for line in text.splitlines():
                if line.startswith("#EXTINF:"):
                    try:
                        longest = max(longest, float(line.split(":", 1)[1].split(",")[0]))
                    except ValueError:
                        pass
        except OSError:
            pass
        return max(grace, 2.0 * longest)

    def stream_stale(self, stream: str) -> bool:
        """True once a broadcast has clearly stopped.

        The playlist file outlives the broadcast: it sits in tmpfs until the
        reaper removes the directory, which is deliberately slow (minutes) so
        a brief encoder hiccup does not destroy a stream. But serving that
        file in the meantime shows viewers a frozen window of segments that
        are no longer being produced — and after eviction, 404s. Falling back
        to the rolling slate instead keeps players polling, so they pick the
        broadcast up the moment it resumes.

        Uses the same grace period as /status so the two never disagree.
        """
        import time
        grace = self.ingest_grace(stream)
        if grace <= 0:
            return False
        d = stream_dir(stream)
        try:
            newest = max(
                (p.stat().st_mtime for p in d.iterdir()
                 if p.suffix in (".ts", ".m4s")),
                default=0,
            )
        except OSError:
            return True
        if not newest:
            return True
        return (time.time() - newest) > grace

    def trim_window(self, body: bytes, stream: str = "") -> bytes:
        """Shorten the playlist to the newest N segments.

        A player starts at the oldest advertised entry, so the window length
        is the dominant latency term. ffmpeg decides it via hls_list_size, but
        that lives in the broadcaster's OBS config — trimming here puts
        latency under server control instead, so it can be tuned without
        touching the encoder.

        Set --window 0 to pass ffmpeg's playlist through untouched.
        """
        keep = CONFIG["window"]
        if keep <= 0:
            return body

        lines = body.decode("utf-8", "replace").splitlines()
        header, entries, seq = [], [], 0
        pending = None
        for line in lines:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    seq = int(line.split(":", 1)[1])
                except ValueError:
                    pass
                header.append(line)
            elif line.startswith("#EXTINF:"):
                pending = line
            elif line and not line.startswith("#"):
                entries.append((pending, line))
                pending = None
            elif line.startswith("#EXT-X-ENDLIST"):
                # Leave a finished playlist alone; trimming it would hide the
                # end of the stream.
                return body
            else:
                header.append(line)

        if len(entries) <= keep:
            return body

        dropped = len(entries) - keep
        # Must advance in step with what was dropped, or a player computes the
        # wrong live edge.
        raw = seq + dropped
        served, restarted = self.monotonic_sequence(stream, raw)

        out = []
        for line in header:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                out.append(f"#EXT-X-MEDIA-SEQUENCE:{served}")
            else:
                out.append(line)
        window = entries[-keep:]
        for i, (inf, uri) in enumerate(window):
            # The encoder restarted, so the first segment of the new timeline
            # does not continue the previous one: different PTS base, possibly
            # different parameters. Saying so lets the player reset its
            # decoder instead of stalling on a timestamp jump.
            if restarted and i == 0:
                out.append("#EXT-X-DISCONTINUITY")
            if inf:
                out.append(inf)
            out.append(uri)
        return ("\n".join(out) + "\n").encode()

    def monotonic_sequence(self, stream: str, raw: int):
        """Map ffmpeg's sequence onto a never-decreasing one.

        Returns (served_sequence, restarted). ffmpeg numbers segments from
        zero on every relaunch; a live playlist whose EXT-X-MEDIA-SEQUENCE
        moves backwards is invalid per RFC 8216, and players respond by
        stalling rather than reseeking. An offset absorbs each restart, so the
        sequence a viewer sees only ever climbs.

        Kept modest: sequences in the hundreds of thousands make some players
        render black video, so the offset wraps well below that.
        """
        if not stream:
            return raw, False
        with SEQ_LOCK:
            st = SEQ_STATE.get(stream)
            if st is None:
                # First sight of this stream: serve ffmpeg's own numbering.
                SEQ_STATE[stream] = {"offset": 0, "raw": raw, "served": raw,
                                     "mark": False}
                return raw, False

            restarted = False
            if raw < st["raw"]:
                # ffmpeg relaunched and its counter went back to zero. Resume
                # just past the last sequence a viewer was shown — tracked as
                # the served value, not the raw one, so repeated restarts each
                # compute their offset from what was actually published.
                st["offset"] = st["served"] + 1 - raw
                restarted = True

            served = (raw + st["offset"]) % 100000
            st["raw"] = raw
            st["served"] = served
            # #EXT-X-DISCONTINUITY must appear on the first published playlist
            # of the new timeline and then stop. Emitting it on every poll
            # would make the player reset its decoder continuously; emitting
            # it never leaves it stalled on the timestamp jump. "mark" carries
            # the one-shot across polls, since a restart is detected on the
            # poll that first sees the lower sequence.
            if restarted:
                st["mark"] = True
            emit = st["mark"]
            st["mark"] = False
            return served, emit

    def serve_segment(self, stream: str, name: str):
        path = stream_dir(stream) / name
        if name.endswith(".m3u8"):
            self.send_text("not found\n", 404)
            return
        ctype = "video/mp4" if name.endswith(".mp4") else TS_TYPE
        # Segments are immutable once written, so caching them is always safe
        # — and it is what stops viewer count from being bounded by uplink
        # bandwidth.
        cache = ("public, max-age=60"
                 if name.endswith(".mp4")
                 else f"public, max-age={CONFIG['segment_ttl']}, immutable")
        self.send_file(path, ctype, cache)

    def serve_status(self, stream: str):
        import json
        import time
        d = stream_dir(stream)
        live = d / "live.m3u8"
        segs = sorted(d.glob("*.ts")) + sorted(d.glob("*.m4s")) if d.is_dir() else []
        newest = max((p.stat().st_mtime for p in segs), default=0)
        body = json.dumps({
            "live": bool(segs) and (time.time() - newest) < self.ingest_grace(stream),
            "segmentsBuffered": len(segs),
            "bufferedBytes": sum(p.stat().st_size for p in segs),
            "secondsSinceLastIngest": round(time.time() - newest, 2) if newest else -1,
            "playlistPresent": live.is_file(),
        }, separators=(",", ":"))
        self.send_body(body.encode(), "application/json", "no-store")

    # ---- ingest ----------------------------------------------------------
    def do_PUT(self):
        self.ingest(write=True)

    def do_DELETE(self):
        self.ingest(write=False)

    def do_POST(self):
        # Nothing here takes a POST; 405 rather than the default 501 so a
        # client learns the method is wrong, not that the server is broken.
        self.drain()
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, PUT, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def ingest(self, write: bool):
        # ffmpeg's http_persistent reuses one connection for every segment and
        # playlist rewrite of a broadcast. That only works if each request's
        # body is fully consumed before the next is parsed — otherwise the
        # leftover bytes are read as the following request line and ffmpeg
        # reports "URL read error: End of file" and reconnects per segment.
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/ingest/([^/]+)/([^/]+)/([^/]+)$", path)
        if not m:
            self.drain()
            # Writes only exist under /ingest/; anywhere else the method
            # is the problem, not the path.
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        key, stream, name = m.groups()
        # compare_digest so a wrong key does not leak its length by timing.
        if not hmac.compare_digest(key, CONFIG["key"]):
            self.drain()
            self.send_text("forbidden\n", 403)
            return
        if not safe(stream) or not safe(name):
            self.drain()
            self.send_text("bad name\n", 400)
            return

        target = stream_dir(stream) / name
        if not write:
            # ffmpeg's delete_segments sends these. Honour them: ffmpeg is the
            # one tracking the window, so its view of what to drop is correct.
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            self.send_text("ok\n")
            return

        data = self.read_body()
        if data is None:
            self.send_text("bad body\n", 400)
            return
        if not data:
            self.send_text("empty\n", 400)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name and rename into place, so a player never
        # reads a half-written segment.
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        # ffmpeg's delete_segments only prunes when it writes local files; over
        # HTTP it never sends DELETE, so nothing here would ever be removed and
        # a long broadcast would fill the tmpfs. Measured: 70 segments and
        # 12MB after five minutes, still climbing.
        if not name.endswith(".m3u8"):
            self.evict(target.parent, name)
        self.send_text("ok\n")

    def evict(self, directory: Path, just_written: str):
        """Keep only the most recently written segments in a stream's directory.

        Ordered by modification time, newest last. Ordering by the counter in
        the filename looked right -- ffmpeg numbers segments monotonically --
        but it breaks on every encoder restart: ffmpeg renumbers from zero,
        so a fresh seg00000 sorts below whatever the previous run left behind
        and is deleted the moment it arrives. The broadcast then never starts
        and nothing reports an error. A first fix caught only restarts after
        long runs (a new index far below the leftovers); a restart a few
        minutes after a short run left indexes 29-47 on disk, the new 0-7
        sorted below them, and the same silent failure came back.

        Write time has no such blind spot: what the playlist references is
        always what was written last. Ties are broken by the counter, for a
        filesystem with coarse timestamps.
        """
        keep = CONFIG["keep_segments"]
        if keep <= 0:
            return
        suffix = Path(just_written).suffix
        try:
            files = [p for p in directory.iterdir() if p.suffix == suffix]
        except OSError:
            return
        if len(files) <= keep:
            return

        def index(p) -> int:
            m = re.search(r"(\d+)(?=\.[^.]+$)", p.name)
            return int(m.group(1)) if m else -1

        def age(p):
            try:
                return (p.stat().st_mtime_ns, index(p))
            except OSError:
                return (0, -1)

        for stale in sorted(files, key=age)[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass

    def read_body(self):
        """Read a request body, whether length-delimited or chunked.

        ffmpeg's hls muxer uploads segments with Transfer-Encoding: chunked
        and no Content-Length. BaseHTTPRequestHandler does not decode that, so
        reading Content-Length alone leaves the chunk framing in the stream —
        the next request line parses as the hex chunk size ("8000") and every
        upload fails with 400.
        """
        if (self.headers.get("Transfer-Encoding") or "").lower().strip() == "chunked":
            out = bytearray()
            while True:
                line = self.rfile.readline(65).strip()
                if not line:
                    return None
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    return None
                if size == 0:
                    # Consume trailers up to the blank line.
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    return bytes(out)
                out += self.rfile.read(size)
                self.rfile.read(2)  # the CRLF after each chunk
                if len(out) > CONFIG["max_body"]:
                    return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > CONFIG["max_body"]:
            return None
        return self.rfile.read(length) if length else b""

    def drain(self):
        """Consume a request body so the connection can be reused."""
        try:
            self.read_body()
        except OSError:
            pass


def reap_stale(root: Path, idle_seconds: int, interval: int = 60):
    """Delete stream directories nobody has written to for a while.

    ffmpeg's delete_segments only prunes its own sliding window, so whatever
    was in flight when a broadcast ended stays on disk. On a tmpfs that is
    memory held for no reason, and it accumulates one stream at a time.
    """
    import time
    while True:
        time.sleep(interval)
        try:
            for d in root.iterdir():
                if not d.is_dir() or d.name == "offline":
                    continue
                files = list(d.iterdir())
                if not files:
                    d.rmdir()
                    continue
                newest = max(f.stat().st_mtime for f in files)
                if time.time() - newest > idle_seconds:
                    shutil.rmtree(d, ignore_errors=True)
            # Timeline state is NOT dropped with the directory. An idle
            # stream keeps serving the slate from that same timeline, so
            # discarding it rewinds EXT-X-MEDIA-SEQUENCE to zero -- which a
            # player reads as a different stream and answers by never
            # loading another fragment. Measured: viewers stalled on the
            # slate every time the reaper ran.
            #
            # Instead it expires on its own inactivity: nobody has asked for
            # this stream in a long while, so no viewer's timeline can break.
            cutoff = time.time() - max(idle_seconds * 4, 3600)
            with SEQ_LOCK:
                for name in [k for k, v in SEQ_STATE.items()
                             if v.get("touched", 0) < cutoff]:
                    SEQ_STATE.pop(name, None)
        except OSError:
            # A stream being written to concurrently can race us; next pass
            # will catch it.
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--root", default="/dev/shm/cf-live",
                    help="where segments live; should be tmpfs")
    ap.add_argument("--www", default="/opt/cf-live/www",
                    help="directory holding index.html")
    ap.add_argument("--key", default=os.environ.get("INGEST_KEY", ""),
                    help="ingest key; also read from INGEST_KEY")
    ap.add_argument("--segment-ttl", type=int, default=3600,
                    help="edge cache lifetime for segments; they are immutable, "
                         "so this only bounds how long a cached copy survives "
                         "after falling out of the playlist")
    ap.add_argument("--keep-segments", type=int, default=12,
                    help="segments retained per stream. Must exceed --window "
                         "so a segment the playlist still references is never "
                         "deleted; the surplus is the tolerance for a viewer "
                         "lagging behind the live edge")
    ap.add_argument("--window", type=int, default=3,
                    help="segments to advertise; 0 passes ffmpeg's playlist "
                         "through. A player starts at the oldest entry, so "
                         "this is the dominant latency term")
    # 2.1333 s, not 2: that is 32 video frames at 15 fps and exactly 100
    # AAC frames at 48 kHz. No whole number of AAC frames sums to 2.000 s,
    # so a 2 s slate carried a 26.7 ms audio surplus, and looping one fixed
    # file accumulated it until the audio clock outran the available video
    # and the picture froze with the buffer still full. EXTINF must match
    # the real duration or the same drift reappears in the playlist.
    ap.add_argument("--slate-duration", type=float, default=32.0 / 15.0)
    ap.add_argument("--slate-window", type=int, default=3,
                    help=argparse.SUPPRESS)   # accepted for old units; unused
    ap.add_argument("--max-body", type=int, default=32 * 1024 * 1024,
                    help="largest accepted upload, in bytes")
    ap.add_argument("--reap-after", type=int, default=300,
                    help="delete a stream's directory after this many seconds idle")
    # 1 s, not 15: a viewer sits about one second behind the newest segment,
    # so a playlist that stops advancing freezes their picture within a
    # second, and it stays frozen until the slate takes over. Fifteen seconds
    # of that was the "stuck on the way to offline" complaint. One second is
    # two missed 0.5 s segments; a hiccup that long costs a brief slate
    # flash and two clean seams, which beats a frozen frame every time.
    ap.add_argument("--stale-after", type=float, default=1.0,
                    help="serve the offline slate once ingest has been idle "
                         "this many seconds (0 disables)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not args.key:
        sys.exit("an ingest key is required (--key or INGEST_KEY)")

    # A segment the playlist still advertises must never be evicted, or
    # viewers get a 404 for it.
    if args.keep_segments > 0 and args.keep_segments <= args.window:
        args.keep_segments = args.window + 1
    CONFIG.update(vars(args))
    Path(args.root).mkdir(parents=True, exist_ok=True)

    # Fingerprint the slate so its cached URL changes whenever the file does.
    slate = Path(args.root) / "offline" / "offline.ts"
    try:
        CONFIG["slate_version"] = hashlib.sha256(
            slate.read_bytes()).hexdigest()[:8]
    except OSError:
        # No slate on disk: idle streams will 404 the segment rather than
        # serve a stale one. Surfaced loudly because it breaks the offline
        # screen entirely.
        CONFIG["slate_version"] = "00000000"
        print(f"  WARNING: no slate at {slate}", flush=True)

    if args.reap_after > 0:
        threading.Thread(
            target=reap_stale, args=(Path(args.root), args.reap_after),
            daemon=True,
        ).start()

    # ThreadingHTTPServer: a stalled viewer must not block ingest, and
    # segments are served concurrently.
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    print(f"KRSZ Live listening on {args.bind}:{args.port}", flush=True)
    print(f"  segments: {args.root}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
