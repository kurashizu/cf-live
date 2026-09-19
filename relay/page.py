"""The relay's own landing page.

Deliberately not a mirror of the origin's. That page documents an ingest
endpoint and OBS settings, and neither exists here: this host receives a
stream from Sydney over UDP and serves it locally. Advertising an upload
URL that would 404 is worse than saying nothing.

What a viewer on this side needs is the playback URL, whether the stream is
up, and enough of the picture to know why it might not be.
"""

PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KRSZ Live</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem 1.25rem 4rem;
    font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI",
          "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #0e0f11; color: #e6e6e6;
  }
  .wrap { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 1.75rem; margin: 0 0 .25rem; letter-spacing: -.02em; }
  h1 .dot { color: #4ade80; }
  .tagline { color: #9aa0a6; margin: 0 0 2rem; }
  h2 { font-size: 1.05rem; margin: 2.25rem 0 .75rem; color: #cfd3d8; }
  video { width: 100%; background: #000; border-radius: 10px; display: block; }
  .bar { display: flex; gap: .6rem; align-items: center; margin: .9rem 0 0; flex-wrap: wrap; }
  button {
    font: inherit; padding: .45rem .9rem; border-radius: 7px;
    border: 1px solid #2c2f34; background: #17191c; color: #e6e6e6; cursor: pointer;
  }
  button:hover { background: #1f2226; }
  .led { width: .6rem; height: .6rem; border-radius: 50%; background: #555; display: inline-block; }
  .led.on { background: #4ade80; box-shadow: 0 0 8px #4ade80; }
  .muted { color: #9aa0a6; font-size: .9rem; }
  code, pre {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .86rem;
  }
  .url {
    display: flex; gap: .5rem; align-items: center; margin: .6rem 0;
    background: #15171a; border: 1px solid #24272c; border-radius: 8px; padding: .6rem .8rem;
  }
  .url code { flex: 1; word-break: break-all; }
  table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
  td, th { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #24272c; }
  th { color: #9aa0a6; font-weight: 500; font-size: .85rem; }
  footer { margin-top: 3rem; color: #6b7178; font-size: .85rem; }
  a { color: #7dd3fc; }
</style>
</head><body><div class="wrap">

<h1>KRSZ Live<span class="dot">.</span></h1>
<p class="tagline">国内中继节点 — 直播内容由境外源站经 UDP 前向纠错传输至此,本地分发。</p>

<h2>播放</h2>
<video id="video" controls playsinline muted></video>
<div class="bar">
  <button onclick="load()">播放</button>
  <button onclick="jump()">跳到最新</button>
  <span class="led" id="led"></span>
  <span class="muted" id="state">检测中…</span>
  <span class="muted" id="lat"></span>
</div>

<h2>播放地址</h2>
<div class="url"><code id="u1"></code><button onclick="cp('u1',this)">复制</button></div>
<p class="muted">可直接填入 VRChat 的视频播放器,或任何支持 HLS 的播放器。</p>

<h2>状态</h2>
<table>
  <tr><th>缓冲分片</th><td id="s-seg">–</td></tr>
  <tr><th>缓冲字节</th><td id="s-bytes">–</td></tr>
  <tr><th>距上次收包</th><td id="s-pkt">–</td></tr>
  <tr><th>运行时长</th><td id="s-up">–</td></tr>
</table>

<h2>说明</h2>
<p class="muted">
  本节点不接受推流。源站在境外,跨境链路约 20% 丢包,因此传输采用
  RS(40,80) 前向纠错而非重传 —— 重传一次往返约 280 毫秒,等确认回来分片
  已经过期。冗余数据无条件发送,接收端凑齐任意一半分片即可还原。
</p>
<p class="muted">
  无人直播时播放器会显示 OFFLINE 画面并持续轮询,开播后自动接上,无需刷新。
</p>

<footer>KRSZ Live · <a href="/stats">状态数据</a> · <a href="/healthz">健康检查</a></footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
const SRC = location.origin + '/main';
document.getElementById('u1').textContent = SRC;
let hls = null;

function cp(id, btn) {
  navigator.clipboard.writeText(document.getElementById(id).textContent)
    .then(() => { const t = btn.textContent; btn.textContent = '已复制';
                  setTimeout(() => btn.textContent = t, 1200); });
}

function load() {
  const v = document.getElementById('video');
  if (hls) { hls.destroy(); hls = null; }
  if (window.Hls && Hls.isSupported()) {
    // lowLatencyMode stays off: it waits for EXT-X-PART segments, which a
    // plain HLS playlist never provides.
    hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 1,
                    maxBufferLength: 10, manifestLoadingMaxRetry: 999,
                    levelLoadingMaxRetry: 999, fragLoadingMaxRetry: 6 });
    hls.loadSource(SRC);
    hls.attachMedia(v);
    hls.on(Hls.Events.ERROR, (_e, d) => {
      if (!d.fatal) return;
      if (d.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
      else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
    });
  } else {
    v.src = SRC;   // Safari plays HLS natively
  }
  v.play().catch(() => {});
}

function jump() {
  const v = document.getElementById('video');
  // Prefer hls.js's own idea of the edge, but only ever seek forward: its
  // liveSyncPosition can legitimately be 0, and seeking backwards would
  // undo the point of the button.
  const cands = [hls && hls.liveSyncPosition,
                 v.seekable.length ? v.seekable.end(v.seekable.length - 1) : null]
                .filter(x => Number.isFinite(x) && x > v.currentTime);
  if (cands.length) v.currentTime = Math.max.apply(null, cands);
  v.play().catch(() => {});
}

async function tick() {
  try {
    const j = await fetch('/stats', { cache: 'no-store' }).then(r => r.json());
    const fresh = j.secondsSinceLastPacket >= 0 && j.secondsSinceLastPacket < 10;
    document.getElementById('led').className = 'led' + (fresh ? ' on' : '');
    document.getElementById('state').textContent = fresh ? '接收中' : '等待源站';
    document.getElementById('s-seg').textContent = j.segmentsBuffered;
    document.getElementById('s-bytes').textContent =
      (j.counters && j.counters.segments_ok ? j.counters.segments_ok + ' 个已恢复' : '–');
    document.getElementById('s-pkt').textContent =
      j.secondsSinceLastPacket < 0 ? '尚未收到' : j.secondsSinceLastPacket.toFixed(1) + ' 秒';
    document.getElementById('s-up').textContent = (j.uptimeSeconds / 60).toFixed(1) + ' 分钟';
    const v = document.getElementById('video');
    if (hls && Number.isFinite(hls.latency)) {
      document.getElementById('lat').textContent = '延迟 ' + hls.latency.toFixed(1) + 's';
    } else if (v.seekable.length) {
      document.getElementById('lat').textContent =
        '延迟 ' + Math.max(0, v.seekable.end(v.seekable.length - 1) - v.currentTime).toFixed(1) + 's';
    }
  } catch {
    document.getElementById('led').className = 'led';
    document.getElementById('state').textContent = '状态不可用';
  }
  setTimeout(tick, 2000);
}
tick();
load();
</script>
</body></html>
"""
