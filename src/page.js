/**
 * Frontend: player preview, live stats, and OBS/API documentation.
 * Served inline by the Worker so the project stays a single deployable unit.
 */

export function landingPage(url, env) {
  const origin = url.origin;
  const segDur = Number(env?.SEGMENT_DURATION ?? 1);
  const playlistSize = Number(env?.PLAYLIST_SIZE ?? 6);
  const maxSegs = Number(env?.MAX_SEGMENTS ?? 10);
  // Players buffer a few segments before starting, which dominates latency.
  const estLatency = segDur * 3 + 1;

  return new Response(html({ origin, segDur, playlistSize, maxSegs, estLatency }), {
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'public, max-age=300',
    },
  });
}

const html = ({ origin, segDur, playlistSize, maxSegs, estLatency }) => `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cf-live — low-latency HLS relay on Cloudflare</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #fff; --fg: #18181b; --muted: #71717a; --line: #e4e4e7;
    --card: #fafafa; --accent: #f6821f; --accent-fg: #fff;
    --code-bg: #f4f4f5; --ok: #16a34a; --off: #a1a1aa; --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --fg: #e8eaed; --muted: #9aa0a6; --line: #272a30;
      --card: #171a20; --code-bg: #1c1f26; --ok: #4ade80;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", ui-sans-serif,
          system-ui, sans-serif;
  }
  .wrap { max-width: 56rem; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }
  header { border-bottom: 1px solid var(--line); padding-bottom: 1.25rem; margin-bottom: 1.5rem; }
  h1 { font-size: 1.6rem; margin: 0 0 .3rem; letter-spacing: -.01em; }
  h1 .dot { color: var(--accent); }
  .tagline { color: var(--muted); margin: 0; font-size: .95rem; }
  h2 { font-size: 1.1rem; margin: 2.5rem 0 .9rem; padding-bottom: .4rem;
       border-bottom: 1px solid var(--line); }
  h3 { font-size: .95rem; margin: 1.5rem 0 .5rem; }
  p { margin: .6rem 0; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         font-size: .875em; background: var(--code-bg); padding: .15em .4em;
         border-radius: 4px; }
  pre { background: var(--code-bg); padding: .9rem 1rem; border-radius: var(--radius);
        overflow-x: auto; font-size: 13px; line-height: 1.55; border: 1px solid var(--line);
        margin: .7rem 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  pre code { background: none; padding: 0; font-size: inherit; }
  .muted { color: var(--muted); }
  .small { font-size: .875rem; }
  .player-card { background: var(--card); border: 1px solid var(--line);
                 border-radius: var(--radius); padding: 1.1rem; margin: 1rem 0; }
  .controls { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center;
              margin-bottom: .85rem; }
  input[type=text] { font: inherit; padding: .45rem .7rem; border: 1px solid var(--line);
                     border-radius: 7px; background: var(--bg); color: var(--fg);
                     min-width: 11rem; }
  button { font: inherit; font-weight: 500; padding: .45rem 1rem; cursor: pointer;
           border: 1px solid transparent; border-radius: 7px; background: var(--accent);
           color: var(--accent-fg); }
  button:hover { filter: brightness(1.07); }
  button.ghost { background: transparent; border-color: var(--line); color: var(--fg); }
  video { width: 100%; aspect-ratio: 16/9; background: #000; border-radius: 8px; display: block; }
  .statusbar { display: flex; gap: 1.1rem; flex-wrap: wrap; align-items: center;
               margin-top: .8rem; font-size: .85rem; color: var(--muted); }
  .badge { display: inline-flex; align-items: center; gap: .4rem; font-weight: 500; }
  .led { width: 8px; height: 8px; border-radius: 50%; background: var(--off); }
  .led.on { background: var(--ok); box-shadow: 0 0 0 3px rgba(22,163,74,.22); }
  .stat b { color: var(--fg); font-variant-numeric: tabular-nums; font-weight: 600; }
  .urlrow { display: flex; gap: .5rem; margin: .6rem 0; }
  .urlrow input { flex: 1; font-family: ui-monospace, Menlo, monospace; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; margin: .8rem 0; }
  th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--line);
           vertical-align: top; }
  th { font-weight: 600; color: var(--muted); font-size: .8rem; text-transform: uppercase;
       letter-spacing: .03em; }
  .method { display: inline-block; font-size: .72rem; font-weight: 700; padding: .1rem .4rem;
            border-radius: 4px; background: var(--accent); color: var(--accent-fg);
            font-family: ui-monospace, monospace; }
  .method.get { background: #2563eb; }
  .note { border-left: 3px solid var(--accent); background: var(--card);
          padding: .7rem 1rem; border-radius: 0 var(--radius) var(--radius) 0;
          margin: 1rem 0; font-size: .9rem; }
  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem,1fr)); gap: .8rem; }
  .kv { background: var(--card); border: 1px solid var(--line);
        border-radius: var(--radius); padding: .8rem .95rem; }
  .kv .k { font-size: .75rem; color: var(--muted); text-transform: uppercase;
           letter-spacing: .04em; }
  .kv .v { font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  ol li, ul li { margin: .35rem 0; }
  footer { margin-top: 3.5rem; padding-top: 1.25rem; border-top: 1px solid var(--line);
           color: var(--muted); font-size: .85rem; }
  a { color: var(--accent); }
</style>
</head><body>
<div class="wrap">

<header>
  <h1>cf-live<span class="dot">.</span></h1>
  <p class="tagline">Low-latency HLS relay on Cloudflare Workers + Durable Objects.
  Ingest from OBS, play anywhere — including VRChat.</p>
</header>

<h2>Preview</h2>
<div class="player-card">
  <div class="controls">
    <input type="text" id="stream" value="main" placeholder="stream name">
    <button onclick="load()">Load</button>
    <button class="ghost" onclick="jumpLive()">Jump to live</button>
    <span class="badge"><span class="led" id="led"></span><span id="livetext">not connected</span></span>
  </div>
  <video id="video" controls playsinline muted autoplay></video>
  <div class="statusbar">
    <span class="stat">buffered <b id="s-segs">–</b></span>
    <span class="stat">sequence <b id="s-seq">–</b></span>
    <span class="stat">total <b id="s-total">–</b></span>
    <span class="stat">memory <b id="s-bytes">–</b></span>
    <span class="stat">latency <b id="s-lat">–</b></span>
  </div>
</div>

<h3>Playback URL</h3>
<p class="small muted">Paste this into the VRChat video player (AVPro or Unity Video Player).</p>
<div class="urlrow">
  <input type="text" id="playurl" readonly>
  <button onclick="copyUrl()">Copy</button>
</div>

<h2>OBS setup</h2>
<p>No plugin needed. OBS's built-in <b>Custom Output (FFmpeg)</b> can write HLS
segments straight to this service over HTTP PUT.</p>

<div class="note">
  <b>Use the Recording panel, not Streaming.</b> OBS's Streaming output only speaks
  RTMP/WHIP, and we need HLS muxing. Start the broadcast with
  <b>Start Recording</b>.
</div>

<h3>1 · Settings → Output → Output Mode: Advanced → Recording tab</h3>
<table>
  <tr><th>Field</th><th>Value</th></tr>
  <tr><td>Type</td><td><code>Custom Output (FFmpeg)</code></td></tr>
  <tr><td>FFmpeg Output Type</td><td><code>Output to URL</code></td></tr>
  <tr><td>File path or URL</td><td><code id="obs-url"></code></td></tr>
  <tr><td>Container Format</td><td><code>hls</code></td></tr>
  <tr><td>Video Encoder</td><td><code>libx264</code></td></tr>
  <tr><td>Audio Encoder</td><td><code>aac</code></td></tr>
</table>

<h3>2 · Muxer Settings</h3>
<p class="small muted">Copy this whole line into the "Muxer Settings" field:</p>
<pre><code id="muxer"></code></pre>

<h3>3 · Video Encoder Settings</h3>
<p class="small muted">The keyframe interval must equal the segment duration, or
segments cannot be cut cleanly:</p>
<pre><code id="vencoder"></code></pre>
<p class="small muted">
  <code>g</code> = frame rate × segment duration. At ${segDur}s segments:
  30 fps → <code>g=${30 * segDur}</code>, 60 fps → <code>g=${60 * segDur}</code>.
</p>

<h3>4 · Recommended settings</h3>
<ul class="small">
  <li><b>Resolution / FPS:</b> VRChat screens are usually small. <code>1280×720 @30fps</code>
      at <code>2500 Kbps</code> is plenty and noticeably reduces stalling.</li>
  <li><b>Audio:</b> AAC, <code>128 Kbps</code>, <code>48 kHz</code>, stereo. AVPro can
      misbehave at other sample rates.</li>
  <li><b>H.264 profile:</b> use <code>main</code> or <code>baseline</code>. Quest builds
      of AVPro do not decode <code>high10</code> and similar.</li>
  <li><b>B-frames:</b> keep them low or zero (<code>bf=0</code>) to reduce decode delay.</li>
</ul>

<h3>5 · Test without OBS</h3>
<p class="small muted">Verify the whole path with ffmpeg alone:</p>
<pre><code id="ffmpegcmd"></code></pre>

<h2>API</h2>

<h3>Ingest (requires key)</h3>
<table>
  <tr><th>Method</th><th>Path</th><th>Description</th></tr>
  <tr><td><span class="method">PUT</span></td>
      <td><code>/ingest/:key/:stream/live.m3u8</code></td>
      <td>ffmpeg's own playlist. Only parsed for authoritative per-segment
          durations; never served to viewers.</td></tr>
  <tr><td><span class="method">PUT</span></td>
      <td><code>/ingest/:key/:stream/:file.ts</code></td>
      <td>One MPEG-TS segment, pushed into the stream's in-memory ring buffer.</td></tr>
  <tr><td><span class="method">DELETE</span></td>
      <td><code>/ingest/:key/:stream/:file</code></td>
      <td>Sent by ffmpeg's <code>delete_segments</code>. Acknowledged only — eviction
          follows our own ring policy so it cannot race active viewers.</td></tr>
</table>
<p class="small muted">
  <code>:key</code> is the <code>INGEST_KEY</code> secret; a wrong key returns
  <code>403</code>. Stream and file names are limited to
  <code>[A-Za-z0-9._-]</code>, max 128 characters.
</p>

<h3>Playback (public)</h3>
<table>
  <tr><th>Method</th><th>Path</th><th>Description</th></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/live/:stream.m3u8</code></td>
      <td>HLS media playlist, ${playlistSize}-segment sliding window, never cached.
          <b>This is the VRChat URL.</b></td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/live/:stream/:file.ts</code></td>
      <td>Segment bytes. Immutable, so cached at the edge — only the first viewer
          per region reaches the Durable Object.</td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/status/:stream</code></td>
      <td>JSON: live flag, buffered segments, sequence, bytes, uptime.</td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/healthz</code></td>
      <td>Liveness check.</td></tr>
</table>

<h3>Example <code>/status/:stream</code></h3>
<pre><code>{
  "live": true,
  "segmentsBuffered": ${maxSegs},
  "mediaSequence": 412,
  "totalSegments": 420,
  "targetDuration": ${segDur},
  "playlistSize": ${playlistSize},
  "maxSegments": ${maxSegs},
  "bufferedBytes": 3422352,
  "secondsSinceLastIngest": 0.31,
  "uptimeSeconds": 420.5
}</code></pre>

<h3>Multiple streams</h3>
<p class="small">Any <code>:stream</code> name is created on demand and gets its own
Durable Object. Push to <code>/ingest/&lt;KEY&gt;/room2/…</code> and play
<code>/live/room2.m3u8</code> — no configuration required.</p>

<h2>How it works</h2>
<div class="grid2">
  <div class="kv"><div class="k">Segment</div><div class="v">${segDur}s</div></div>
  <div class="kv"><div class="k">Playlist window</div><div class="v">${playlistSize}</div></div>
  <div class="kv"><div class="k">Ring buffer</div><div class="v">${maxSegs}</div></div>
  <div class="kv"><div class="k">Expected latency</div><div class="v">~${estLatency}s</div></div>
</div>

<pre><code>OBS ──PUT segments──> Worker ──> Durable Object
                        │          (in-memory ring, ${maxSegs} segments)
                        │               │
    viewers <──edge cache┴───────────────┘
    (.m3u8: no-store · .ts: immutable)</code></pre>

<p class="small">
  Media never touches R2 or Durable Object storage. A live segment is written once,
  read for a few seconds, then irrelevant — persisting it buys nothing, and keeping
  it in the heap also sidesteps the 128 KB per-value limit on DO storage. Segments
  are served through the Cloudflare edge cache, so a single Durable Object only
  handles one origin pull per segment per region rather than one per viewer.
</p>

<div class="note">
  <b>Why latency stops here:</b> HLS latency ≈ segment duration × segments buffered
  by the player. Going lower needs LL-HLS partial segments, which AVPro does not
  reliably support, so ${segDur}s segments are the practical floor on this path.
  Sub-second delivery requires WebRTC, and the VRChat video players cannot consume it.
</div>

<footer>cf-live · runs within the Cloudflare Workers free tier ·
  <a href="/healthz">health</a></footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
const ORIGIN = location.origin;
const SEG_DUR = ${segDur};
let hls = null, statusTimer = null, currentStream = null;

function streamName() {
  const raw = document.getElementById('stream').value || 'main';
  return raw.trim().replace(/[^A-Za-z0-9._-]/g, '') || 'main';
}
function playUrl(n) { return ORIGIN + '/live/' + encodeURIComponent(n) + '.m3u8'; }

function refreshDocs() {
  const n = streamName();
  document.getElementById('playurl').value = playUrl(n);
  document.getElementById('obs-url').textContent =
    ORIGIN + '/ingest/<KEY>/' + n + '/live.m3u8';
  document.getElementById('muxer').textContent =
    'method=PUT http_persistent=1 ignore_io_errors=1 ' +
    'hls_time=' + SEG_DUR + ' hls_list_size=6 ' +
    'hls_flags=delete_segments+omit_endlist hls_segment_type=mpegts ' +
    'hls_segment_filename=' + ORIGIN + '/ingest/<KEY>/' + n + '/seg%05d.ts';
  document.getElementById('vencoder').textContent =
    'preset=veryfast tune=zerolatency profile=main bf=0 ' +
    'g=' + (30 * SEG_DUR) + ' keyint_min=' + (30 * SEG_DUR) + ' sc_threshold=0';
  document.getElementById('ffmpegcmd').textContent =
    'ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 \\\\\\n' +
    '       -f lavfi -i sine=frequency=440 \\\\\\n' +
    '  -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -bf 0 \\\\\\n' +
    '  -g ' + (30*SEG_DUR) + ' -keyint_min ' + (30*SEG_DUR) +
        ' -sc_threshold 0 -b:v 2500k -pix_fmt yuv420p \\\\\\n' +
    '  -c:a aac -b:a 128k -ar 48000 -ac 2 \\\\\\n' +
    '  -f hls -hls_time ' + SEG_DUR + ' -hls_list_size 6 \\\\\\n' +
    '  -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \\\\\\n' +
    '  -method PUT -http_persistent 1 -ignore_io_errors 1 \\\\\\n' +
    '  -hls_segment_filename "' + ORIGIN + '/ingest/<KEY>/' + n + '/seg%05d.ts" \\\\\\n' +
    '  "' + ORIGIN + '/ingest/<KEY>/' + n + '/live.m3u8"';
}

function load() {
  const name = streamName();
  currentStream = name;
  refreshDocs();
  const src = playUrl(name);
  const v = document.getElementById('video');

  if (hls) { hls.destroy(); hls = null; }
  if (window.Hls && Hls.isSupported()) {
    // lowLatencyMode stays off on purpose: it makes hls.js wait for EXT-X-PART
    // partial segments, which this plain-HLS playlist never provides.
    hls = new Hls({
      lowLatencyMode: false,
      liveSyncDurationCount: 3,
      maxBufferLength: 10,
      manifestLoadingMaxRetry: 999,  // the stream may not be live yet
      levelLoadingMaxRetry: 999,
      fragLoadingMaxRetry: 6,
    });
    hls.loadSource(src);
    hls.attachMedia(v);
    hls.on(Hls.Events.ERROR, (_e, d) => {
      if (!d.fatal) return;
      // A stream that has not started yet is normal here — recover, don't die.
      if (d.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
      else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
    });
  } else {
    v.src = src;  // Safari and iOS play HLS natively
  }
  v.play().catch(() => {});
  startStatus(name);
}

function jumpLive() {
  const v = document.getElementById('video');
  if (hls && hls.liveSyncPosition != null) v.currentTime = hls.liveSyncPosition;
  else if (v.seekable.length) v.currentTime = v.seekable.end(v.seekable.length - 1);
  v.play().catch(() => {});
}

function copyUrl() {
  const el = document.getElementById('playurl');
  el.select();
  navigator.clipboard?.writeText(el.value);
}

function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}

async function startStatus(name) {
  if (statusTimer) clearTimeout(statusTimer);
  const tick = async () => {
    if (currentStream !== name) return;
    try {
      const r = await fetch('/status/' + encodeURIComponent(name), { cache: 'no-store' });
      const j = await r.json();
      document.getElementById('led').className = 'led' + (j.live ? ' on' : '');
      document.getElementById('livetext').textContent = j.live ? 'live' : 'offline';
      document.getElementById('s-segs').textContent = j.segmentsBuffered;
      document.getElementById('s-seq').textContent = j.mediaSequence;
      document.getElementById('s-total').textContent = j.totalSegments;
      document.getElementById('s-bytes').textContent = fmtBytes(j.bufferedBytes);
      const v = document.getElementById('video');
      let lat = '–';
      if (hls && hls.latency) lat = hls.latency.toFixed(1) + 's';
      else if (v.seekable.length) {
        lat = Math.max(0, v.seekable.end(v.seekable.length - 1) - v.currentTime).toFixed(1) + 's';
      }
      document.getElementById('s-lat').textContent = lat;
    } catch {
      document.getElementById('led').className = 'led';
      document.getElementById('livetext').textContent = 'status unavailable';
    }
    statusTimer = setTimeout(tick, 2000);
  };
  tick();
}

document.getElementById('stream').addEventListener('input', refreshDocs);
refreshDocs();
load();
</script>
</body></html>`;
