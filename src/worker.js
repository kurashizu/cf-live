/**
 * KRSZ Live — low-latency HLS relay on Cloudflare Workers + Durable Objects.
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
import { slateBytes, SLATE_DURATION, SLATE_VERSION } from './slate.js';

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

// MPEG-TS media type for segments. video/mp2t is correct, but some players
// route on it into a demuxer path that handles our segments poorly, so this
// is configurable to allow testing against octet-stream — which is what
// several reference streams that do play actually serve.
// SRS serves exactly this, casing included; matching it removes one
// difference from a stack known to play in VRChat.
const TS = 'video/MP2T';
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
        // init.mp4 and .m4s arrive when the encoder is configured for fMP4,
        // which is what DASH needs and HLS can also use via EXT-X-MAP.

        const ingested = await roomFetch(
          env, stream, request, `/ingest/${encodeURIComponent(file)}`);
        // A live segment just arrived, so any cached OFFLINE playlist for this
        // stream is now wrong. Purge it rather than waiting out its TTL, which
        // otherwise leaves viewers on the slate for seconds after the
        // broadcast has actually started.
        if (ingested.status === 200 && request.method === 'PUT'
            && !file.endsWith('.m3u8')) {
          const purge = caches.default.delete(offlineCacheKey(request, stream));
          if (ctx && typeof ctx.waitUntil === 'function') ctx.waitUntil(purge);
        }
        return ingested;
      }

      // ---- playback --------------------------------------------------------
      // /live/<stream>/index.m3u8 — the media playlist a master points at.
      const media = path.match(/^\/live\/([^/]+)\/index\.m3u8$/);
      if (media) {
        const stream = media[1];
        if (!isSafeName(stream)) return text('bad name', 400);
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          return text('method not allowed', 405);
        }
        return servePlaylist(env, stream, request, ctx);
      }

      // /live/<stream>.m3u8
      const playlist = path.match(/^\/live\/([^/]+)\.m3u8$/);
      if (playlist) {
        const stream = playlist[1];
        if (!isSafeName(stream)) return text('bad name', 400);
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          return text('method not allowed', 405);
        }
        return masterPlaylist(stream, env);
      }

      // The offline slate. Served straight from the Worker with a long cache
      // lifetime — it never changes, so it must never reach a Durable Object.
      // This is what makes an idle viewer free after the first request.
      // /live/_offline.<version>.<sequence>.ts — the sequence only makes the
      // URL unique per playlist slot; every one serves the same bytes.
      const slate = path.match(/^\/live\/_offline(?:\.([0-9a-f]{8}))?(?:\.\d+)?\.ts$/);
      if (slate) {
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          return text('method not allowed', 405);
        }
        const bytes = slateBytes();
        // Only the versioned URL may be cached long-term. An unversioned
        // request is either an old client or a stale playlist, and caching
        // that for a year is what pinned viewers to an outdated slate.
        const versioned = slate[1] === SLATE_VERSION;
        return new Response(request.method === 'HEAD' ? null : bytes, {
          headers: {
            'Content-Type': TS,
            'Content-Length': String(bytes.byteLength),
            'Cache-Control': versioned
              ? 'public, max-age=31536000, immutable'
              : 'public, max-age=60',
            'Access-Control-Allow-Origin': '*',
          },
        });
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
      // Only .m3u8 is stripped; any other extension stays part of the name, so
      // /x.mpd would become a stream literally called "x.mpd". Reject names
      // carrying a different extension rather than inventing a stream for a
      // request that was clearly meant for something else.
      const alias = path.match(/^\/([^/]+?)(?:\.m3u8)?$/);
      if (alias && !RESERVED_PATHS.has(alias[1].toLowerCase())
          && isSafeName(alias[1]) && !/\.[A-Za-z0-9]+$/.test(alias[1])) {
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          return text('method not allowed', 405);
        }
        return masterPlaylist(alias[1], env);
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

/**
 * A master playlist pointing at the stream's single media playlist.
 *
 * Players in the VRChat ecosystem are used to being handed a master: AVPro
 * reads codec and resolution hints from EXT-X-STREAM-INF before it will
 * commit to a rendition, and given a bare media playlist some builds sit in a
 * loading state instead. Declaring one variant costs nothing and makes the
 * stream look like every other HLS source such a player has seen.
 */
function masterPlaylist(stream, env) {
  // When the player is one that stops at ENDLIST, the media playlist URL
  // carries a cache-busting token. Re-reading the master then yields a URL
  // the player has not seen finish, which is the only way to get such a
  // player to continue past the end of a window.
  const bust = env && env.PSEUDO_VOD === '1'
    ? `?t=${Math.floor(Date.now() / 1000)}`
    : '';
  const body = [
    '#EXTM3U',
    '#EXT-X-VERSION:3',
    // Matches SRS, which advertises only a nominal bandwidth and omits
    // CODECS and RESOLUTION entirely. Declaring codecs invites a player to
    // decide up front whether it can decode the stream, and a mismatch
    // between the declaration and the actual bitstream is a plausible cause
    // of a black picture with otherwise healthy playback.
    '#EXT-X-STREAM-INF:BANDWIDTH=1,AVERAGE-BANDWIDTH=1',
    // Root-absolute. The master is reachable at two different depths
    // (/<stream> and /live/<stream>.m3u8), so no single relative path
    // resolves correctly from both — from the latter, "live/x/index.m3u8"
    // becomes /live/live/x/index.m3u8 and 404s.
    `/live/${encodeURIComponent(stream)}/index.m3u8${bust}`,
    '',
  ].join('\n');
  return new Response(body, {
    headers: {
      'Content-Type': M3U8,
      // Not cacheable when it carries a token: the point is that each read
      // produces a different media playlist URL.
      'Cache-Control': bust ? 'no-store' : 'public, max-age=300',
      'Access-Control-Allow-Origin': '*',
    },
  });
}

/**
 * Serve a playlist, caching it at the edge only when the DO says it is safe.
 *
 * A live playlist must never be cached: hls.js treats a byte-identical
 * manifest as "nothing new" and stops loading fragments, which stalls
 * playback. The offline slate playlist is the opposite — it is identical on
 * every request, and caching it is what stops an idle viewer from reaching the
 * Worker at all. Cloudflare will not cache these paths on its own (no static
 * extension), so it has to go through the Cache API explicitly.
 */
async function servePlaylist(env, stream, request, ctx) {
  const cache = caches.default;
  // Cache the offline playlist under a key the ingest path can also compute,
  // so starting a broadcast can purge it. Keyed off the stream name only, not
  // the request URL, because the same playlist is reachable from three paths
  // (/s, /s.m3u8, /live/s.m3u8) and all three must be invalidated together.
  const cacheKey = offlineCacheKey(request, stream);

  const hit = await cache.match(cacheKey);
  if (hit) return hit;

  const res = await roomFetch(env, stream, request, '/playlist');
  // The DO marks a cacheable (offline) playlist with an explicit max-age.
  const cc = res.headers.get('cache-control') || '';
  if (res.status === 200 && /max-age=[1-9]/.test(cc) && !/no-store/.test(cc)) {
    const body = await res.text();
    const headers = new Headers(res.headers);
    const cached = new Response(body, { status: 200, headers });
    if (ctx && typeof ctx.waitUntil === 'function') {
      ctx.waitUntil(cache.put(cacheKey, cached.clone()));
    }
    return cached;
  }
  return res;
}

/**
 * Cache key for a stream's offline playlist.
 *
 * Shared by the playback and ingest paths so that the first segment of a new
 * broadcast can delete the entry. Without that purge, viewers keep being
 * served OFFLINE from the edge for the full cache lifetime even though the
 * Durable Object already has live segments.
 */
function offlineCacheKey(request, stream) {
  const url = new URL(request.url);
  url.pathname = `/__offline/${encodeURIComponent(stream)}`;
  url.search = '';
  return new Request(url.toString(), { method: 'GET' });
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
    this.playlistSize = Math.max(1, intVar(env.PLAYLIST_SIZE, 4));
    // Hard ceiling on how much wall-clock time the playlist may span, so a
    // mis-sized segment cannot inflate startup latency without bound.
    this.maxWindowSeconds = Math.max(
      this.targetDuration,
      intVar(env.MAX_WINDOW_SECONDS, 10),
    );
    // The ring must hold at least one segment more than the playlist
    // advertises, or eviction would drop a segment the playlist still points
    // at and viewers would get a 404 for it. Enforced rather than documented,
    // since a misconfiguration here breaks playback for everyone.
    this.maxSegments = Math.max(
      this.playlistSize + 1,
      intVar(env.MAX_SEGMENTS, 8),
    );
    /** Per-segment durations parsed from OBS's own playlist, keyed by file. */
    this.durations = new Map();

    this.lastIngestAt = 0;
    this.startedAt = 0;
    this.totalSegments = 0;
    this.pendingDiscontinuity = false;
    /** fMP4 initialisation segment, when the encoder sends one. */
    this.initSegment = null;

    /**
     * Restore the metadata that must outlive eviction. Segments deliberately
     * stay in memory, but timing and discontinuity state is only a few bytes,
     * and losing it splices a restarted encoder into the timeline with no
     * discontinuity marker.
     *
     * This must come after the defaults above: blockConcurrencyWhile takes an
     * async callback, so the rest of the constructor runs first and would
     * otherwise overwrite everything restored here.
     */
    this.ready = state.blockConcurrencyWhile(async () => {
      const init = await state.storage.get('init');
      if (init) {
        this.initSegment = { name: init.name, bytes: new Uint8Array(init.bytes) };
      }
      const meta = await state.storage.get('meta');
      if (!meta) return;
      this.lastIngestAt = meta.lastIngestAt ?? 0;
      this.discontinuitySequence = meta.discontinuitySequence ?? 0;
      this.totalSegments = meta.totalSegments ?? 0;
      this.startedAt = meta.startedAt ?? 0;
      // Every segment from before the eviction is gone from memory, so the
      // next ingest necessarily starts a new, discontinuous timeline.
      // Note: a sequenceBase was persisted by an earlier version. It is
      // deliberately not restored — reviving it would reintroduce the huge
      // media sequence numbers it used to produce.
      this.pendingDiscontinuity = this.lastIngestAt > 0;
    });
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
    // Pin the sequence base the first time content arrives, so live numbering
    // continues past whatever the offline playlist had reached rather than
    // restarting at zero. The margin covers the window the offline playlist
    // was advertising when the switch happened.
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

    // The fMP4 initialisation segment is not part of the timeline: it carries
    // the decoder configuration and every media segment depends on it, so it
    // is stored once and never evicted with the ring.
    if (file === 'init.mp4' || file.endsWith('.mp4')) {
      this.initSegment = { name: file, bytes: buf };
      // Persist it, unlike media segments. It is a couple of kilobytes, it
      // never changes during a broadcast, and every media segment is
      // undecodable without it — so losing it to an eviction would break
      // playback until the encoder happened to resend it.
      await this.state.storage.put('init', { name: file, bytes: [...buf] });
      return text('ok');
    }

    this.segments.set(file, { bytes: buf, at: now });
    this.order.push(file);
    this.totalSegments++;

    if (this.pendingDiscontinuity) {
      // The encoder restarted, and its segment counter restarts with it.
      // Keeping the previous timeline's segments would interleave two
      // unrelated numberings in one playlist — seg00039 followed by
      // seg00000 — which makes the media sequence go backwards and is enough
      // on its own to leave a player showing black video. Drop them and
      // start clean from the new timeline.
      this.segments.clear();
      this.order.length = 0;
      this.durations.clear();
      this.discontinuityAt = null;
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
    // Count polls so a test can tell whether a player re-requests the
    // playlist after reaching the end, or fetches it once and stops.
    this.playlistHits = (this.playlistHits || 0) + 1;
    this.lastPlaylistAt = Date.now();
    if (this.order.length === 0) {
      // No live content. Serve the "OFFLINE" slate as a *moving* live playlist
      // rather than a single static entry.
      //
      // A one-segment playlist with a fixed MEDIA-SEQUENCE looks finished to a
      // player: it plays the 2s slate once, sees nothing new on reload, and
      // stops. Advancing the sequence with wall-clock time and advertising a
      // full window makes the slate behave like any other live stream, so it
      // loops indefinitely and — crucially — the player keeps polling, which
      // is what lets it pick up the real broadcast when it starts.
      //
      // The same slate segment is repeated under rotating sequence numbers.
      // It is byte-identical every time, so it stays cheap to serve: one
      // immutable object in the edge cache regardless of how long anyone
      // waits.
      const ttl = intVar(this.env.OFFLINE_CACHE_TTL, 10);
      const count = this.playlistSize;
      // Derived from the clock so every viewer, and every poll, sees the same
      // window advance at the same rate without the DO holding any state.
      const seq = offlineSequence();
      const lines = [
        '#EXTM3U',
        '#EXT-X-VERSION:3',
        `#EXT-X-TARGETDURATION:${Math.ceil(SLATE_DURATION)}`,
        `#EXT-X-MEDIA-SEQUENCE:${seq}`,
      ];
      for (let i = 0; i < count; i++) {
        // Every slot is the same file, so its media restarts at PTS 0 while
        // the playlist advances. Without this marker a player places the
        // fragment at i x duration, finds timestamps that rewind, treats it
        // as already buffered, and stops advancing — the slate freezes after
        // one fragment with no error reported.
        lines.push('#EXT-X-DISCONTINUITY');
        lines.push(`#EXTINF:${SLATE_DURATION.toFixed(3)}, no desc`);
        // Distinct URL per slot: players dedupe by URI, and repeating one
        // would be read as the same segment already played rather than the
        // next one in the timeline.
        // Relative to /live/<stream>/index.m3u8, so ../ lands in /live/.
        lines.push(`../_offline.${SLATE_VERSION}.${seq + i}.ts`);
      }
      lines.push('');
      return new Response(lines.join('\n'), {
        headers: {
          'Content-Type': M3U8,
          // Cache for less than one segment, so the sequence keeps advancing
          // for viewers while still collapsing bursts of polls into one
          // origin hit.
          'Cache-Control': `public, max-age=${Math.min(ttl, Math.floor(SLATE_DURATION))}`,
          'Access-Control-Allow-Origin': '*',
        },
      });
    }

    // Serve a trailing window, bounded by BOTH a segment count and a total
    // duration. The duration bound matters when the encoder's keyframe
    // interval does not match hls_time: segments then come out several times
    // longer than requested, and a fixed count would stretch the window to
    // tens of seconds and dominate the latency budget.
    // 'grow' advertises every buffered segment instead of a trailing window,
    // so entries never disappear from the top of the list. Some players lose
    // the timeline when the playlist they are following is truncated from the
    // front, and this isolates that behaviour.
    // Pseudo-VOD terminates every playlist with ENDLIST, because AVPro in
    // VRChat renders a normal live playlist as black video but plays the same
    // segments when the playlist claims to be finished.
    //
    // It still serves a trailing window rather than the whole buffer: the
    // master playlist hands out a fresh, tokenised media-playlist URL on each
    // read, so a player that stops at ENDLIST and comes back gets the current
    // live edge. Advertising the whole buffer would instead pin it to the
    // oldest segment and let latency grow without bound.
    const pseudoVod = this.env.PSEUDO_VOD === '1';
    const window = trailingWindow(
      this.order,
      this.playlistSize,
      this.durations,
      this.targetDuration,
      this.maxWindowSeconds,
    );
    const startIndex = this.order.length - window.length;
    // EXT-X-MEDIA-SEQUENCE must be monotonic across the whole life of the URL,
    // including the offline->live transition. The offline playlist numbers
    // itself from wall-clock time (a large number), so restarting from
    // ffmpeg's seg%05d counter would make the sequence collapse from hundreds
    // of millions to zero. Players read that as a different stream and stop
    // following it, which is why a viewer had to force-reload to see a
    // broadcast start.
    //
    // Offset the encoder's counter past the offline numbering, pinned at the
    // moment ingest began so it stays stable for the rest of the broadcast.
    // Straight from the encoder's own seg%05d counter. Continuing from the
    // offline playlist's clock-derived numbering was tried and reverted: it
    // opened live playlists at sequences in the hundreds of thousands, which
    // AVPro renders as black video.
    const seq = segmentIndex(window[0]) ?? (this.mediaSequence + startIndex);

    // No EXT-X-START. It should pull the entry point toward the live edge
    // for free, but at 1s segments hls.js never settles on a start position:
    // measured with the tag present, startPosition walked 2 -> 6 -> 10 on
    // successive refreshes and no fragment ever loaded. The window rolls once
    // per second, faster than the player recomputes, so the target keeps
    // moving. Latency is instead controlled by PLAYLIST_SIZE and the client's
    // own liveSyncDurationCount.
    // Header order follows SRS: VERSION, MEDIA-SEQUENCE, TARGETDURATION.
    // EXT-X-MAP requires version 7; plain TS segments only need 3.
    const lines = [
      '#EXTM3U',
      `#EXT-X-VERSION:${this.initSegment ? 7 : 3}`,
      `#EXT-X-MEDIA-SEQUENCE:${seq}`,
      `#EXT-X-TARGETDURATION:${this.targetDuration}`,
    ];
    if (pseudoVod) lines.push('#EXT-X-PLAYLIST-TYPE:VOD');
    // EXT-X-DISCONTINUITY-SEQUENCE is deliberately not emitted. A restart
    // clears the ring, so the window always holds one continuous timeline and
    // never contains an EXT-X-DISCONTINUITY to count. Publishing a non-zero
    // count with no matching marker made hls.js compute a timeline offset of
    // -91s: segments were fetched and appended, but at a negative position,
    // so nothing ever played. The counter is still tracked for /status.

    // fMP4 segments are undecodable without the initialisation segment, so
    // it has to be declared before the first media segment.
    if (this.initSegment) {
      lines.push(`#EXT-X-MAP:URI="${this.initSegment.name}"`);
    }

    window.forEach((file, i) => {
      // Only mark a discontinuity between segments, never before the first
      // one in the window: at that position it tells a player the stream
      // breaks at the very point it is about to start decoding, and SRS only
      // emits the tag when an actual codec change occurred.
      if (file === this.discontinuityAt && i > 0) {
        lines.push('#EXT-X-DISCONTINUITY');
      }
      const dur = this.durations.get(file) ?? this.targetDuration;
      // Three decimals and the ", no desc" title are SRS's exact output.
      // SRS declined to change the title (ossrs/srs#2343), so every player
      // that works against SRS has been exercised against this form.
      lines.push(`#EXTINF:${dur.toFixed(3)}, no desc`);
      // Root-absolute on purpose: this playlist is served from three paths
      // (/live/<s>.m3u8, /<s>.m3u8 and /<s>), and a relative URI would
      // resolve differently under each. An absolute path is correct for all.
      // Relative URI. The media playlist is only served from
      // /live/<stream>/index.m3u8, so a bare filename resolves to
      // /live/<stream>/<file>. Root-absolute paths are what every working
      // reference stream avoids, and AVPro would not play a playlist using
      // them.
      lines.push(file);
    });
    if (pseudoVod) lines.push('#EXT-X-ENDLIST');
    lines.push('');

    return new Response(lines.join('\n'), {
      headers: {
        'Content-Type': M3U8,
        // Plain no-store. The previous value combined no-cache, max-age=0 and
        // must-revalidate, and Cloudflare stripped the header entirely on the
        // way out, leaving live playlists with no caching directive at all —
        // so a player was free to reuse its first copy and never see the
        // stream advance.
        'Cache-Control': 'no-store',
        'Access-Control-Allow-Origin': '*',
      },
    });
  }

  handleSegmentRead(file) {
    if (this.initSegment && file === this.initSegment.name) {
      return new Response(this.initSegment.bytes, {
        headers: {
          'Content-Type': 'video/mp4',
          'Content-Length': String(this.initSegment.bytes.byteLength),
          'Cache-Control': 'public, max-age=60',
          'Access-Control-Allow-Origin': '*',
        },
      });
    }
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
      playlistHits: this.playlistHits || 0,
      secondsSinceLastPoll: this.lastPlaylistAt ? (Date.now() - this.lastPlaylistAt) / 1000 : -1,
      maxWindowSeconds: this.maxWindowSeconds,
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

/**
 * Take the newest segments, stopping at whichever limit is hit first: the
 * segment count, or the total duration. Always returns at least one segment,
 * since an empty window would be an invalid live playlist.
 */
function trailingWindow(order, maxCount, durations, fallback, maxSeconds) {
  const picked = [];
  let total = 0;
  for (let i = order.length - 1; i >= 0; i--) {
    const file = order[i];
    const dur = durations.get(file) ?? fallback;
    if (picked.length >= maxCount) break;
    if (picked.length > 0 && total + dur > maxSeconds) break;
    picked.unshift(file);
    total += dur;
  }
  return picked;
}

/** Total duration of a playlist window, for computing a live-edge offset. */
function windowDuration(files, durations, fallback) {
  return files.reduce((n, f) => n + (durations.get(f) ?? fallback), 0);
}

/**
 * The offline playlist's media sequence: wall-clock time in slate-length
 * units. Shared with the live path so live numbering can continue past it
 * instead of resetting, which would break the transition for players.
 */
/**
 * Media sequence for the offline playlist.
 *
 * It has to advance with time so the playlist looks live and players keep
 * polling, but it must also stay small: values in the hundreds of millions
 * make some players — AVPro among them — stall rather than play. Wrapping at
 * a large-but-safe modulus keeps the number under a million while still
 * advancing once per slate length.
 *
 * The wrap is harmless in practice: it happens once every ~23 days of
 * continuous offline time, and a player that sees the rollover simply treats
 * it as a new stream, which is the correct outcome for a feed nobody is
 * watching.
 */
function offlineSequence() {
  // Wrapped small on purpose. It still has to advance once per slate length
  // so the playlist reads as live, but AVPro renders a stream whose media
  // sequence is in the hundreds of thousands as black video, so the modulus
  // is kept to four digits.
  return Math.floor(Date.now() / 1000 / SLATE_DURATION) % 10_000;
}

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

/** Parse a fractional env var, falling back when unset or unparseable. */
function numVar(v, fallback) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : fallback;
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
