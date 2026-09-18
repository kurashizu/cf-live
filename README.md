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
| Video Encoder | `libx264` |
| Audio Encoder | `aac` |

**Muxer Settings** (one line):

```
method=PUT http_persistent=1 ignore_io_errors=1 hls_time=1 hls_list_size=6 hls_flags=delete_segments+omit_endlist hls_segment_type=mpegts hls_segment_filename=https://<your-worker>/ingest/<KEY>/main/seg%05d.ts
```

**Video Encoder Settings** — the keyframe interval must equal the segment
duration, or segments cannot be cut cleanly:

```
preset=veryfast tune=zerolatency profile=main bf=0 g=30 keyint_min=30 sc_threshold=0
```

`g` = frame rate × `hls_time`. At 1s segments: 30 fps → `g=30`, 60 fps → `g=60`.

`ignore_io_errors=1` matters: it keeps a long broadcast alive through a transient
upload failure instead of ending the recording.

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
| `SEGMENT_DURATION` | `1` | Seconds per segment. Must match OBS's `hls_time`. |
| `MAX_SEGMENTS` | `10` | Segments held in memory. Must exceed `PLAYLIST_SIZE`. |
| `PLAYLIST_SIZE` | `6` | Segments advertised in the playlist. |
| `SEGMENT_CACHE_TTL` | `30` | Edge cache lifetime for immutable segments. |

## Latency

End-to-end latency ≈ segment duration × segments the player buffers before
starting, plus upload and CDN propagation. With 1s segments this lands around
**4–6 seconds**.

That is the floor for this architecture. Going lower requires LL-HLS partial
segments, which AVPro does not reliably support; sub-second needs WebRTC, which
the VRChat players cannot consume.

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
