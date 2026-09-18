/**
 * cf-live — low-latency HLS relay on Cloudflare Workers + Durable Objects.
 *
 * Ingest:   OBS (custom FFmpeg output, hls muxer, method=PUT)
 *             PUT /ingest/:key/:stream/live.m3u8
 *             PUT /ingest/:key/:stream/seg00042.ts
 *             DELETE ...            (hls_flags=delete_segments)
 *
 * Playback: VRChat AVPro / any HLS player
 *             GET  /live/:stream.m3u8
 *             GET  /live/:stream/seg00042.ts
 *
 * Media never touches R2 or DO storage — segments live in the DO's JS heap as a
 * ring buffer, which is all a live stream needs and avoids the 128 KB
 * per-value limit on DO storage.
 */

import { landingPage } from './page.js';

/**
 * Paths that can never be a stream name, because the short alias route
 * (/<stream>) would otherwise shadow a real endpoint or a browser's
 * well-known request.
 */
const RESERVED_PATHS = new Set([
  'healthz', 'status', 'live', 'ingest', 'index.html',
  'favicon.ico', 'robots.txt', 'sitemap.xml', 'apple-touch-icon.png',
  '.well-known',
]);

const TS = 'video/mp2t';
const M3U8 = 'application/vnd.apple.mpegurl';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    try {
      if (path === '/' || path === '/index.html') return landingPage(url, env);
      if (path === '/healthz') return text('ok');

      // ---- ingest ----------------------------------------------------------
      // /ingest/<key>/<stream>/<file>
      const ingest = path.match(/^\/ingest\/([^/]+)\/([^/]+)\/([^/]+)$/);
      if (ingest) {
        const [, key, stream, file] = ingest;
        if (!env.INGEST_KEY) {
          return text('INGEST_KEY secret is not configured on this Worker', 500);
        }
        // Constant-time-ish compare; keys are short and this is not a
        // high-value oracle, but avoid the trivial early-exit.
        if (!safeEqual(key, env.INGEST_KEY)) return text('forbidden', 403);
        if (!isSafeName(stream) || !isSafeName(file)) return text('bad name', 400);

        return roomFetch(env, stream, request, `/ingest/${encodeURIComponent(file)}`);
      }

      // ---- playback --------------------------------------------------------
      // /live/<stream>.m3u8
      const playlist = path.match(/^\/live\/([^/]+)\.m3u8$/);
      if (playlist) {
        const stream = playlist[1];
        if (!isSafeName(stream)) return text('bad name', 400);
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          return text('method not allowed', 405);
        }
        return roomFetch(env, stream, request, '/playlist');
      }

      // /live/<stream>/<file>.ts
      const segment = path.match(/^\/live\/([^/]+)\/([^/]+)$/);
      if (segment) {
        const [, stream, file] = segment;
        if (!isSafeName(stream) || !isSafeName(file)) return text('bad name', 400);
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          return text('method not allowed', 405);
        }
        return serveSegment(env, stream, file, request, ctx);
      }

      // ---- status ----------------------------------------------------------
      const status = path.match(/^\/status\/([^/]+)$/);
      if (status) {
        const stream = status[1];
        if (!isSafeName(stream)) return text('bad name', 400);
        return roomFetch(env, stream, request, '/status');
      }

      // ---- short aliases ---------------------------------------------------
      // /<stream> and /<stream>.m3u8 both serve the playlist, so a VRChat URL
      // can be as short as https://host/main. Checked last so real endpoints
      // always win, and served inline rather than redirected because some
      // players (AVPro among them) will not follow a 302 for a manifest.
      const alias = path.match(/^\/([^/]+?)(?:\.m3u8)?$/);
      if (alias && !RESERVED_PATHS.has(alias[1].toLowerCase()) && isSafeName(alias[1])) {
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          return text('method not allowed', 405);
        }
        return roomFetch(env, alias[1], request, '/playlist');
      }

      return text('not found', 404);
    } catch (err) {
      return text(`worker error: ${err.message}`, 500);
    }
  },
};

/**
 * Serve a segment through the Cloudflare edge cache.
 *
 * Segments are immutable once written, so a cache hit is always correct and
 * means viewers in a region only pull from the (single-colo) DO once. Without
 * this, every viewer worldwide would hit the DO for every segment.
 */
async function serveSegment(env, stream, file, request, ctx) {
  const cache = caches.default;
  const cacheKey = new Request(new URL(request.url).toString(), { method: 'GET' });

  const hit = await cache.match(cacheKey);
  if (hit) return hit;

  const res = await roomFetch(env, stream, request, `/segment/${encodeURIComponent(file)}`);
  if (res.status !== 200) return res;

  // Buffer the segment so the same bytes can go to both the cache and the
  // viewer. Streaming one body to both ends up reading a consumed stream after
  // the response is sent, which the runtime rejects.
  const bytes = await res.arrayBuffer();
  const ttl = intVar(env.SEGMENT_CACHE_TTL, 30);
  const headers = {
    'Content-Type': TS,
    'Content-Length': String(bytes.byteLength),
    // Segments never change once written, so this is always safe and means a
    // region only pulls from the single-colo DO once per segment.
    'Cache-Control': `public, max-age=${ttl}, immutable`,
    'Access-Control-Allow-Origin': '*',
  };
  const cacheWrite = cache.put(cacheKey, new Response(bytes, { headers }));
  if (ctx && typeof ctx.waitUntil === 'function') ctx.waitUntil(cacheWrite);
  return new Response(bytes, { headers });
}

/** Route a request to the DO that owns this stream. */
function roomFetch(env, stream, request, innerPath) {
  const id = env.LIVE_ROOM.idFromName(stream);
  const room = env.LIVE_ROOM.get(id);
  // Build a clean request rather than forwarding the client's. Passing the
  // original headers through drags along Range/If-* and content negotiation
  // that don't apply to the internal hop, and forwarding request.body for a
  // GET makes the runtime throw "Can't read from request stream after response
  // has been sent", which corrupts the playlist response.
  const headers = new Headers();
  // The DO needs its own stream name to emit correctly-resolving segment URLs.
  headers.set('x-cf-live-stream', stream);
  const hasBody = !bodylessMethod(request.method);
  if (hasBody) {
    const ct = request.headers.get('content-type');
    if (ct) headers.set('content-type', ct);
  }
  const inner = new Request(`https://room.internal${innerPath}`, {
    method: request.method,
    headers,
    body: hasBody ? request.body : undefined,
  });
  return room.fetch(inner);
}

// =============================================================================
// Durable Object: one per stream name. Holds the segment ring buffer.
// =============================================================================

export class LiveRoom {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    /**
     * Restore the small amount of metadata that must outlive eviction.
     * Segments deliberately stay in memory, but timing/discontinuity state is
     * only a few bytes and losing it splices a restarted encoder into the
     * timeline with no discontinuity marker.
     */
    this.ready = state.blockConcurrencyWhile(async () => {
      const meta = await state.storage.get('meta');
      if (meta) {
        this.lastIngestAt = meta.lastIngestAt ?? 0;
        this.discontinuitySequence = meta.discontinuitySequence ?? 0;
        this.totalSegments = meta.totalSegments ?? 0;
        this.startedAt = meta.startedAt ?? 0;
        // Any segment referenced before eviction is gone from memory, so the
        // next ingest necessarily begins a new, discontinuous timeline.
        this.pendingDiscontinuity = this.lastIngestAt > 0;
      }
    });

    /** @type {Map<string, {bytes: Uint8Array, at: number}>} filename -> segment */
    this.segments = new Map();
    /** Segment filenames in ingest order — the playlist window source. */
    this.order = [];
    /** Media sequence number of order[0]; grows as segments are evicted. */
    this.mediaSequence = 0;
    /** Discontinuity counter, bumped when the encoder restarts. */
    this.discontinuitySequence = 0;
    /** Target duration in seconds, from config. */
    this.targetDuration = intVar(env.SEGMENT_DURATION, 1);
    this.maxSegments = Math.max(2, intVar(env.MAX_SEGMENTS, 8));
    this.playlistSize = Math.max(1, intVar(env.PLAYLIST_SIZE, 4));
    /** Per-segment durations parsed from OBS's own playlist, keyed by file. */
    this.durations = new Map();

    this.lastIngestAt = 0;
    this.startedAt = 0;
    this.totalSegments = 0;
    this.pendingDiscontinuity = false;
  }

  async fetch(request) {
    await this.ready;
    const url = new URL(request.url);
    const path = url.pathname;

    if (path.startsWith('/ingest/')) {
      const file = decodeURIComponent(path.slice('/ingest/'.length));
      return this.handleIngest(request, file);
    }
    if (path === '/playlist') {
      return this.handlePlaylist(request.headers.get('x-cf-live-stream') || 'main');
    }
    if (path.startsWith('/segment/')) {
      const file = decodeURIComponent(path.slice('/segment/'.length));
      return this.handleSegmentRead(file);
    }
    if (path === '/status') return this.handleStatus();
    return text('not found', 404);
  }

  // --- ingest ---------------------------------------------------------------

  async handleIngest(request, file) {
    if (request.method === 'DELETE') {
      // ffmpeg's delete_segments flag. We evict on our own schedule, so just
      // acknowledge — deleting here would race with viewers still reading it.
      await drain(request);
      return text('ok');
    }
    if (request.method !== 'PUT' && request.method !== 'POST') {
      return text('method not allowed', 405);
    }

    const now = Date.now();
    // A long gap means the encoder stopped and restarted: the timeline is
    // discontinuous and players must be told, or they will stall or show
    // corrupt frames across the splice.
    if (this.lastIngestAt && now - this.lastIngestAt > 10_000) {
      this.pendingDiscontinuity = true;
    }
    if (!this.startedAt) this.startedAt = now;
    this.lastIngestAt = now;

    if (file.endsWith('.m3u8')) {
      // OBS/ffmpeg uploads its own playlist. We do not serve it (its URLs and
      // window are wrong for our routing), but it carries the authoritative
      // per-segment EXTINF durations, so parse those out.
      const body = await request.text();
      this.absorbUpstreamPlaylist(body);
      return text('ok');
    }

    const buf = new Uint8Array(await request.arrayBuffer());
    if (buf.byteLength === 0) return text('empty segment', 400);

    this.segments.set(file, { bytes: buf, at: now });
    this.order.push(file);
    this.totalSegments++;

    if (this.pendingDiscontinuity) {
      this.discontinuityAt = file;
      this.pendingDiscontinuity = false;
      this.discontinuitySequence++;
    }

    // Evict oldest beyond the ring. mediaSequence must advance in lockstep so
    // the playlist's EXT-X-MEDIA-SEQUENCE stays truthful.
    while (this.order.length > this.maxSegments) {
      const dropped = this.order.shift();
      this.segments.delete(dropped);
      this.durations.delete(dropped);
      if (this.discontinuityAt === dropped) this.discontinuityAt = null;
      this.mediaSequence++;
    }

    // Small enough to write every segment, and keeps restart detection honest
    // across an eviction.
    await this.state.storage.put('meta', {
      lastIngestAt: this.lastIngestAt,
      discontinuitySequence: this.discontinuitySequence,
      totalSegments: this.totalSegments,
      startedAt: this.startedAt,
    });

    return text('ok');
  }

  /** Pull EXTINF durations out of the playlist ffmpeg PUTs to us. */
  absorbUpstreamPlaylist(body) {
    const lines = body.split('\n');
    let pending = null;
    for (const raw of lines) {
      const line = raw.trim();
      if (line.startsWith('#EXTINF:')) {
        const n = parseFloat(line.slice('#EXTINF:'.length));
        if (Number.isFinite(n)) pending = n;
      } else if (line && !line.startsWith('#')) {
        if (pending !== null) {
          // ffmpeg writes absolute URLs here; we only want the filename.
          const name = line.split('/').pop().split('?')[0];
          this.durations.set(name, pending);
          if (pending > this.targetDuration) {
            this.targetDuration = Math.ceil(pending);
          }
          pending = null;
        }
      } else if (line.startsWith('#EXT-X-TARGETDURATION:')) {
        const n = parseInt(line.slice('#EXT-X-TARGETDURATION:'.length), 10);
        if (Number.isFinite(n) && n > this.targetDuration) this.targetDuration = n;
      }
    }
  }

  // --- playback -------------------------------------------------------------

  handlePlaylist(stream) {
    if (this.order.length === 0) {
      // Nothing ingested yet. A 404 makes AVPro give up permanently, so serve
      // a valid empty live playlist and let it keep polling.
      const body = [
        '#EXTM3U',
        '#EXT-X-VERSION:3',
        `#EXT-X-TARGETDURATION:${this.targetDuration}`,
        '#EXT-X-MEDIA-SEQUENCE:0',
        '',
      ].join('\n');
      return new Response(body, {
        headers: {
          'Content-Type': M3U8,
          'Cache-Control': 'no-store',
          'Access-Control-Allow-Origin': '*',
        },
      });
    }

    // Serve a trailing window of the buffer.
    const window = this.order.slice(-this.playlistSize);
    const startIndex = this.order.length - window.length;
    // EXT-X-MEDIA-SEQUENCE must identify the first segment in the window on the
    // same numbering the segments themselves use. ffmpeg's seg%05d counter is
    // authoritative; our own eviction count drifts away from it whenever the
    // encoder restarts, and a mismatch makes players miscompute the live edge
    // and refuse to load fragments.
    const seq = segmentIndex(window[0]) ?? (this.mediaSequence + startIndex);

    const lines = [
      '#EXTM3U',
      '#EXT-X-VERSION:3',
      `#EXT-X-TARGETDURATION:${this.targetDuration}`,
      `#EXT-X-MEDIA-SEQUENCE:${seq}`,
    ];
    if (this.discontinuitySequence > 0) {
      lines.push(`#EXT-X-DISCONTINUITY-SEQUENCE:${this.discontinuitySequence}`);
    }

    for (const file of window) {
      if (file === this.discontinuityAt) lines.push('#EXT-X-DISCONTINUITY');
      const dur = this.durations.get(file) ?? this.targetDuration;
      lines.push(`#EXTINF:${dur.toFixed(6)},`);
      // Root-absolute on purpose: this playlist is served from three paths
      // (/live/<s>.m3u8, /<s>.m3u8 and /<s>), and a relative URI would
      // resolve differently under each. An absolute path is correct for all.
      lines.push(`/live/${encodeURIComponent(stream)}/${file}`);
    }
    lines.push('');

    return new Response(lines.join('\n'), {
      headers: {
        'Content-Type': M3U8,
        // The playlist MUST NOT be cached by the browser. hls.js treats a
        // byte-identical live playlist as "nothing new" and declines to load
        // any fragment, so a cached playlist stalls playback permanently.
        // Edge collapsing is handled by the immutable segment cache instead.
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0',
        'Access-Control-Allow-Origin': '*',
      },
    });
  }

  handleSegmentRead(file) {
    const seg = this.segments.get(file);
    if (!seg) return text('segment not found', 404);
    return new Response(seg.bytes, {
      headers: {
        'Content-Type': TS,
        'Content-Length': String(seg.bytes.byteLength),
        'Access-Control-Allow-Origin': '*',
      },
    });
  }

  handleStatus() {
    const now = Date.now();
    const sinceIngest = this.lastIngestAt ? now - this.lastIngestAt : Infinity;
    // "live" must mean actually playable: recent ingest AND segments in memory.
    // After an eviction the metadata survives but the ring buffer does not, so
    // a recent timestamp alone would report a stream that serves nothing.
    const staleAfter = Math.max(5000, this.targetDuration * 1000 * 5);
    const live = sinceIngest < staleAfter && this.order.length > 0;
    return Response.json({
      live,
      segmentsBuffered: this.order.length,
      mediaSequence: this.mediaSequence,
      discontinuitySequence: this.discontinuitySequence,
      totalSegments: this.totalSegments,
      targetDuration: this.targetDuration,
      playlistSize: this.playlistSize,
      maxSegments: this.maxSegments,
      bufferedBytes: [...this.segments.values()].reduce((n, s) => n + s.bytes.byteLength, 0),
      // Always a number so consumers can compare without null-checking; -1
      // means nothing has ever been ingested for this stream.
      secondsSinceLastIngest: this.lastIngestAt ? sinceIngest / 1000 : -1,
      uptimeSeconds: this.startedAt ? (now - this.startedAt) / 1000 : 0,
    }, { headers: { 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*' } });
  }
}

// =============================================================================
// helpers
// =============================================================================

/** Extract ffmpeg's numeric counter from a segment filename, e.g. seg00042.ts -> 42. */
function segmentIndex(file) {
  if (!file) return null;
  const m = file.match(/(\d+)(?=\.[^.]+$)/);
  return m ? parseInt(m[1], 10) : null;
}

/** Consume and discard a request body so no stream is left unread. */
async function drain(request) {
  try {
    if (request.body) await request.arrayBuffer();
  } catch {
    // Nothing to read, or already consumed — nothing to do.
  }
}

function isSafeName(s) {
  return /^[A-Za-z0-9._-]{1,128}$/.test(s) && !s.includes('..');
}

function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Methods whose body we never forward to the Durable Object.
 *
 * DELETE belongs here: ffmpeg's delete_segments sends one per segment, the DO
 * answers without reading it, and forwarding the stream anyway leaves it
 * dangling — which the runtime reports as "Can't read from request stream
 * after response has been sent" on every single segment.
 */
function bodylessMethod(m) {
  return m === 'GET' || m === 'HEAD' || m === 'DELETE';
}

function intVar(v, fallback) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : fallback;
}

function text(body, status = 200) {
  return new Response(body + '\n', {
    status,
    headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}
