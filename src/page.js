/**
 * Frontend: player preview, live stats, and interactive OBS/API documentation.
 * Served inline by the Worker so the project stays a single deployable unit.
 *
 * Every snippet a user has to transfer into OBS gets a copy button, and the
 * ingest key is substituted into all of them from a local-only input so the
 * copied text is immediately usable rather than containing a placeholder.
 */

export function landingPage(url, env) {
  const origin = url.origin;
  const segDur = Number(env?.SEGMENT_DURATION ?? 1);
  const playlistSize = Number(env?.PLAYLIST_SIZE ?? 6);
  const maxSegs = Number(env?.MAX_SEGMENTS ?? 10);
  // Measured, not guessed: one segment to encode, ~0.6s to upload, the window
  // depth a player starts behind by, plus its own 2-3s buffer.
  const estLow = segDur + 0.6 + (playlistSize - 1) * segDur + 2;
  const estHigh = estLow + 1.5;
  const estLatency = `${estLow.toFixed(1)}-${estHigh.toFixed(1)}`;

  return new Response(html({ origin, segDur, playlistSize, maxSegs, estLatency }), {
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      // The page embeds its own script, so caching the HTML caches the player
      // logic with it — a stale copy meant an ordinary reload kept showing
      // OFFLINE and only a force-reload recovered.
      //
      // Plain no-store, not "no-cache, must-revalidate": Cloudflare strips
      // that combination on the way out, leaving the response with no caching
      // directive at all, at which point a browser applies its own heuristics
      // and caches it anyway. The same substitution was needed for the live
      // playlist.
      'Cache-Control': 'no-store',
      // Belt and braces for intermediaries that predate Cache-Control.
      'Pragma': 'no-cache',
      'Expires': '0',
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
    --bg:#fff; --fg:#18181b; --muted:#71717a; --line:#e4e4e7;
    --card:#fafafa; --accent:#f6821f; --accent-fg:#fff;
    --code-bg:#f4f4f5; --ok:#16a34a; --off:#a1a1aa; --radius:10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0f1115; --fg:#e8eaed; --muted:#9aa0a6; --line:#272a30;
      --card:#171a20; --code-bg:#1c1f26; --ok:#4ade80;
    }
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--fg);
    font:15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", ui-sans-serif, system-ui, sans-serif;
  }
  .wrap { max-width:56rem; margin:0 auto; padding:2.5rem 1.25rem 5rem; }
  header { border-bottom:1px solid var(--line); padding-bottom:1.25rem; margin-bottom:1.5rem; }
  h1 { font-size:1.6rem; margin:0 0 .3rem; letter-spacing:-.01em; }
  h1 .dot { color:var(--accent); }
  .tagline { color:var(--muted); margin:0; font-size:.95rem; }
  h2 { font-size:1.1rem; margin:2.5rem 0 .9rem; padding-bottom:.4rem; border-bottom:1px solid var(--line); }
  h3 { font-size:.95rem; margin:1.5rem 0 .5rem; }
  p { margin:.6rem 0; }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:.875em;
         background:var(--code-bg); padding:.15em .4em; border-radius:4px; }
  .muted { color:var(--muted); } .small { font-size:.875rem; }
  a { color:var(--accent); }

  /* ---- copyable code block ---- */
  .snip { position:relative; margin:.7rem 0; }
  .snip pre {
    background:var(--code-bg); padding:.9rem 1rem; border-radius:var(--radius);
    font-size:13px; line-height:1.55; border:1px solid var(--line);
    margin:0; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    /* Wrap instead of scrolling horizontally: these snippets are single long
       lines (muxer settings, ffmpeg commands) and a scrollbar would slide the
       text underneath the floating copy button. */
    white-space:pre-wrap; word-break:break-word; overflow-wrap:anywhere; tab-size:2;
  }
  /* Reserve room on the first line so the button never overlaps text. */
  .snip pre code { display:block; }
  .snip pre::before { content:''; float:right; width:3.2rem; height:1.2rem; }
  .snip pre code { background:none; padding:0; font-size:inherit; }
  .copy {
    position:absolute; top:.5rem; right:.5rem; font:inherit; font-size:.75rem; font-weight:600;
    padding:.25rem .55rem; border:1px solid var(--line); border-radius:6px;
    background:var(--bg); color:var(--muted); cursor:pointer; opacity:.85;
    transition:opacity .12s, color .12s, border-color .12s;
  }
  .copy:hover { opacity:1; color:var(--fg); }
  .copy.done { color:var(--ok); border-color:var(--ok); opacity:1; }

  /* ---- key input ---- */
  .keybox {
    background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
    padding:.9rem 1.05rem; margin:1rem 0;
  }
  .keybox label { display:block; font-size:.82rem; font-weight:600; margin-bottom:.4rem; }
  .keybox .hint { font-size:.8rem; color:var(--muted); margin-top:.45rem; }
  .keyrow { display:flex; gap:.5rem; }
  input[type=text], input[type=password] {
    font:inherit; padding:.45rem .7rem; border:1px solid var(--line); border-radius:7px;
    background:var(--bg); color:var(--fg); min-width:8rem;
  }
  .keyrow input { flex:1; font-family:ui-monospace,Menlo,monospace; font-size:13px; }
  button {
    font:inherit; font-weight:500; padding:.45rem 1rem; cursor:pointer;
    border:1px solid transparent; border-radius:7px; background:var(--accent); color:var(--accent-fg);
  }
  button:hover { filter:brightness(1.07); }
  button.ghost { background:transparent; border-color:var(--line); color:var(--fg); }
  button.flash { border-color:var(--accent); color:var(--accent); font-variant-numeric:tabular-nums; }

  /* ---- quick start ---- */
  .qs { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
        padding:1.1rem 1.2rem; margin:1.2rem 0; }
  .qs ol { margin:0; padding-left:1.3rem; }
  .qs li { margin:.9rem 0; }
  .qs li:first-child { margin-top:.2rem; }
  .qs .what { font-weight:600; font-size:.9rem; }
  .qs .where { font-size:.8rem; color:var(--muted); margin-bottom:.1rem; }
  .qs .hint-sm { font-size:.8rem; color:var(--muted); margin-top:.3rem; line-height:1.5; }
  .warn-inline { font-size:.75rem; font-weight:600; color:var(--accent);
                 border:1px solid var(--accent); border-radius:4px; padding:.05rem .35rem; }

  /* ---- player ---- */
  .player-card { background:var(--card); border:1px solid var(--line);
                 border-radius:var(--radius); padding:1.1rem; margin:1rem 0; }
  .controls { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; margin-bottom:.85rem; }
  video { width:100%; aspect-ratio:16/9; background:#000; border-radius:8px; display:block; }
  .statusbar { display:flex; gap:1.1rem; flex-wrap:wrap; align-items:center;
               margin-top:.8rem; font-size:.85rem; color:var(--muted); }
  .badge { display:inline-flex; align-items:center; gap:.4rem; font-weight:500; }
  .led { width:8px; height:8px; border-radius:50%; background:var(--off); }
  .led.on { background:var(--ok); box-shadow:0 0 0 3px rgba(22,163,74,.22); }
  .stat b { color:var(--fg); font-variant-numeric:tabular-nums; font-weight:600; }

  /* ---- tables ---- */
  table { width:100%; border-collapse:collapse; font-size:.9rem; margin:.8rem 0; }
  th, td { text-align:left; padding:.5rem .6rem; border-bottom:1px solid var(--line); vertical-align:top; }
  th { font-weight:600; color:var(--muted); font-size:.8rem; text-transform:uppercase; letter-spacing:.03em; }
  .cfg td:nth-child(2) { width:100%; }
  .cfgval { display:flex; gap:.5rem; align-items:flex-start; }
  .cfgval code {
    flex:1; word-break:break-all; background:var(--code-bg);
    padding:.3rem .5rem; border-radius:5px; font-size:.8rem; line-height:1.5;
  }
  .minicopy {
    flex:0 0 auto; font:inherit; font-size:.7rem; font-weight:600; padding:.2rem .45rem;
    border:1px solid var(--line); border-radius:5px; background:var(--bg);
    color:var(--muted); cursor:pointer;
  }
  .minicopy:hover { color:var(--fg); }
  .minicopy.done { color:var(--ok); border-color:var(--ok); }
  .method { display:inline-block; font-size:.72rem; font-weight:700; padding:.1rem .4rem;
            border-radius:4px; background:var(--accent); color:var(--accent-fg);
            font-family:ui-monospace,monospace; }
  .method.get { background:#2563eb; }

  .note { border-left:3px solid var(--accent); background:var(--card);
          padding:.7rem 1rem; border-radius:0 var(--radius) var(--radius) 0;
          margin:1rem 0; font-size:.9rem; }
  .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(11rem,1fr)); gap:.8rem; }
  .kv { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
        padding:.8rem .95rem; }
  .kv .k { font-size:.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .kv .v { font-size:1.15rem; font-weight:600; font-variant-numeric:tabular-nums; }
  ul li, ol li { margin:.35rem 0; }
  footer { margin-top:3.5rem; padding-top:1.25rem; border-top:1px solid var(--line);
           color:var(--muted); font-size:.85rem; }
</style>
</head><body>
<div class="wrap">

<header>
  <h1>cf-live<span class="dot">.</span></h1>
  <p class="tagline">Low-latency HLS relay on Cloudflare Workers + Durable Objects.
  Ingest from OBS, play anywhere — including VRChat.</p>
</header>

<!-- ============ KEY ============ -->
<div class="keybox">
  <label for="key">Ingest key</label>
  <div class="keyrow">
    <input type="text" id="key" placeholder="paste your INGEST_KEY to fill in every snippet below"
           autocomplete="off" spellcheck="false">
    <button class="ghost" onclick="forgetKey()">Forget</button>
  </div>
  <div class="hint">Stored in this browser only (localStorage) and substituted into the
  commands below so you can copy them ready to use. It is never sent anywhere.
  Leave blank to see <code>&lt;KEY&gt;</code> placeholders instead.</div>
</div>

<!-- ============ QUICK START ============ -->
<h2>Quick start</h2>
<div class="qs">
  <ol>
    <li>
      <div class="where">OBS → Settings → Output → Output Mode: <b>Advanced</b> → <b>Recording</b> tab →
        Type: <code>Custom Output (FFmpeg)</code>, FFmpeg Output Type: <code>Output to URL</code></div>
      <div class="what">Paste as the URL</div>
      <div class="snip"><pre><code id="qs-url"></code></pre></div>
    </li>
    <li>
      <div class="where">Same screen → Container Format: <code>hls</code> → <b>Muxer Settings</b></div>
      <div class="what">Paste as the muxer settings</div>
      <div class="snip"><pre><code id="qs-mux"></code></pre></div>
    </li>
    <li>
      <div class="where">Same screen → <b>Keyframe interval (frames)</b> — its own
        numeric field, below Video Bitrate</div>
      <div class="what">Set to <code id="qs-gop"></code> &nbsp;<span class="warn-inline">most
        important setting on this page</span></div>
      <div class="hint-sm">OBS defaults this to <code>249</code>, which forces
        ~8s segments no matter what <code>hls_time</code> says — segments can only
        be cut on a keyframe. This field overrides any <code>g=</code> you put in
        the encoder settings, so it must be set here.</div>
    </li>
    <li>
      <div class="where">Press <b>Start Recording</b>, then in VRChat</div>
      <div class="what">Paste into the video player</div>
      <div class="snip"><pre><code id="qs-play"></code></pre></div>
    </li>
  </ol>
</div>

<!-- ============ PLAYER ============ -->
<h2>Preview</h2>
<div class="player-card">
  <div class="controls">
    <input type="text" id="stream" value="main" placeholder="stream name">
    <button onclick="load()">Load</button>
    <button class="ghost" id="btn-live" onclick="jumpLive()"
            title="Skip ahead to the newest content without reloading the player">Jump to live</button>
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

<h3>Playback URLs</h3>
<p class="small muted">All three serve a master playlist pointing at the live
media playlist — the shape players in the VRChat ecosystem expect. The shortest
form is easiest to type in; the <code>.m3u8</code> form is safest if a player
insists on the extension.</p>
<table class="cfg">
  <tr><td class="small muted" style="white-space:nowrap">shortest</td>
      <td><div class="cfgval"><code id="u-short"></code><button class="minicopy" data-copy="u-short">Copy</button></div></td></tr>
  <tr><td class="small muted" style="white-space:nowrap">with extension</td>
      <td><div class="cfgval"><code id="u-ext"></code><button class="minicopy" data-copy="u-ext">Copy</button></div></td></tr>
  <tr><td class="small muted" style="white-space:nowrap">explicit</td>
      <td><div class="cfgval"><code id="u-full"></code><button class="minicopy" data-copy="u-full">Copy</button></div></td></tr>
</table>

<!-- ============ OBS ============ -->
<h2>OBS configuration</h2>
<div class="note">
  <b>Use the Recording panel, not Streaming.</b> OBS's streaming output only speaks
  RTMP/WHIP, and this service needs HLS muxing. Start the broadcast with
  <b>Start Recording</b>.
</div>

<h3>Settings → Output → Advanced → Recording</h3>
<table class="cfg">
  <tr><th>Field</th><th>Value</th></tr>
  <tr><td>Type</td>
      <td><div class="cfgval"><code id="f-type">Custom Output (FFmpeg)</code><button class="minicopy" data-copy="f-type">Copy</button></div></td></tr>
  <tr><td>FFmpeg Output Type</td>
      <td><div class="cfgval"><code id="f-otype">Output to URL</code><button class="minicopy" data-copy="f-otype">Copy</button></div></td></tr>
  <tr><td>File path or URL</td>
      <td><div class="cfgval"><code id="f-url"></code><button class="minicopy" data-copy="f-url">Copy</button></div></td></tr>
  <tr><td>Container Format</td>
      <td><div class="cfgval"><code id="f-fmt">hls</code><button class="minicopy" data-copy="f-fmt">Copy</button></div></td></tr>
  <tr><td>Muxer Settings</td>
      <td><div class="cfgval"><code id="f-mux"></code><button class="minicopy" data-copy="f-mux">Copy</button></div></td></tr>
  <tr><td><b>Keyframe interval (frames)</b><br>
          <span class="small muted">own numeric field, not encoder settings</span></td>
      <td><div class="cfgval"><code id="f-gop"></code><button class="minicopy" data-copy="f-gop">Copy</button></div></td></tr>
  <tr><td>Video Bitrate</td>
      <td><div class="cfgval"><code id="f-rate">2500</code><button class="minicopy" data-copy="f-rate">Copy</button></div></td></tr>
  <tr><td>Video Encoder</td>
      <td><div class="cfgval"><code id="f-venc">libx264</code><button class="minicopy" data-copy="f-venc">Copy</button></div></td></tr>
  <tr><td>Video Encoder Settings</td>
      <td><div class="cfgval"><code id="f-vset"></code><button class="minicopy" data-copy="f-vset">Copy</button></div></td></tr>
  <tr><td>Audio Encoder</td>
      <td><div class="cfgval"><code id="f-aenc">aac</code><button class="minicopy" data-copy="f-aenc">Copy</button></div></td></tr>
</table>

<div class="note">
  <b>Keyframe interval is the setting that actually controls latency.</b>
  A segment can only be cut on a keyframe, so if keyframes are 8s apart,
  <code>hls_time=${segDur}</code> is ignored and you get 8s segments — which is
  what turns a ${segDur}s configuration into 20-30s of observed latency.
  <br><br>
  OBS ships with <code>249</code> frames here. Set it to
  <b>frame rate × ${segDur}</b>: <code>${30 * segDur}</code> at 30 fps,
  <code>${60 * segDur}</code> at 60 fps. The dedicated
  <b>Keyframe interval</b> field wins over any <code>g=</code> in the encoder
  settings box, so setting it there alone has no effect.
</div>

<h3>Recommended settings</h3>
<ul class="small">
  <li><b>1280×720 @ 30 fps, 2500 Kbps.</b> VRChat screens are small; this looks fine
      and noticeably reduces stalling.</li>
  <li><b>AAC, 128 Kbps, 48 kHz, stereo.</b> AVPro can misbehave at other sample rates.</li>
  <li><b>H.264 <code>main</code> or <code>baseline</code> profile.</b> Quest builds do not
      decode <code>high10</code>.</li>
  <li><b><code>bf=0</code></b> (no B-frames) to reduce decode delay.</li>
</ul>

<h3>Test without OBS</h3>
<p class="small muted">Verify the whole path with ffmpeg alone:</p>
<div class="snip"><pre><code id="cmd-ffmpeg"></code></pre></div>

<p class="small muted">Then confirm a real HLS client can play it back:</p>
<div class="snip"><pre><code id="cmd-verify"></code></pre></div>

<!-- ============ API ============ -->
<h2>API</h2>

<h3>Ingest — requires key</h3>
<table>
  <tr><th>Method</th><th>Path</th><th>Description</th></tr>
  <tr><td><span class="method">PUT</span></td>
      <td><code>/ingest/:key/:stream/live.m3u8</code></td>
      <td>ffmpeg's own playlist. Parsed only for authoritative per-segment
          durations; never served to viewers.</td></tr>
  <tr><td><span class="method">PUT</span></td>
      <td><code>/ingest/:key/:stream/:file.ts</code></td>
      <td>One MPEG-TS segment, pushed into the stream's in-memory ring buffer.</td></tr>
  <tr><td><span class="method">DELETE</span></td>
      <td><code>/ingest/:key/:stream/:file</code></td>
      <td>Sent by ffmpeg's <code>delete_segments</code>. Acknowledged only — eviction
          follows our own ring policy so it cannot race active viewers.</td></tr>
</table>
<p class="small muted">A wrong key returns <code>403</code>. Stream and file names are
limited to <code>[A-Za-z0-9._-]</code>, max 128 characters.</p>

<h3>Playback — public</h3>
<table>
  <tr><th>Method</th><th>Path</th><th>Description</th></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/:stream</code></td>
      <td>Shortest playlist URL. Reserved names (<code>healthz</code>,
          <code>status</code>, <code>live</code>, <code>ingest</code>, …) are not
          usable as stream names.</td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/:stream.m3u8</code></td>
      <td>Same playlist, with the extension some players require.</td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/live/:stream.m3u8</code></td>
      <td>Explicit form. ${playlistSize}-segment sliding window, never cached.</td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/live/:stream/index.m3u8</code></td>
      <td>The media playlist the master points at. Sliding window, never cached.</td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/live/:stream/:file.ts</code></td>
      <td>Segment bytes. Immutable, so edge-cached — only the first viewer per
          region reaches the Durable Object.</td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/status/:stream</code></td>
      <td>JSON: live flag, buffered segments, sequence, bytes, uptime.</td></tr>
  <tr><td><span class="method get">GET</span></td>
      <td><code>/healthz</code></td>
      <td>Liveness check.</td></tr>
</table>

<h3>Example <code>/status/:stream</code></h3>
<div class="snip"><pre><code id="ex-status">{
  "live": true,
  "segmentsBuffered": ${maxSegs},
  "mediaSequence": 412,
  "discontinuitySequence": 0,
  "totalSegments": 420,
  "targetDuration": ${segDur},
  "playlistSize": ${playlistSize},
  "maxSegments": ${maxSegs},
  "bufferedBytes": 3422352,
  "secondsSinceLastIngest": 0.31,
  "uptimeSeconds": 420.5
}</code></pre></div>

<h3>Multiple streams</h3>
<p class="small">Any stream name is created on demand and gets its own Durable Object.
Push to <code>/ingest/&lt;KEY&gt;/room2/…</code> and play <code>/room2</code> — no
configuration required.</p>

<!-- ============ ARCHITECTURE ============ -->
<h2>How it works</h2>
<div class="grid2">
  <div class="kv"><div class="k">Segment</div><div class="v">${segDur}s</div></div>
  <div class="kv"><div class="k">Playlist window</div><div class="v">${playlistSize}</div></div>
  <div class="kv"><div class="k">Ring buffer</div><div class="v">${maxSegs}</div></div>
  <div class="kv"><div class="k">Expected latency</div><div class="v">~${estLatency}s</div></div>
</div>

<div class="snip"><pre><code>OBS ──PUT segments──> Worker ──> Durable Object
                        │          (in-memory ring, ${maxSegs} segments)
                        │               │
    viewers <──edge cache┴───────────────┘
    (.m3u8: no-store · .ts: immutable)</code></pre></div>

<p class="small">
  Media never touches R2 or Durable Object storage. A live segment is written once,
  read for a few seconds, then irrelevant — persisting it buys nothing, and keeping
  it in the heap also sidesteps the 128 KB per-value limit on DO storage. Segments
  are served through the Cloudflare edge cache, so a single Durable Object handles
  one origin pull per segment per region rather than one per viewer.
</p>

<div class="note">
  <b>Where the latency goes:</b> one segment to encode (${segDur}s, unavoidable),
  ~0.6s to upload, ${((playlistSize - 1) * segDur).toFixed(1)}s of window depth
  because a player starts at the oldest advertised segment, and 2-3s of the
  player's own buffer. <b>Playlist window</b> is the knob that matters — each
  extra segment costs about ${segDur}s.
  <br><br>
  A player's own latency readout measures only the distance to the live edge
  and excludes encoding and upload, so it reads several seconds lower than what
  a viewer actually sees. Going below ~3.5s needs LL-HLS partial segments,
  which AVPro does not reliably support; sub-second needs WebRTC, which the
  VRChat players cannot consume.
</div>

<div class="note">
  <b>VRChat under Proton renders live streams as black.</b> AVPro delegates
  decoding to Windows Media Foundation, and Proton's translation handles
  video-on-demand but not the continuous playlist re-reading a live stream
  needs. Set <code>PSEUDO_VOD = "1"</code> to make playlists end with
  <code>ENDLIST</code>, which such a player will decode — at the cost of
  playback stopping at the end of the window and not resuming. On Windows,
  leave it off.
</div>

<footer>cf-live · runs within the Cloudflare Workers free tier ·
  <a href="/healthz">health</a></footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
const ORIGIN = location.origin;
const SEG_DUR = ${segDur};
const KEY_STORE = 'cf-live-ingest-key';
let hls = null, statusTimer = null, currentStream = null;

/* ---------- key handling (local only) ---------- */
function getKey() {
  const v = document.getElementById('key').value.trim();
  return v || '<KEY>';
}
function forgetKey() {
  document.getElementById('key').value = '';
  try { localStorage.removeItem(KEY_STORE); } catch {}
  render();
}
function saveKey() {
  const v = document.getElementById('key').value.trim();
  try { v ? localStorage.setItem(KEY_STORE, v) : localStorage.removeItem(KEY_STORE); } catch {}
}

/* ---------- names and urls ---------- */
function streamName() {
  const raw = document.getElementById('stream').value || 'main';
  return raw.trim().replace(/[^A-Za-z0-9._-]/g, '') || 'main';
}
function playUrl(n)  { return ORIGIN + '/' + encodeURIComponent(n); }
function playUrlExt(n) { return ORIGIN + '/' + encodeURIComponent(n) + '.m3u8'; }
function playUrlFull(n) { return ORIGIN + '/live/' + encodeURIComponent(n) + '.m3u8'; }
function ingestUrl(n, k) { return ORIGIN + '/ingest/' + k + '/' + n + '/live.m3u8'; }
function segPattern(n, k) { return ORIGIN + '/ingest/' + k + '/' + n + '/seg%05d.ts'; }

function muxerSettings(n, k) {
  return 'method=PUT http_persistent=1 ignore_io_errors=1 ' +
         'hls_time=' + SEG_DUR + ' hls_list_size=6 ' +
         'hls_flags=delete_segments+omit_endlist hls_segment_type=mpegts ' +
         'hls_segment_filename=' + segPattern(n, k);
}
function videoEncoderSettings() {
  const g = 30 * SEG_DUR;
  return 'preset=veryfast tune=zerolatency profile=main bf=0 ' +
         'g=' + g + ' keyint_min=' + g + ' sc_threshold=0';
}
function ffmpegCommand(n, k) {
  const g = 30 * SEG_DUR;
  return [
    'ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 \\\\',
    '       -f lavfi -i sine=frequency=440 \\\\',
    '  -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -bf 0 \\\\',
    '  -g ' + g + ' -keyint_min ' + g + ' -sc_threshold 0 -b:v 2500k -pix_fmt yuv420p \\\\',
    '  -c:a aac -b:a 128k -ar 48000 -ac 2 \\\\',
    '  -f hls -hls_time ' + SEG_DUR + ' -hls_list_size 6 \\\\',
    '  -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \\\\',
    '  -method PUT -http_persistent 1 -ignore_io_errors 1 \\\\',
    '  -hls_segment_filename "' + segPattern(n, k) + '" \\\\',
    '  "' + ingestUrl(n, k) + '"',
  ].join('\\n');
}
function verifyCommand(n) {
  return 'ffmpeg -i "' + playUrl(n) + '" -t 5 -c copy out.ts\\nffprobe out.ts';
}

/* ---------- render every snippet ---------- */
function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
function render() {
  const n = streamName();
  const k = getKey();

  setText('qs-url', ingestUrl(n, k));
  setText('qs-mux', muxerSettings(n, k));
  setText('qs-play', playUrl(n));

  setText('u-short', playUrl(n));
  setText('u-ext', playUrlExt(n));
  setText('u-full', playUrlFull(n));

  setText('f-url', ingestUrl(n, k));
  setText('f-mux', muxerSettings(n, k));
  setText('f-vset', videoEncoderSettings());
  setText('f-gop', String(30 * SEG_DUR));
  setText('qs-gop', String(30 * SEG_DUR));

  setText('cmd-ffmpeg', ffmpegCommand(n, k));
  setText('cmd-verify', verifyCommand(n));
}

/* ---------- copy buttons ---------- */
async function copyText(text, btn, label, sourceEl) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch {
    // The async clipboard API can be unavailable (insecure context) or simply
    // rejected. execCommand still works from inside a click handler.
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.top = '-1000px';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      ta.remove();
    } catch {}
  }
  if (!ok && sourceEl) {
    // Last resort: select the real text so the manual shortcut actually copies
    // something. Telling the user to press a shortcut against an empty
    // clipboard would be worse than useless.
    try {
      const range = document.createRange();
      range.selectNodeContents(sourceEl);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch {}
  }
  btn.textContent = ok ? 'Copied' : 'Selected — press ⌘C';
  btn.classList.toggle('done', ok);
  setTimeout(() => { btn.textContent = label; btn.classList.remove('done'); }, 1600);
}

function installCopyButtons() {
  // Every code block gets a floating copy button.
  document.querySelectorAll('.snip').forEach((snip) => {
    if (snip.querySelector('.copy')) return;
    const btn = document.createElement('button');
    btn.className = 'copy';
    btn.type = 'button';
    btn.textContent = 'Copy';
    btn.addEventListener('click', () => {
      const code = snip.querySelector('code') || snip.querySelector('pre');
      copyText(code.textContent, btn, 'Copy', code);
    });
    snip.appendChild(btn);
  });

  // Per-row copy buttons in the config tables.
  document.querySelectorAll('.minicopy[data-copy]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const src = document.getElementById(btn.dataset.copy);
      if (src) copyText(src.textContent, btn, 'Copy', src);
    });
  });
}

/* ---------- player ---------- */
function load() {
  const name = streamName();
  currentStream = name;
  render();
  const src = playUrlExt(name);
  const v = document.getElementById('video');

  if (hls) { hls.destroy(); hls = null; }
  if (window.Hls && Hls.isSupported()) {
    // lowLatencyMode stays off on purpose: it makes hls.js wait for EXT-X-PART
    // partial segments, which this plain-HLS playlist never provides.
    hls = new Hls({
      lowLatencyMode: false,
      // Sit one segment behind the live edge rather than three. Costs nothing
      // in requests — the poll rate follows segment duration — and removes a
      // couple of seconds. 1 is the floor: at 0 the player has nothing
      // buffered ahead and stalls on the first jitter.
      liveSyncDurationCount: 1,
      maxBufferLength: 6,
      manifestLoadingMaxRetry: 999,  // the stream may not be live yet
      levelLoadingMaxRetry: 999,
      fragLoadingMaxRetry: 6,
    });
    hls.loadSource(src);
    hls.attachMedia(v);
    hls.on(Hls.Events.ERROR, (_e, d) => {
      if (!d.fatal) return;
      if (d.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
      else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
    });
  } else {
    v.src = src;  // Safari and iOS play HLS natively
  }
  v.play().catch(() => {});
  startStatus(name);
}

/**
 * Seek to the furthest playable position without rebuilding the player.
 *
 * Deliberately does NOT use hls.liveSyncPosition: that is hls.js's own
 * conservative sync point (held back by liveSyncDurationCount), so it is
 * usually where playback already sits and seeking to it does nothing visible.
 * The real edge is the end of what has been buffered.
 */
function jumpLive() {
  const v = document.getElementById('video');
  const btn = document.getElementById('btn-live');

  const ends = [];
  if (v.buffered.length) ends.push(v.buffered.end(v.buffered.length - 1));
  if (v.seekable.length) ends.push(v.seekable.end(v.seekable.length - 1));
  const edge = Math.max(...ends.filter((n) => Number.isFinite(n) && n > 0));

  if (!Number.isFinite(edge)) {
    flashButton(btn, 'No stream', 'Jump to live');
    v.play().catch(() => {});
    return;
  }

  // Land a fraction behind the very end: seeking exactly to the edge often
  // stalls, because the player then has nothing buffered ahead of it.
  const target = Math.max(0, edge - 0.3);
  const gained = target - v.currentTime;

  // Only seek forward, and only when it is worth the interruption.
  if (gained > 0.5) {
    v.currentTime = target;
    flashButton(btn, '-' + gained.toFixed(1) + 's', 'Jump to live');
  } else {
    flashButton(btn, 'Already live', 'Jump to live');
  }
  v.play().catch(() => {});
}

/** Briefly replace a button's label with a result, then restore it. */
function flashButton(btn, text, restore) {
  if (!btn) return;
  btn.textContent = text;
  btn.classList.add('flash');
  setTimeout(() => {
    btn.textContent = restore;
    btn.classList.remove('flash');
  }, 1300);
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
  // Back off while offline and pause entirely when the tab is hidden. A fixed
  // 2s poll was the single largest idle cost: one page left open consumed
  // ~35k requests/day doing nothing, enough for three tabs to exhaust the
  // free tier without anyone watching.
  let idleStreak = 0;
  // null until the first status reply, so the initial load is not treated as a
  // transition and does not trigger an immediate second load.
  let wasLive = null;
  const tick = async () => {
    if (currentStream !== name) return;
    if (document.hidden) {
      // Nobody is looking; check back only when the tab is shown again.
      statusTimer = setTimeout(tick, 5000);
      return;
    }
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
      // 0 is a legitimate latency (right at the edge), so test for a finite
      // number rather than truthiness.
      if (hls && Number.isFinite(hls.latency)) {
        lat = hls.latency.toFixed(1) + 's';
      } else if (v.seekable.length) {
        lat = Math.max(0, v.seekable.end(v.seekable.length - 1) - v.currentTime).toFixed(1) + 's';
      }
      document.getElementById('s-lat').textContent = lat;
      idleStreak = j.live ? 0 : idleStreak + 1;

      // Reload across an offline/live transition. The offline playlist starts
      // at MEDIA-SEQUENCE 0 while a live one resumes at whatever the encoder
      // has reached, and there is no EXT-X-DISCONTINUITY bridging them, so
      // hls.js treats the jump as a playlist reset and stays on the slate's
      // timeline instead of following the stream. Rebuilding is the only
      // reliable way across that seam.
      if (wasLive !== null && wasLive !== j.live) {
        wasLive = j.live;
        load();
        return;
      }
      wasLive = j.live;
    } catch {
      document.getElementById('led').className = 'led';
      document.getElementById('livetext').textContent = 'status unavailable';
      idleStreak++;
    }
    // Live: stay responsive. Offline: ease off towards 30s.
    const delay = idleStreak === 0
      ? 2000
      : Math.min(30000, 2000 * Math.pow(2, Math.min(idleStreak, 4)));
    statusTimer = setTimeout(tick, delay);
  };
  // Resume promptly when the tab comes back, rather than waiting out a backoff.
  if (!window.__visHooked) {
    window.__visHooked = true;
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && statusTimer) {
        clearTimeout(statusTimer);
        idleStreak = 0;
        tick();
      }
    });
  }
  tick();
}

/* ---------- init ---------- */
try {
  const saved = localStorage.getItem(KEY_STORE);
  if (saved) document.getElementById('key').value = saved;
} catch {}
document.getElementById('key').addEventListener('input', () => { saveKey(); render(); });
document.getElementById('stream').addEventListener('input', render);
installCopyButtons();
render();
load();
</script>
</body></html>`;
