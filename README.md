# KRSZ Live

Low-latency HLS live relay on Cloudflare Workers + Durable Objects.
Ingest from OBS over HTTP, play anywhere an HLS player runs — including the
VRChat video players (AVPro / Unity Video Player).

Runs entirely within the Cloudflare Workers free tier. No R2, no VPS, no
transcoding server.

```
OBS ──PUT segments──> Worker ──> Durable Object
                        │          (in-memory ring buffer)
                        │               │
    viewers <──edge cache┴───────────────┘
```

## Why HLS and not WHIP/WebRTC

WHIP/WHEP is WebRTC: signalling is HTTP, but media is UDP + DTLS-SRTP + ICE.
Cloudflare Workers and Durable Objects cannot listen on UDP, cannot open
outbound UDP, and have no DTLS/SRTP stack — so a Worker can never be a WebRTC
media endpoint. (Cloudflare's own WHIP/WHEP product, Cloudflare Realtime, is a
separate paid SFU.)

More decisively, the VRChat video players cannot consume WebRTC at all: AVPro
Video is MediaFoundation on Windows and ExoPlayer on Quest, and VRChat exposes
no WebRTC playback path. HLS is what actually plays, so HLS is what this serves.

## Setup

```sh
npm install
npx wrangler secret put INGEST_KEY     # choose a long random string
npm run deploy
```

For local development, put the key in `.dev.vars`:

```
INGEST_KEY=your-local-dev-key
```

then `npm run dev` and open http://localhost:8787.

The landing page is the setup UI: paste your ingest key into the field at the top
and every command on the page — the OBS URL, muxer settings, encoder settings and
the ffmpeg test command — is rewritten with the real key and can be copied with
one click. The key is kept in `localStorage` only and is never transmitted.

## OBS configuration

Use the **Recording** panel, not Streaming: OBS's streaming output only speaks
RTMP/WHIP, and this service needs HLS muxing. Start the broadcast with
**Start Recording**.

Settings → Output → Output Mode: **Advanced** → **Recording** tab:

| Field | Value |
|---|---|
| Type | `Custom Output (FFmpeg)` |
| FFmpeg Output Type | `Output to URL` |
| File path or URL | `https://<your-worker>/ingest/<KEY>/main/live.m3u8` |
| Container Format | `hls` |
| **Keyframe interval (frames)** | **`30`** — see below, this one controls latency |
| Video Bitrate | `2500` Kbps |
| Video Encoder | `libx264` |
| Audio Encoder | `aac` |

**Muxer Settings** (one line):

```
method=PUT http_persistent=1 ignore_io_errors=1 hls_time=1 hls_list_size=6 hls_flags=delete_segments+omit_endlist hls_segment_type=mpegts hls_segment_filename=https://<your-worker>/ingest/<KEY>/main/seg%05d.ts
```

**Keyframe interval** is the setting that actually determines latency, and OBS
gets it wrong by default.

A segment can only be cut on a keyframe. OBS ships with **249** frames in the
`Keyframe interval (frames)` field, which is ~8s at 30 fps — so `hls_time=1` is
ignored and you get 8s segments. That alone turns a 1s configuration into
20-30s of observed latency.

Set it to **frame rate × segment duration**: `30` at 30 fps, `60` at 60 fps.

> The `Keyframe interval (frames)` field is a separate numeric input in the
> FFmpeg output panel, and it overrides any `g=` written in Video Encoder
> Settings. Setting `g=` alone has no effect.

**Video Encoder Settings** (optional, for latency and compatibility):

```
preset=veryfast tune=zerolatency profile=main bf=0 sc_threshold=0
```

`ignore_io_errors=1` in the muxer settings matters too: it keeps a long
broadcast alive through a transient upload failure instead of ending the
recording.

### Recommended encoder settings

- **1280×720 @ 30 fps, 2500 Kbps** — VRChat screens are small; this looks fine
  and noticeably reduces stalling.
- **AAC, 128 Kbps, 48 kHz, stereo.** AVPro can misbehave at other sample rates.
- **H.264 `main` or `baseline` profile.** Quest builds do not decode `high10`.
- **`bf=0`** (no B-frames) to reduce decode delay.

## Playback

Put this in the VRChat video player:

```
https://<your-worker>/main
```

Three URL forms serve the same playlist — use whichever suits the player:

| URL | Notes |
|---|---|
| `https://<host>/main` | Shortest; easiest to type into VRChat |
| `https://<host>/main.m3u8` | Use if a player insists on the extension |
| `https://<host>/live/main.m3u8` | Explicit form |

All three return a **master playlist** naming one variant, which points at
`/live/main/index.m3u8`. AVPro reads codec and resolution hints from
`EXT-X-STREAM-INF` before committing to a rendition, and handed a bare media
playlist some builds sit in a loading state instead of playing.

The bare `/<stream>` alias is served inline rather than redirected, because some
players (AVPro among them) will not follow a 302 for a manifest. These names are
reserved and cannot be used as stream names: `healthz`, `status`, `live`,
`ingest`, `index.html`, `favicon.ico`, `robots.txt`, `sitemap.xml`,
`apple-touch-icon.png`, `.well-known`.

Any other stream name works and is created on demand, each backed by its own
Durable Object. Push to `/ingest/<KEY>/room2/…` and play `/room2`.

## API

### Ingest (requires key)

| Method | Path | Notes |
|---|---|---|
| `PUT` | `/ingest/:key/:stream/live.m3u8` | ffmpeg's playlist; parsed only for authoritative segment durations, never served |
| `PUT` | `/ingest/:key/:stream/:file.ts` | One MPEG-TS segment into the ring buffer |
| `DELETE` | `/ingest/:key/:stream/:file` | Sent by `delete_segments`; acknowledged only, since eviction follows our own policy and must not race active viewers |

A wrong key returns `403`. Stream and file names are restricted to
`[A-Za-z0-9._-]`, max 128 characters.

### Playback (public)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/:stream` | Shortest playlist URL |
| `GET` | `/:stream.m3u8` | Same playlist, with extension |
| `GET` | `/live/:stream.m3u8` | Media playlist, sliding window, never cached |
| `GET` | `/live/:stream/:file.ts` | Segment bytes, immutable, edge-cached |
| `GET` | `/status/:stream` | JSON stream state |
| `GET` | `/healthz` | Liveness |

## Configuration

`wrangler.toml` `[vars]`:

| Variable | Default | Meaning |
|---|---|---|
| `SEGMENT_DURATION` | `1` | Seconds per segment. Must match OBS's `hls_time` **and** its keyframe interval. |
| `PLAYLIST_SIZE` | `3` | Segments advertised. The dominant latency term. |
| `MAX_SEGMENTS` | `8` | Segments held in memory. Raised to `PLAYLIST_SIZE + 1` if set lower. Sets both jitter tolerance and worst-case latency. |
| `MAX_WINDOW_SECONDS` | `5` | Ceiling on playlist span, guarding latency when segments come out longer than requested. |
| `OFFLINE_CACHE_TTL` | `3` | Edge cache for the offline playlist; worst case before a viewer notices a broadcast started. |
| `SEGMENT_CACHE_TTL` | `30` | Edge cache for immutable segments. |
| `LIVE_EDGE_START` | `1` | Advertise `EXT-X-START` so players begin near the live edge. Set to `0` to omit it. |
| `PSEUDO_VOD` | `0` | Terminate playlists with `ENDLIST` for players that cannot follow a live playlist. See VRChat compatibility. |

## Latency

Measured end to end at the defaults (1s segments, 3 advertised):

| | |
|---|---|
| Encoder fills one segment | 1.0s (unavoidable) |
| Upload until readable | ~0.3-0.6s |
| Player's position behind the edge | ~1s, set by `EXT-X-START` |
| **Total** | **~2.5s**, with the player reporting ~1.2s to the live edge |

Three things get it there, none of which cost any requests — the poll rate
depends only on `SEGMENT_DURATION`:

- **`EXT-X-START`** tells the player to begin one segment behind the live edge
  rather than at the oldest advertised segment.
- **A 3-segment window.** Two is worse, not better: measured with a 2-segment
  window the latency was identical at 2.11s but playback stalled, because the
  player had nothing left to buffer against jitter.
- **`liveSyncDurationCount: 1`** in the bundled player, down from 3.

A player's own latency readout measures only the distance to the live edge and
excludes encoding and upload, so it reads lower than what a viewer sees.

Going below ~2s needs LL-HLS partial segments, which AVPro does not reliably
support; sub-second needs WebRTC, which the VRChat players cannot consume.

**Keyframe interval must match.** `SEGMENT_DURATION` is a request, not a
guarantee — segments can only be cut on a keyframe, so an encoder with a
longer keyframe interval produces longer segments and proportionally more
latency, whatever this is set to. `MAX_WINDOW_SECONDS` caps the damage but
does not prevent it.

## Tolerance

`MAX_SEGMENTS x SEGMENT_DURATION` is how far behind a viewer may drift and
still find the segment they want — so it is simultaneously the jitter
tolerance and the worst-case latency. The default of 8 gives 8s of both.

Drift is the player's choice, not the server's: a viewer on a good connection
tracks the live edge and never requests the deeper segments at all. A shallow
ring makes a straggler rebuffer and catch up rather than sit far behind
everyone else, which suits a shared watch session.

## VRChat compatibility

Verified working: browsers (hls.js), ffmpeg, and VRChat on Windows.

**VRChat under Proton does not play live streams.** AVPro delegates decoding to
Windows Media Foundation, and Proton's translation of it handles
video-on-demand but not the continuous playlist re-reading a live stream
requires. The symptom is video that loads and then stays black, while the same
segments play fine once the playlist claims to be finished.

If you need playback there, set `PSEUDO_VOD = "1"`. Every playlist then ends
with `ENDLIST`, which such a player will decode. The limitation is not
worked around, only traded: playback stops at the end of the window and does
not resume, because a player that has seen `ENDLIST` stops issuing requests
entirely — verified by counting polls server-side, five requests and then
nothing. On Windows, leave it off.

## Design notes

**Media never touches R2 or Durable Object storage.** A live segment is written
once, read for a few seconds, then irrelevant — persisting it buys nothing, and
keeping it in the DO heap also sidesteps the 128 KB per-value limit on DO
storage. Only a few bytes of metadata (last-ingest time, discontinuity counter)
are persisted, so a restarted encoder is still spliced with a proper
`EXT-X-DISCONTINUITY` after an idle eviction.

**Segments are edge-cached, the playlist is not.** Segment filenames are
immutable, so `immutable` caching is always correct and means a single Durable
Object serves one origin pull per segment *per region* rather than per viewer —
which is what makes a single-colo DO viable for a worldwide audience. The
playlist must be `no-store`: hls.js treats a byte-identical live playlist as
"nothing new" and stops loading fragments, so a cached playlist stalls playback.

**`EXT-X-MEDIA-SEQUENCE` is derived from ffmpeg's own `seg%05d` counter**, not
from an internal eviction count. The two drift apart whenever the encoder
restarts, and a mismatch makes players miscompute the live edge.

## Testing without OBS

```sh
ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 \
       -f lavfi -i sine=frequency=440 \
  -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -bf 0 \
  -g 30 -keyint_min 30 -sc_threshold 0 -b:v 2500k -pix_fmt yuv420p \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  -f hls -hls_time 1 -hls_list_size 6 \
  -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \
  -method PUT -http_persistent 1 -ignore_io_errors 1 \
  -hls_segment_filename "http://localhost:8787/ingest/$INGEST_KEY/main/seg%05d.ts" \
  "http://localhost:8787/ingest/$INGEST_KEY/main/live.m3u8"
```

Then verify a real HLS client can play it:

```sh
ffmpeg -i "http://localhost:8787/live/main.m3u8" -t 5 -c copy out.ts
ffprobe out.ts
```

`scripts/smoke.sh` runs that whole loop and checks the results.
`scripts/edge-cases.sh` covers the boundary conditions — offline viewers,
encoder restarts, oversized segments, concurrent streams, malformed ingest,
and the caching the free tier depends on. Both accept a base URL, so they can
run against a deployment as well as locally:

```sh
scripts/smoke.sh      https://<your-worker> "$INGEST_KEY"
scripts/edge-cases.sh https://<your-worker> "$INGEST_KEY"
```

## Offline behaviour

A stream with no live content serves a playable "OFFLINE / WAITING FOR STREAM"
slate rather than an empty playlist. It carries a `KRSZ LIVE` watermark, a
`KRSZ.IN` footer, and a looping sweep animation so viewers can tell the feed is
alive and simply not broadcasting yet.

Regenerate it with `scripts/make-slate.sh` after editing `scripts/slate.py`.
The generated URL carries a content fingerprint (`_offline.<hash>.ts`), so a
regenerated slate is a new URL and reaches viewers immediately despite being
served `immutable` for a year. The unversioned path still works for old
clients but is deliberately short-lived.
The animation is a ping-pong sweep driven by a cosine, so velocity is zero at
both ends of the 2s segment and the loop point is continuous in both position
and motion — no visible jerk each time a player repeats it. Frames are drawn by
a pure-Python PNG writer with a 5x7 bitmap font, so generation needs no fonts,
no Pillow, and no ffmpeg `drawtext` (absent from many builds). Viewers see why there is no picture, and
because the slate is identical on every request it can be cached at the edge —
which is what keeps idle viewers from draining the request quota.

An empty playlist cost ~0.67 req/s per idle viewer indefinitely: three tabs
left open would exhaust a day's free tier showing nothing. The slate playlist
is cached for `OFFLINE_CACHE_TTL` seconds and the status poll backs off from 2s
toward 30s while offline (pausing entirely on a hidden tab), which measured at
~90% edge-hit rate in production.

Ingest purges the cached offline playlist as soon as the first live segment
arrives, so viewers do not sit on the slate waiting out its TTL. Cloudflare's
cache is per-colo and the purge only runs where the ingest request landed, so
`OFFLINE_CACHE_TTL` (3s) remains the worst case for viewers in other regions.
Raise it to spend less quota, at the cost of a slower-looking start.
