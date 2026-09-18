# cf-live

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
| **Keyframe interval (frames)** | **`60`** — see below, this one controls latency |
| Video Bitrate | `2500` Kbps |
| Video Encoder | `libx264` |
| Audio Encoder | `aac` |

**Muxer Settings** (one line):

```
method=PUT http_persistent=1 ignore_io_errors=1 hls_time=2 hls_list_size=6 hls_flags=delete_segments+omit_endlist hls_segment_type=mpegts hls_segment_filename=https://<your-worker>/ingest/<KEY>/main/seg%05d.ts
```

**Keyframe interval** is the setting that actually determines latency, and OBS
gets it wrong by default.

A segment can only be cut on a keyframe. OBS ships with **249** frames in the
`Keyframe interval (frames)` field, which is ~8s at 30 fps — so `hls_time=2` is
ignored and you get 8s segments. That alone turns a 2s configuration into
20-30s of observed latency.

Set it to **frame rate × segment duration**: `60` at 30 fps, `120` at 60 fps.

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
| `SEGMENT_DURATION` | `2` | Seconds per segment. Must match OBS's `hls_time`. |
| `MAX_SEGMENTS` | `6` | Segments held in memory. Raised automatically to `PLAYLIST_SIZE + 1` if set lower. |
| `PLAYLIST_SIZE` | `4` | Segments advertised in the playlist. |
| `MAX_WINDOW_SECONDS` | `8` | Ceiling on how many seconds the playlist may span. Guards startup latency when the encoder emits segments longer than `SEGMENT_DURATION`. |
| `SEGMENT_CACHE_TTL` | `30` | Edge cache lifetime for immutable segments. |

## Latency and tolerance

Two different numbers, set by two different knobs:

**Startup latency** — how far behind a viewer begins — is bounded by
`PLAYLIST_SIZE`, since a player can only start on a segment the playlist
advertises. At the defaults (2s segments, 4 advertised):

| | |
|---|---|
| Encoder fills one segment | 2.0s (unavoidable) |
| Upload until readable | ~0.5s (measured 0.04–0.85s) |
| Player honours `EXT-X-START` | **~5.5s total** |
| Player starts at the oldest advertised segment | **~8.5s total** |

**Worst-case steady-state latency** — how far behind a viewer may drift and
still keep playing — equals the tolerance window, `MAX_SEGMENTS × segment
duration`, because that is how much history stays in memory. At the defaults
that is 12s of tolerance, so ~14.5s worst case.

These conflict directly: a deeper ring absorbs more network jitter without a
rebuffer, but also lets a viewer sit further behind. The default is
deliberately shallow — for a shared watch session, a straggler rebuffering and
catching up beats them drifting 30s behind everyone else.

Note that drift is the player's choice, not the server's. A viewer on a good
connection always tracks the live edge, and never requests the deeper segments
at all.

Going below ~5s requires LL-HLS partial segments, which AVPro does not reliably
support; sub-second needs WebRTC, which the VRChat players cannot consume.

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
  -g 60 -keyint_min 60 -sc_threshold 0 -b:v 2500k -pix_fmt yuv420p \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  -f hls -hls_time 2 -hls_list_size 6 \
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
slate rather than an empty playlist. Viewers see why there is no picture, and
because the slate is identical on every request it can be cached at the edge —
which is what keeps idle viewers from draining the request quota.

An empty playlist cost ~0.67 req/s per idle viewer indefinitely: three tabs
left open would exhaust a day's free tier showing nothing. The slate playlist
is cached for `OFFLINE_CACHE_TTL` seconds and the status poll backs off from 2s
toward 30s while offline (pausing entirely on a hidden tab), which measured at
~90% edge-hit rate in production.

The trade-off is that a viewer may keep seeing the slate for up to
`OFFLINE_CACHE_TTL` seconds after a broadcast actually starts. Lower it for a
faster start, raise it to spend less quota.
