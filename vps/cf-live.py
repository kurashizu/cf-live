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
import hmac
import os
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
#    "window": [(uri, dur, disc)],
#    "live": bool,          # was the last appended entry live content
#    "last_raw": str,       # newest ffmpeg segment already appended
#    "next_slate": float}   # when the next slate entry is due
SEQ_STATE = {}
SEQ_LOCK = threading.Lock()

# How many distinct slate URIs to rotate through. Must comfortably exceed the
# playlist window so no two entries in one window ever share a URI — a player
# keys fragments by URI and would treat a repeat as one fragment, which is the
# bug that made the slate play once and stall.
SLATE_POSITIONS = 64


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
        m = re.match(r"^/live/_offline\.([0-9a-f]{8})(?:\.\d+)?\.ts$", path)
        if m:
            fresh = m.group(1) == CONFIG.get("slate_version")
            self.send_file(
                Path(CONFIG["root"]) / "offline" / "offline.ts",
                TS_TYPE,
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
                      "last_raw": "", "next_slate": 0.0}
                SEQ_STATE[stream] = st
                # Start with a full window rather than one entry that grows
                # over the next few seconds. A player handed a single 2s
                # fragment has nothing buffered ahead and stalls on the first
                # jitter — the same failure the repeated-URI bug produced,
                # just arriving by a different route. These are backdated so
                # the next entry is due immediately, keeping the timeline on
                # real time.
                uri = f"/live/_offline.{CONFIG['slate_version']}"
                for i in range(max(CONFIG["window"], 1)):
                    st["window"].append(
                        (f"{uri}.{i % SLATE_POSITIONS}.ts", dur, False))
                st["next_slate"] = now

            fresh = self.live_segments(stream)
            if fresh:
                # Append only what has not been published yet, preserving
                # ffmpeg's order.
                new_entries = []
                if st["last_raw"]:
                    try:
                        after = [n for n, _ in fresh]
                        idx = after.index(st["last_raw"])
                        pending = fresh[idx + 1:]
                    except ValueError:
                        # The name is gone from disk: the encoder restarted
                        # and renumbered. Everything on offer is new.
                        pending = fresh
                else:
                    pending = fresh
                for name, d in pending:
                    new_entries.append((f"{name}", d, not st["live"]))
                    st["live"] = True
                if new_entries:
                    st["last_raw"] = pending[-1][0]
                    st["window"].extend(new_entries)
                    # Slate resumes only after ingest has actually stopped.
                    st["next_slate"] = now + dur
            else:
                # Idle. Add a slate entry when the previous one has played
                # out, so the timeline advances at real time rather than as
                # fast as the viewer polls.
                if now >= st["next_slate"]:
                    # A distinct URI per position: a player keys fragments by
                    # URI, so repeating one URI reads as a single fragment —
                    # the slate would play once (~2s) and stall with the
                    # sequence still climbing. The bytes are the same object;
                    # only the path differs.
                    # The counter only has to make consecutive entries in
                    # the window distinct, so it wraps over a small set.
                    # Letting it climb forever would mint ~1800 immutable
                    # cache entries per idle hour for what is one 35 KB
                    # object.
                    pos = st["seq"] + len(st["window"])
                    uri = (f"/live/_offline.{CONFIG['slate_version']}"
                           f".{pos % SLATE_POSITIONS}.ts")
                    st["window"].append((uri, dur, st["live"] or not st["window"]))
                    st["live"] = False
                    st["last_raw"] = ""
                    st["next_slate"] = max(now, st["next_slate"]) + dur

            # Roll the window, advancing the sequence by whatever was dropped.
            if len(st["window"]) > keep:
                dropped = len(st["window"]) - keep
                st["window"] = st["window"][dropped:]
                st["seq"] += dropped
            # A discontinuity that scrolled to the front of the window is
            # still meaningful, but one on the very first entry of a brand
            # new timeline is not — there is nothing before it to break from.
            entries = list(st["window"])
            seq = st["seq"]

        target = max(int(round(max((d for _, d, _ in entries), default=dur))), 1)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f"#EXT-X-MEDIA-SEQUENCE:{seq}",
                 f"#EXT-X-TARGETDURATION:{target}"]
        for i, (uri, d, disc) in enumerate(entries):
            if disc and not (seq == 0 and i == 0):
                lines.append("#EXT-X-DISCONTINUITY")
            # Three decimals and the ", no desc" title are SRS's exact output.
            lines.append(f"#EXTINF:{d:.3f}, no desc")
            lines.append(uri)
        return ("\n".join(lines) + "\n").encode()

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
        grace = CONFIG.get("stale_after", 15)
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

    def offline_playlist(self) -> str:
        import time
        dur = CONFIG["slate_duration"]
        # Advances with the clock so the playlist reads as live. Kept small:
        # a media sequence in the hundreds of thousands makes some players
        # render black video.
        seq = int(time.time() / dur) % 10000
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-MEDIA-SEQUENCE:{seq}",
            f"#EXT-X-TARGETDURATION:{int(dur) or 1}",
        ]
        for _ in range(CONFIG["slate_window"]):
            # Three decimals and the ", no desc" title are SRS's exact output.
            lines.append(f"#EXTINF:{dur:.3f}, no desc")
            lines.append(f"/live/_offline.{CONFIG['slate_version']}.ts")
        return "\n".join(lines) + "\n"

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
            "live": bool(segs) and (time.time() - newest) < CONFIG.get("stale_after", 15),
            "segmentsBuffered": len(segs),
            "bufferedBytes": sum(p.stat().st_size for p in segs),
            "secondsSinceLastIngest": round(time.time() - newest, 2) if newest else -1,
            "playlistPresent": live.is_file(),
        })
        self.send_body(body.encode(), "application/json", "no-store")

    # ---- ingest ----------------------------------------------------------
    def do_PUT(self):
        self.ingest(write=True)

    def do_DELETE(self):
        self.ingest(write=False)

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
            self.send_text("not found\n", 404)
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
        """Keep only the most recent segments in a stream's directory.

        Ordered by the counter in the filename rather than mtime: ffmpeg
        numbers segments monotonically, and that is what the playlist
        references, whereas mtimes can tie at this granularity.
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

        def index(p: Path) -> int:
            m = re.search(r"(\d+)(?=\.[^.]+$)", p.name)
            return int(m.group(1)) if m else -1

        for stale in sorted(files, key=index)[:-keep]:
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
                    with SEQ_LOCK:
                        SEQ_STATE.pop(d.name, None)
                    continue
                newest = max(f.stat().st_mtime for f in files)
                if time.time() - newest > idle_seconds:
                    shutil.rmtree(d, ignore_errors=True)
                    # Drop the sequence state too, or it accumulates one
                    # entry per stream name for the life of the process.
                    # A stream reaped and later revived starts a fresh
                    # timeline anyway, so there is nothing to preserve.
                    with SEQ_LOCK:
                        SEQ_STATE.pop(d.name, None)
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
    ap.add_argument("--slate-duration", type=float, default=2.0)
    ap.add_argument("--slate-window", type=int, default=3)
    ap.add_argument("--max-body", type=int, default=32 * 1024 * 1024,
                    help="largest accepted upload, in bytes")
    ap.add_argument("--reap-after", type=int, default=300,
                    help="delete a stream's directory after this many seconds idle")
    ap.add_argument("--stale-after", type=int, default=15,
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
    # Two entries in one window must never share a slate URI, or a player
    # collapses them into a single fragment and the slate stalls.
    if args.window >= SLATE_POSITIONS:
        sys.exit(f"--window must be below {SLATE_POSITIONS}")
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
