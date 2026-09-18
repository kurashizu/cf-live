# cf-live on a VPS

> Deployed and verified on Oracle Linux 9.7 (1 core, 1 GB) behind a Cloudflare
> tunnel. Ingest and playback both work end to end. One thing still needs doing
> by hand: the Cache Rule in the section below — without it every viewer
> request reaches the box.

## What actually runs

Not nginx. The target machine could not install packages without falling over,
and Caddy — already present — has a read-only file server, so it cannot accept
OBS's uploads. `cf-live.py` is a relay in the Python standard library instead:
no packages, no build, ~320 lines.

It receives HLS segments over HTTP PUT, writes them to tmpfs, and serves them
with the playlist format and cache headers established during the Workers
build. systemd keeps it alive and recreates the tmpfs tree on boot.

The same service without the Workers free-tier request limit. Needs only
**nginx** (with the dav module) and **cloudflared** — no RTMP module, no
transcoder, no media server.

A 1-core / 1 GB box is ample: this is HTTP file serving, not transcoding.
Bandwidth is the real constraint, and Cloudflare's cache absorbs most of it.

## Why this replaces the Worker version rather than porting it

Most of the Worker's code exists to work around limits that do not apply here:
a Durable Object ring buffer because Workers cannot hold state, manual
eviction and media-sequence bookkeeping because nothing else would, and an
edge-cache strategy to keep request counts inside a free quota. On a VPS,
ffmpeg writes files and nginx serves them.

What carries over is what was hard to get right:

- **Playlist format matched to SRS** — tag order, `#EXTINF:1.000, no desc`,
  `BANDWIDTH=1` with no `CODECS`. Established by comparing against a stack
  known to play in VRChat; ffmpeg produces most of it, and the master playlist
  here reproduces the rest.
- **Cache headers** — `.ts` immutable, playlists `no-store`. Plain `no-store`
  specifically: `no-cache, must-revalidate` gets stripped in transit, leaving
  no directive at all, at which point a browser caches anyway.
- **The offline slate** — animated, correct codecs, seamless loop, served as a
  rolling playlist so players keep polling and notice the broadcast starting.
- **Reserved path handling** so a stream name cannot shadow an endpoint.

## Install

On your machine:

```sh
./vps/export.sh                 # renders offline.ts and index.html
scp -r vps/ user@host:~/cf-live-vps
```

On the VPS:

```sh
cd ~/cf-live-vps
sudo ./install.sh "$(head -c 30 /dev/urandom | base64 | tr -dc A-Za-z0-9)"
```

The script prints the ingest key it used along with the OBS and playback URLs.

Point cloudflared at nginx:

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /root/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: live.example.com
    service: http://127.0.0.1:8080
  - service: http_status:404
```

nginx listens on `127.0.0.1:8080` only, so the box is not directly exposed.

## Cache rules — do this, or bandwidth is the limit

Cloudflare does not cache through a tunnel by default, and without caching
every viewer's every request reaches the VPS. At 1.2 Mbps per viewer a 40 Mbps
uplink runs out around 33 viewers; with caching, viewer count barely affects
it.

In the dashboard, under **Caching → Cache Rules**, add:

| | |
|---|---|
| When | `URI Path` contains `/live/` and `URI Path` ends with `.ts` |
| Then | Eligible for cache, Edge TTL: respect origin |

Verify with `curl -sSI https://<host>/live/main/seg00001.ts | grep cf-cache-status`
— the second request for the same segment should report `HIT`.

## OBS

Unchanged from the Worker version, except the hostname. In
**Settings → Output → Recording**, Type `Custom Output (FFmpeg)`:

| Field | Value |
|---|---|
| FFmpeg Output Type | `Output to URL` |
| File path or URL | `https://<host>/ingest/<KEY>/main/live.m3u8` |
| Container Format | `hls` |
| **Keyframe interval (frames)** | **`30`** at 30 fps — see below |

Muxer settings:

```
method=PUT http_persistent=1 ignore_io_errors=1 hls_time=1 hls_list_size=6 hls_flags=delete_segments+omit_endlist hls_segment_type=mpegts hls_segment_filename=https://<host>/ingest/<KEY>/main/seg%05d.ts
```

**Keyframe interval is the setting that determines latency.** Segments can
only be cut on a keyframe, so OBS's default of 249 frames produces ~8s
segments no matter what `hls_time` says — which turns a 1s configuration into
20-30s of observed delay. Set it to frame rate × segment duration.

## Playback

```
https://<host>/main
```

`/main.m3u8` also works. Any stream name is created on demand; push to
`/ingest/<KEY>/room2/…` and play `/room2`.

## Where the segments live

`/dev/shm/cf-live/<stream>/` — RAM, not disk. A segment is written once, read
for a few seconds and then deleted by ffmpeg's `delete_segments`, so disk only
adds wear. `/dev/shm` is cleared on reboot; a tmpfiles rule recreates the tree.

At 1.2 Mbps and a 6-segment ffmpeg window, that is under 1 MB per stream.

## Latency

Roughly **2-3s**, slightly better than the Worker version — one less hop, and
no need to trade latency against a request quota. Made up of one segment to
encode (unavoidable), the upload, and the player's own buffer.

Because there is no request quota here, 0.5s segments become viable if you
want to push further; on Workers that doubled the request rate.

## Known nginx pitfalls this config avoids

- **`try_files` does not work with `alias`.** The offline fallback uses
  `error_page 404 = @offline` instead. `try_files` fails by serving the wrong
  path rather than by refusing to start, so it is easy to miss.
- **`create_full_put_path on`** is required, or the first PUT of a broadcast
  fails because the stream's directory does not exist yet.

## Keeping the Worker version

They can coexist: the Worker on `workers.dev` or a second hostname, the VPS on
the main one. The Worker version works and stays within the free tier for a
couple of hours a day with a handful of viewers — worth keeping as a fallback
while the VPS setup proves itself.
