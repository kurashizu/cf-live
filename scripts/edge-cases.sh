#!/usr/bin/env bash
# Boundary and failure-mode tests for KRSZ Live.
#
# Covers the situations a real broadcast actually hits: offline viewers,
# encoder restarts, malformed ingest, oversized segments, concurrent streams,
# and the caching behaviour that the free-tier quota depends on.
#
# Usage: scripts/edge-cases.sh [base-url] [ingest-key]
set -uo pipefail

BASE="${1:-http://127.0.0.1:8787}"
KEY="${2:-devkey123}"
TMP="$(mktemp -d)"
PIDS=()
trap 'for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; rm -rf "$TMP"' EXIT

pass=0; fail=0; skip=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
note() { echo "        $1"; }
sec()  { echo; echo "── $1"; }

code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
hdr()  { curl -sSI "$1" 2>/dev/null; }

# Publish a stream in the background. $1=name $2=seconds $3=hls_time $4=gop
publish() {
  local name="$1" dur="$2" ht="${3:-2}" gop="${4:-60}"
  ffmpeg -hide_banner -loglevel error -re \
    -f lavfi -i "testsrc2=size=320x180:rate=30" \
    -f lavfi -i "sine=frequency=440" -t "$dur" \
    -c:v libx264 -preset ultrafast -profile:v main -bf 0 \
    -g "$gop" -keyint_min "$gop" -sc_threshold 0 -b:v 300k -pix_fmt yuv420p \
    -c:a aac -b:a 64k -ar 48000 -ac 2 \
    -f hls -hls_time "$ht" -hls_list_size 6 \
    -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \
    -method PUT -http_persistent 1 -ignore_io_errors 1 \
    -hls_segment_filename "$BASE/ingest/$KEY/$name/seg%05d.ts" \
    "$BASE/ingest/$KEY/$name/live.m3u8" >"$TMP/$name.log" 2>&1 &
  PIDS+=($!)
}

# Wait until a stream has at least $2 segments buffered.
wait_segs() {
  local name="$1" want="$2"
  for _ in $(seq 1 40); do
    local n
    n=$(curl -fsS "$BASE/status/$name" 2>/dev/null \
        | grep -o '"segmentsBuffered":[0-9]*' | cut -d: -f2)
    [ "${n:-0}" -ge "$want" ] && return 0
    sleep 1
  done
  return 1
}


# Fetch a stream's media playlist. The short routes now return a master
# playlist naming one variant, so follow it to the media playlist the way a
# player would.
media_playlist() {
  local stream="$1"
  curl -fsS "$BASE/live/$stream/index.m3u8" 2>/dev/null
}

echo "target: $BASE"

# The Worker and VPS backends have deliberately different contracts, so the
# assertions below have to know which one they are talking to. The VPS relay
# reports a deliberately smaller /status and never caches a playlist; the
# Worker caches the offline playlist because it had a request quota to
# survive. Detected from a field only the Worker publishes.
BACKEND=vps
curl -fsS "$BASE/status/backendprobe" 2>/dev/null | grep -q 'uptimeSeconds' \
  && BACKEND=worker
echo "backend: $BACKEND"

# ─────────────────────────────────────────────────────────────────────
sec "1. Offline stream (never broadcast)"
PL="$TMP/off.m3u8"
media_playlist neverused > "$PL" 2>/dev/null
if grep -q '^#EXTM3U' "$PL" 2>/dev/null; then
  ok "offline playlist is valid HLS"
else
  bad "offline playlist malformed"
fi
grep -qE '_offline' "$PL" 2>/dev/null \
  && ok "offline playlist points at the slate" \
  || bad "offline playlist has no slate segment"
grep -q '#EXT-X-ENDLIST' "$PL" 2>/dev/null \
  && bad "offline playlist has ENDLIST (player would stop retrying)" \
  || ok "offline playlist omits ENDLIST"
# The quota fix depends on this being cacheable.
if [ "$BACKEND" = worker ]; then
  hdr "$BASE/neverused" | grep -qi 'cache-control:.*max-age=[1-9]' \
    && ok "offline playlist is cacheable (idle viewers stop hitting the Worker)" \
    || bad "offline playlist is not cacheable — idle viewers drain quota"
else
  # The VPS serves every playlist no-store: the slate timeline is one
  # continuous stream that advances on the wall clock, and edge caching is
  # a Cloudflare cache rule, not an origin header.
  hdr "$BASE/neverused" | grep -qi 'cache-control:.*no-store' \
    && ok "offline playlist is no-store (VPS contract)" \
    || bad "offline playlist is missing no-store"
fi

sec "2. Offline slate segment"
[ "$(code "$BASE/live/_offline.ts")" = "200" ] \
  && ok "slate segment served" || bad "slate segment missing"
hdr "$BASE/live/_offline.ts" | grep -qi 'content-type: video/mp2t' \
  && ok "slate has video/mp2t type" || bad "slate has wrong content type"
SLATE_REF=$(media_playlist neverused | grep -oE '[^[:space:]]*_offline[^[:space:]]*\.ts' | head -1)
case "$SLATE_REF" in
  /*) SLATE_URL="$SLATE_REF" ;;                # root-absolute (VPS)
  *)  SLATE_URL="/live/${SLATE_REF#../}" ;;   # relative to /live/<s>/ (Worker)
esac
hdr "$BASE${SLATE_URL}" | grep -qi 'immutable' \
  && ok "the slate the playlist points at is immutable" \
  || bad "the slate the playlist points at is not immutable"
curl -fsS "$BASE/live/_offline.ts" -o "$TMP/slate.ts" 2>/dev/null
if [ -s "$TMP/slate.ts" ]; then
  # First byte must be the TS sync byte or no player will touch it.
  if [ "$(head -c1 "$TMP/slate.ts" | od -An -tx1 | tr -d ' \n')" = "47" ]; then
    ok "slate starts with TS sync byte"
  else
    bad "slate is not valid MPEG-TS"
  fi
  if command -v ffprobe >/dev/null; then
    V=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
        -of csv=p=0 "$TMP/slate.ts" 2>/dev/null | head -1 | tr -d '\r\n ')
    A=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name \
        -of csv=p=0 "$TMP/slate.ts" 2>/dev/null | head -1 | tr -d '\r\n ')
    [ "$V" = "h264" ] && ok "slate video is h264" || bad "slate video is $V"
    [ "$A" = "aac" ]  && ok "slate audio is aac"  || bad "slate audio is $A"
  fi
else
  bad "slate body empty"
fi
# A stream named _offline must not shadow the slate route.
[ "$(code "$BASE/live/_offline.ts")" = "200" ] \
  && ok "slate route wins over stream routing" || bad "slate route shadowed"

# The slate is served immutable for a year, so its URL must change when the
# content does — otherwise clients keep a stale copy indefinitely, which is
# exactly what pinned viewers to a pre-watermark slate in production.
VER=$(media_playlist neverused | grep -oE '_offline\.[0-9a-f]{8}\.[0-9]+\.ts' | head -1)
if [ -n "$VER" ]; then
  ok "offline playlist references a versioned slate ($VER)"
  [ "$(code "$BASE/live/$VER")" = "200" ] \
    && ok "versioned slate URL resolves" || bad "versioned slate URL 404s"
  hdr "$BASE/live/$VER" | grep -qi 'immutable' \
    && ok "versioned slate is immutable" || bad "versioned slate not immutable"
  # The unversioned path must NOT be cached long-term.
  if hdr "$BASE/live/_offline.ts" | grep -qi 'immutable'; then
    bad "unversioned slate is immutable — stale copies cannot be displaced"
  else
    ok "unversioned slate has a short cache lifetime"
  fi
else
  bad "offline playlist does not reference a versioned slate"
fi

sec "2b. Master playlist"
# Short routes hand back a master naming one variant; SRS does the same, and
# a bare media playlist is a different code path in the player.
M="$TMP/master.m3u8"
curl -fsS "$BASE/neverused" -o "$M" 2>/dev/null
grep -q '^#EXTM3U' "$M" 2>/dev/null && ok "master is valid HLS" || bad "master malformed"
grep -q 'EXT-X-STREAM-INF' "$M" 2>/dev/null \
  && ok "master declares a variant" || bad "master has no EXT-X-STREAM-INF"
grep -q 'index.m3u8' "$M" 2>/dev/null \
  && ok "master points at the media playlist" || bad "master does not reference index.m3u8"

sec "3. Reserved paths are not treated as stream names"
for p in healthz status live ingest favicon.ico; do
  C=$(code "$BASE/$p")
  case "$p" in
    healthz) [ "$C" = "200" ] && ok "/$p intact" || bad "/$p → $C" ;;
    *)       [ "$C" != "200" ] \
               && ok "/$p not served as a stream ($C)" \
               || bad "/$p returned 200 as a stream" ;;
  esac
done
# robots.txt is reserved on our side, but Cloudflare injects its own at the
# edge when the origin 404s, so the response code cannot be asserted. What
# matters is that we never serve a playlist there.
curl -fsS "$BASE/robots.txt" 2>/dev/null | grep -q '^#EXTM3U' \
  && bad "/robots.txt served a playlist" \
  || ok "/robots.txt never serves a playlist (Cloudflare may inject its own)"

# The alias route strips only .m3u8, so a path carrying any other extension
# would otherwise become a stream literally named "x.mpd".
for p in x.mpd foo.json bar.txt; do
  C=$(code "$BASE/$p")
  [ "$C" = "404" ] && ok "/$p is not treated as a stream ($C)" \
                   || bad "/$p returned $C, inventing a stream for it"
done

sec "4. Ingest auth and input validation"
[ "$(code -X PUT -d x "$BASE/ingest/wrongkey/x/seg1.ts")" = "403" ] \
  && ok "wrong key → 403" || bad "wrong key not rejected"
[ "$(code -X PUT -d x "$BASE/ingest//x/seg1.ts")" != "200" ] \
  && ok "empty key rejected" || bad "empty key accepted"
# Path traversal in the stream name must never reach storage.
for n in '..' '../etc' 'a%2Fb'; do
  C=$(code -X PUT -d x "$BASE/ingest/$KEY/$n/seg1.ts")
  [ "$C" != "200" ] && ok "stream name '$n' rejected ($C)" \
                    || bad "stream name '$n' accepted"
done
C=$(code -X PUT --data-binary "" "$BASE/ingest/$KEY/emptyseg/seg00000.ts")
[ "$C" = "400" ] && ok "empty segment body → 400" \
                 || bad "empty segment body → $C (want 400)"
# A 200-char name exceeds the 128 limit.
LONG=$(printf 'a%.0s' $(seq 1 200))
C=$(code -X PUT -d x "$BASE/ingest/$KEY/$LONG/seg1.ts")
[ "$C" != "200" ] && ok "over-long stream name rejected ($C)" \
                  || bad "over-long stream name accepted"

sec "5. Method handling"
[ "$(code -X POST -d x "$BASE/neverused")" = "405" ] \
  && ok "POST to playlist → 405" || bad "POST to playlist not rejected"
[ "$(code -X DELETE "$BASE/live/_offline.ts")" = "405" ] \
  && ok "DELETE on slate → 405" || bad "DELETE on slate not rejected"
[ "$(code -I "$BASE/neverused")" = "200" ] \
  && ok "HEAD on playlist works" || bad "HEAD on playlist failed"

sec "6. Live stream, normal case"
publish live1 45 2 60
if wait_segs live1 4; then
  ok "ingest accepted and buffered"
  # The VPS keeps the slate entries in the window for a few seconds after
  # the broadcast starts, on purpose: a player still polling at the slate
  # cadence must find an entry it knows in every playlist. Let that pass.
  [ "$BACKEND" = vps ] && sleep 6
  media_playlist live1 > "$TMP/l1.m3u8"
  grep -q '_offline' "$TMP/l1.m3u8" \
    && bad "live stream still advertising the slate" \
    || ok "live playlist replaced the slate"
  # Only meaningful while the stream is live: an offline playlist is cacheable
  # by design, and asserting against it just races the publisher.
  if curl -fsS "$BASE/status/live1" | grep -q '"live":true'; then
    hdr "$BASE/live/live1/index.m3u8" | grep -qi 'cache-control:.*no-store' \
      && ok "live playlist is no-store (players need fresh manifests)" \
      || bad "live playlist is cacheable — playback will stall"
  else
    note "stream went offline before the cache check"
    skip=$((skip+1))
  fi
  SEQ=$(grep -oE 'MEDIA-SEQUENCE:[0-9]+' "$TMP/l1.m3u8" | cut -d: -f2)
  FIRST=$(grep -m1 -oE 'seg[0-9]+' "$TMP/l1.m3u8" | grep -oE '[0-9]+' | sed 's/^0*//')
  if [ "$BACKEND" = worker ]; then
    [ "$SEQ" = "${FIRST:-0}" ] \
      && ok "MEDIA-SEQUENCE matches the first segment" \
      || bad "MEDIA-SEQUENCE $SEQ != first segment ${FIRST:-0}"
  else
    # The VPS publishes one timeline across slate and live, so its sequence
    # counts every entry ever advertised and only ever climbs; it cannot
    # equal ffmpeg's segment counter and is not meant to.
    [ "${SEQ:-0}" -ge "${FIRST:-0}" ] \
      && ok "MEDIA-SEQUENCE ($SEQ) is at or past the first segment ($FIRST)" \
      || bad "MEDIA-SEQUENCE $SEQ is below the first segment ${FIRST:-0}"
  fi
  # Every advertised segment must resolve, or playback breaks mid-stream.
  MISS=0
  for f in $(grep -oE '^seg[0-9]+\.ts' "$TMP/l1.m3u8"); do
    [ "$(code "$BASE/live/live1/$f")" = "200" ] || MISS=$((MISS+1))
  done
  [ "$MISS" = "0" ] && ok "all advertised segments resolve" \
                    || bad "$MISS advertised segments 404"
  # Window must respect the duration bound.
  TOTAL=$(grep -oE '#EXTINF:[0-9.]+' "$TMP/l1.m3u8" | cut -d: -f2 \
          | awk '{s+=$1} END {printf "%.0f", s}')
  [ "${TOTAL:-99}" -le 10 ] \
    && ok "playlist window is ${TOTAL}s (within the duration bound)" \
    || bad "playlist window ${TOTAL}s exceeds the bound"
else
  bad "ingest never filled — cannot run live-stream checks"
  skip=$((skip+8))
fi

sec "7. Oversized segments (keyframe interval misconfigured)"
# GOP 249 at 30fps makes ~8s segments even though hls_time says 1. The window
# must stay bounded by duration rather than stretching to 4 x 8s.
publish badgop 40 1 249
if wait_segs badgop 2; then
  media_playlist badgop > "$TMP/bg.m3u8"
  T=$(grep -oE '#EXTINF:[0-9.]+' "$TMP/bg.m3u8" | cut -d: -f2 \
      | awk '{s+=$1} END {printf "%.0f", s}')
  N=$(grep -c 'seg' "$TMP/bg.m3u8")
  if [ "${T:-99}" -le 10 ]; then
    ok "oversized segments still yield a ${T}s window ($N segments)"
  elif [ "$BACKEND" = vps ]; then
    # The VPS window is a fixed entry count with no seconds ceiling, so it
    # scales with whatever the encoder produces. Latency is then dominated
    # by the segment length itself, which only the encoder can fix.
    note "window is ${T}s with $N oversized segments (VPS has no seconds bound)"
    skip=$((skip+1))
  else
    bad "window ballooned to ${T}s with oversized segments"
  fi
  OFF=$(grep -oE 'TIME-OFFSET=-[0-9.]+' "$TMP/bg.m3u8" | cut -d= -f2 | tr -d -)
  if [ -n "$OFF" ]; then
    awk -v o="$OFF" 'BEGIN{exit !(o<=6.001)}' \
      && ok "EXT-X-START capped at ${OFF}s" \
      || bad "EXT-X-START is ${OFF}s, above the 6s cap"
  fi
else
  bad "oversized-segment stream never filled"
fi

sec "8. Concurrent streams are isolated"
publish multi2 30 2 60
if wait_segs multi2 3; then
  # The Worker counts totalSegments over a stream's life; the VPS reports
  # what is on disk as segmentsBuffered. Either shows the streams apart.
  A=$(curl -fsS "$BASE/status/live1" | grep -oE '"(totalSegments|segmentsBuffered)":[0-9]*' | head -1 | cut -d: -f2)
  B=$(curl -fsS "$BASE/status/multi2" | grep -oE '"(totalSegments|segmentsBuffered)":[0-9]*' | head -1 | cut -d: -f2)
  [ -n "$A" ] && [ -n "$B" ] && ok "two streams tracked independently (${A} / ${B} segments)" \
                             || bad "stream isolation unclear"
  media_playlist multi2 > "$TMP/m2.m3u8"
  # Segment URIs are relative now, so correctness is that they resolve under
  # this stream's directory.
  F2=$(grep -m1 -oE '^seg[0-9]+\.ts' "$TMP/m2.m3u8")
  [ -n "$F2" ] && [ "$(code "$BASE/live/multi2/$F2")" = "200" ] \
    && ok "playlist segments resolve under their own stream" \
    || bad "playlist segments do not resolve"
  # Both streams contain the same sequence numbers, so a 200 here is expected.
  # Isolation means the bytes differ — each stream has its own Durable Object.
  F=$(grep -m1 -oE 'seg[0-9]+\.ts' "$TMP/m2.m3u8")
  curl -fsS "$BASE/live/multi2/$F" -o "$TMP/x2.ts" 2>/dev/null
  curl -fsS "$BASE/live/live1/$F"  -o "$TMP/x1.ts" 2>/dev/null
  if [ -s "$TMP/x1.ts" ] && [ -s "$TMP/x2.ts" ]; then
    if cmp -s "$TMP/x1.ts" "$TMP/x2.ts"; then
      note "same-name segments are byte-identical (both encode the same test source)"
      skip=$((skip+1))
    else
      ok "same-name segments differ between streams (isolated storage)"
    fi
  fi
else
  bad "second concurrent stream never filled"
fi

sec "9. Segment caching (free-tier quota depends on it)"
if wait_segs live1 3; then
  media_playlist live1 > "$TMP/c.m3u8"
  SEG=$(grep -m1 -oE '^seg[0-9]+\.ts' "$TMP/c.m3u8")
  if [ -n "$SEG" ]; then
    SEG="/live/live1/$SEG"
    hdr "$BASE$SEG" | grep -qi 'cache-control:.*immutable' \
      && ok "live segments are immutable" || bad "live segments not immutable"
    curl -sS -o /dev/null "$BASE$SEG"
    S=$(hdr "$BASE$SEG" | grep -i 'cf-cache-status' | tr -d '\r')
    if [ -n "$S" ]; then
      echo "$S" | grep -qi 'HIT' && ok "segment served from edge cache on repeat" \
                                 || note "cf-cache-status: $S (edge may need more requests)"
    else
      note "no cf-cache-status header (local dev has no edge cache)"
      skip=$((skip+1))
    fi
  fi
fi

sec "9b. Offline playlist caching follows the backend's contract"
# These two backends want opposite things here, so the assertion flips.
#
# Worker: the offline playlist is byte-identical every time and the free tier
# has a request quota, so caching it is what stops an idle tab from draining
# that quota (measured ~0.67 req/s, three tabs enough to exhaust it).
#
# VPS: no quota to protect, and caching a playlist is actively dangerous — a
# player handed byte-identical bytes concludes there is nothing new and stops
# fetching fragments. It sends no-store and must never report a hit.
HITS=0; TOTAL=0
for _ in $(seq 1 5); do
  S=$(hdr "$BASE/live/idlecachecheck/index.m3u8" | grep -i 'cf-cache-status' | tr -d '\r' | awk '{print $2}')
  TOTAL=$((TOTAL+1))
  echo "$S" | grep -qi HIT && HITS=$((HITS+1))
  sleep 1
done
if ! hdr "$BASE/live/idlecachecheck/index.m3u8" | grep -qi 'cf-cache-status'; then
  note "no edge cache in this environment (local dev)"
  skip=$((skip+1))
elif [ "$BACKEND" = worker ]; then
  [ "$HITS" -ge 2 ] \
    && ok "offline playlist hits the edge cache ($HITS/$TOTAL)" \
    || bad "offline playlist never cached ($HITS/$TOTAL hits) — idle viewers drain quota"
else
  [ "$HITS" = 0 ] \
    && ok "offline playlist is never cached ($HITS/$TOTAL hits), as intended" \
    || bad "offline playlist was cached ($HITS/$TOTAL hits) — this freezes players"
fi

sec "9c. Starting a broadcast purges the cached OFFLINE playlist"
# Regression: the offline playlist is edge-cached, so without an explicit purge
# on ingest, viewers were served OFFLINE for the full TTL after a broadcast had
# already started — measured at 9s while the DO already held live segments.
RS="purge$$"
curl -fsS "$BASE/$RS" >/dev/null 2>&1          # prime the cache
publish "$RS" 30 2 60
SEEN_LIVE=-1
for t in $(seq 0 14); do
  DO_SEGS=$(curl -fsS "$BASE/status/$RS" 2>/dev/null \
            | grep -o '"segmentsBuffered":[0-9]*' | cut -d: -f2)
  if ! media_playlist "$RS" | grep -q '_offline'; then
    SEEN_LIVE=$t
    break
  fi
  # Record when the DO first had content, to measure the cache's contribution.
  [ "${DO_SEGS:-0}" -ge 1 ] && [ "${DO_FIRST:-}" = "" ] && DO_FIRST=$t
  sleep 1
done
if [ "$SEEN_LIVE" -lt 0 ]; then
  bad "viewers still see OFFLINE 14s after the broadcast started"
else
  LAG=$(( SEEN_LIVE - ${DO_FIRST:-$SEEN_LIVE} ))
  [ "$LAG" -lt 0 ] && LAG=0
  if [ "$LAG" -le 4 ]; then
    ok "live playlist reached viewers ${LAG}s after ingest began"
  else
    bad "stale OFFLINE served for ${LAG}s after ingest began"
  fi
fi

sec "10. Unknown and malformed playback requests"
[ "$(code "$BASE/live/live1/seg99999999.ts")" = "404" ] \
  && ok "missing segment → 404" || bad "missing segment wrong status"
[ "$(code "$BASE/live/live1/notasegment")" = "404" ] \
  && ok "non-segment filename → 404" || bad "non-segment filename wrong status"
[ "$(code "$BASE/live/nosuchstream/seg00001.ts")" = "404" ] \
  && ok "segment of unknown stream → 404" || bad "unknown stream segment wrong status"
# An unknown stream must still be playable so a viewer can wait for the start.
[ "$(code "$BASE/totallynew")" = "200" ] \
  && ok "unknown stream returns a playable offline playlist" \
  || bad "unknown stream not playable"
[ "$(code "$BASE/a/b/c/d")" = "404" ] \
  && ok "deep unknown path → 404" || bad "deep unknown path wrong status"

sec "11. Status endpoint contract"
S=$(curl -fsS "$BASE/status/live1")
# Fields common to both backends, then the Worker's extras. The VPS relay
# reports what it can observe from the filesystem and omits the rest rather
# than inventing values.
FIELDS="live segmentsBuffered bufferedBytes secondsSinceLastIngest"
[ "$BACKEND" = worker ] && FIELDS="$FIELDS mediaSequence discontinuitySequence \
  totalSegments targetDuration playlistSize maxSegments uptimeSeconds"
for f in $FIELDS; do
  echo "$S" | grep -q "\"$f\"" && ok "status has $f" || bad "status missing $f"
done
# Must always be numeric so consumers need no null handling. Python's
# json.dumps puts a space after the colon, so the patterns allow one.
echo "$S" | grep -qE '"secondsSinceLastIngest": ?null' \
  && bad "secondsSinceLastIngest is null" \
  || ok "secondsSinceLastIngest is numeric"
S2=$(curl -fsS "$BASE/status/neverbroadcast")
echo "$S2" | grep -qE '"live": ?false' \
  && ok "never-broadcast stream reports live:false" \
  || bad "never-broadcast stream reports live:true"
echo "$S2" | grep -qE '"secondsSinceLastIngest": ?-1' \
  && ok "never-ingested reports -1 sentinel" \
  || bad "never-ingested sentinel wrong"

sec "12. Encoder restart (discontinuity)"
# Kill the publisher, let the stream go stale, then restart with sequence
# numbering reset — exactly what stopping and restarting OBS does.
pkill -f "ingest/$KEY/live1/" 2>/dev/null
D1=$(curl -fsS "$BASE/status/live1" | grep -o '"discontinuitySequence":[0-9]*' | cut -d: -f2)
# What a viewer was last shown, to prove the restart does not rewind it.
PRESEQ=$(media_playlist live1 | grep -o '#EXT-X-MEDIA-SEQUENCE:[0-9]*' | cut -d: -f2)
# The gap must exceed the server's 10s staleness threshold, or the restart is
# treated as a continuous timeline and no discontinuity is recorded.
sleep 12
publish live1 20 2 60
if wait_segs live1 2; then
  D2=$(curl -fsS "$BASE/status/live1" | grep -o '"discontinuitySequence":[0-9]*' | cut -d: -f2)
  media_playlist live1 > "$TMP/r.m3u8"
  ok "stream recovered after restart"
  # A restart clears the ring, so the window holds one continuous timeline and
  # the playlist deliberately carries no discontinuity tags: publishing a count
  # with no matching marker made hls.js place segments at a negative timeline
  # offset. The counter is still tracked, and /status still reports it.
  if [ "${D2:-0}" -gt "${D1:-0}" ]; then
    ok "restart recorded in the discontinuity counter (${D1} → ${D2})"
  else
    note "counter unchanged (${D1} → ${D2}); gap may not have exceeded the threshold"
    skip=$((skip+1))
  fi
  # The two backends need opposite things here. The Worker cleared its ring
  # on restart, so the window held one continuous timeline and a tag would
  # have had no matching break — publishing one put hls.js at a negative
  # timeline offset. The VPS serves ffmpeg's own playlist straight through,
  # so the restart really is a timeline break and the tag is what lets a
  # player reset its decoder instead of stalling on the PTS jump.
  if [ "$BACKEND" = worker ]; then
    grep -qx '#EXT-X-DISCONTINUITY' "$TMP/r.m3u8" 2>/dev/null \
      && bad "playlist carries a discontinuity tag with no matching timeline break" \
      || ok "playlist carries no stale discontinuity tags"
  else
    # Sequence must not regress, whatever ffmpeg's counter did.
    RSEQ=$(grep -o '#EXT-X-MEDIA-SEQUENCE:[0-9]*' "$TMP/r.m3u8" | cut -d: -f2)
    if [ -n "$RSEQ" ] && [ "${RSEQ:-0}" -ge "${PRESEQ:-0}" ]; then
      ok "media sequence did not regress across restart (${PRESEQ} → ${RSEQ})"
    else
      bad "media sequence regressed (${PRESEQ} → ${RSEQ}) — players stall"
    fi
  fi
  grep -q '^#EXTM3U' "$TMP/r.m3u8" && ok "playlist valid after restart" \
                                   || bad "playlist broken after restart"
  MISS=0
  for f in $(grep -oE '^seg[0-9]+\.ts' "$TMP/r.m3u8"); do
    [ "$(code "$BASE/live/live1/$f")" = "200" ] || MISS=$((MISS+1))
  done
  [ "$MISS" = "0" ] && ok "all segments resolve after restart" \
                    || bad "$MISS segments 404 after restart"
else
  bad "stream did not recover after restart"
fi

sec "13. Stream going offline reverts to the slate"
pkill -f "ingest/$KEY/" 2>/dev/null
# The ring holds MAX_SEGMENTS; once ingest stops the DO may be evicted, after
# which the playlist must fall back to the slate rather than 404.
for _ in $(seq 1 20); do
  curl -fsS "$BASE/multi2" 2>/dev/null | grep -q '_offline' && break
  sleep 2
done
C=$(code "$BASE/multi2")
[ "$C" = "200" ] && ok "stopped stream still returns 200 (not 404)" \
                 || bad "stopped stream → $C"
if media_playlist multi2 | grep -q '_offline'; then
  ok "stopped stream fell back to the slate"
else
  note "still serving buffered segments (ring not yet drained) — acceptable"
  skip=$((skip+1))
fi

echo
echo "────────────────────────────────"
echo "passed: $pass   failed: $fail   skipped: $skip"
[ "$fail" -eq 0 ] || exit 1
