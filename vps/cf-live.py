#!/usr/bin/env python3
"""cf-live: an HLS relay in the standard library.

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
}

SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
M3U8 = "application/vnd.apple.mpegurl"
# SRS serves exactly this, casing included.
TS_TYPE = "video/MP2T"

CONFIG = {}


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

        # The offline slate. Shared by every idle stream, immutable, and
        # deliberately served from one path so it caches once.
        if path == "/live/offline.ts":
            self.send_file(
                Path(CONFIG["root"]) / "offline" / "offline.ts",
                TS_TYPE, "public, max-age=31536000, immutable",
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
        live = stream_dir(stream) / "live.m3u8"
        if live.is_file():
            # Never cache a live playlist: a player reusing a stale copy sees
            # the stream frozen. Plain no-store, not no-cache +
            # must-revalidate — Cloudflare strips that combination, leaving no
            # directive at all, at which point a client caches anyway.
            try:
                body = live.read_bytes()
            except OSError:
                self.send_text("not found\n", 404)
                return
            self.send_body(self.trim_window(body), M3U8, "no-store")
            return
        # Nothing being broadcast. Serve a rolling slate playlist rather than
        # an empty one or a 404: an empty playlist shows the viewer nothing,
        # a 404 makes AVPro give up permanently, and a single static entry
        # looks finished so the player stops polling and never notices the
        # broadcast starting.
        self.send_body(self.offline_playlist().encode(), M3U8, "no-store")

    def trim_window(self, body: bytes) -> bytes:
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
        out = []
        for line in header:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                # Must advance in step with what was dropped, or a player
                # computes the wrong live edge.
                out.append(f"#EXT-X-MEDIA-SEQUENCE:{seq + dropped}")
            else:
                out.append(line)
        for inf, uri in entries[-keep:]:
            if inf:
                out.append(inf)
            out.append(uri)
        return ("\n".join(out) + "\n").encode()

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
            lines.append("/live/offline.ts")
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
            "live": bool(segs) and (time.time() - newest) < 15,
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
        self.send_text("ok\n")

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
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not args.key:
        sys.exit("an ingest key is required (--key or INGEST_KEY)")

    CONFIG.update(vars(args))
    Path(args.root).mkdir(parents=True, exist_ok=True)

    if args.reap_after > 0:
        threading.Thread(
            target=reap_stale, args=(Path(args.root), args.reap_after),
            daemon=True,
        ).start()

    # ThreadingHTTPServer: a stalled viewer must not block ingest, and
    # segments are served concurrently.
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    print(f"cf-live listening on {args.bind}:{args.port}", flush=True)
    print(f"  segments: {args.root}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
