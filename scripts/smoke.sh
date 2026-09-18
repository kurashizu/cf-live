#!/usr/bin/env bash
# End-to-end smoke test: publish a synthetic stream, then verify a real HLS
# client can play it back through the Worker.
#
# Usage: scripts/smoke.sh [base-url] [ingest-key] [stream]
set -uo pipefail

BASE="${1:-http://127.0.0.1:8787}"
# Local dev key; override for any real deployment.
KEY="${2:-local-dev-key}"
STREAM="${3:-smoke}"
SEG_DUR=1
FPS=30
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; [ -n "${PUB_PID:-}" ] && kill "$PUB_PID" 2>/dev/null' EXIT

pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }

command -v ffmpeg  >/dev/null || { echo "ffmpeg not found";  exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe not found"; exit 1; }

echo "target: $BASE  stream: $STREAM"
echo

echo "health"
curl -fsS "$BASE/healthz" >/dev/null && ok "/healthz reachable" || { bad "/healthz unreachable"; exit 1; }

echo "publishing ${SEG_DUR}s segments"
ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i "testsrc2=size=1280x720:rate=$FPS" \
  -f lavfi -i "sine=frequency=440" -t 180 \
  -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -bf 0 \
  -g $((FPS*SEG_DUR)) -keyint_min $((FPS*SEG_DUR)) -sc_threshold 0 \
  -b:v 2500k -pix_fmt yuv420p \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  -f hls -hls_time "$SEG_DUR" -hls_list_size 6 \
  -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \
  -method PUT -http_persistent 1 -ignore_io_errors 1 \
  -hls_segment_filename "$BASE/ingest/$KEY/$STREAM/seg%05d.ts" \
  "$BASE/ingest/$KEY/$STREAM/live.m3u8" >"$TMP/pub.log" 2>&1 &
PUB_PID=$!

# Wait for the buffer to fill rather than sleeping a fixed amount.
for _ in $(seq 1 30); do
  n=$(curl -fsS "$BASE/status/$STREAM" 2>/dev/null \
      | grep -o '"segmentsBuffered":[0-9]*' | cut -d: -f2)
  [ "${n:-0}" -ge 5 ] && break
  sleep 1
done
[ "${n:-0}" -ge 5 ] && ok "ingest buffered ${n} segments" || bad "ingest did not fill (got ${n:-0})"

echo
echo "playlist"
# The short routes return a master playlist naming one variant; the tags below
# live in the media playlist it points at, so follow it the way a player does.
MASTER="$TMP/master.m3u8"
curl -fsS "$BASE/$STREAM.m3u8" -o "$MASTER" && ok "master playlist served" \
                                            || bad "master playlist failed"
grep -q 'EXT-X-STREAM-INF' "$MASTER" 2>/dev/null \
  && ok "master declares a variant" || bad "master has no variant"

PL="$TMP/pl.m3u8"
curl -fsS "$BASE/live/$STREAM/index.m3u8" -o "$PL" && ok "media playlist served" \
                                                   || bad "media playlist failed"
grep -q '^#EXTM3U'                 "$PL" && ok "has #EXTM3U"          || bad "missing #EXTM3U"
grep -q '#EXT-X-TARGETDURATION'    "$PL" && ok "has TARGETDURATION"   || bad "missing TARGETDURATION"
grep -q '#EXT-X-MEDIA-SEQUENCE'    "$PL" && ok "has MEDIA-SEQUENCE"   || bad "missing MEDIA-SEQUENCE"
grep -q '#EXT-X-ENDLIST'           "$PL" && bad "ENDLIST present (would end a live stream)" \
                                         || ok "no ENDLIST (correct for live)"

# MEDIA-SEQUENCE must match the first segment's own counter, or players
# miscompute the live edge.
SEQ=$(grep -oE 'MEDIA-SEQUENCE:[0-9]+' "$PL" | cut -d: -f2)
FIRST=$(grep -m1 -oE 'seg[0-9]+' "$PL" | grep -oE '[0-9]+' | sed 's/^0*//')
FIRST=${FIRST:-0}
[ "$SEQ" = "$FIRST" ] && ok "MEDIA-SEQUENCE($SEQ) matches first segment($FIRST)" \
                      || bad "MEDIA-SEQUENCE($SEQ) != first segment($FIRST)"

echo
echo "caching headers"
curl -fsSI "$BASE/live/$STREAM/index.m3u8" | grep -qi 'cache-control:.*no-store' \
  && ok "media playlist is no-store" || bad "media playlist is cacheable (stalls hls.js)"
SEGFILE=$(grep -m1 -oE "^seg[0-9]+\.ts" "$PL")
curl -fsSI "$BASE/live/$STREAM/$SEGFILE" | grep -qi 'cache-control:.*immutable' \
  && ok "segments are immutable" || bad "segments not immutable"

echo
echo "playback (real HLS client)"
if ffmpeg -hide_banner -loglevel error -i "$BASE/live/$STREAM.m3u8" \
     -t 4 -c copy -y "$TMP/out.ts" >"$TMP/play.log" 2>&1; then
  DUR=$(ffprobe -hide_banner -loglevel error -show_entries format=duration \
        -of csv=p=0 "$TMP/out.ts" 2>/dev/null | cut -d. -f1)
  VID=$(ffprobe -hide_banner -loglevel error -select_streams v:0 -show_entries stream=codec_name \
        -of csv=p=0 "$TMP/out.ts" 2>/dev/null | head -1 | tr -d '\r\n ')
  AUD=$(ffprobe -hide_banner -loglevel error -select_streams a:0 -show_entries stream=codec_name \
        -of csv=p=0 "$TMP/out.ts" 2>/dev/null | head -1 | tr -d '\r\n ')
  [ "${DUR:-0}" -ge 3 ] && ok "decoded ${DUR}s of media" || bad "decoded only ${DUR:-0}s"
  [ "$VID" = "h264" ]   && ok "video is h264 (AVPro-compatible)" || bad "video codec: $VID"
  [ "$AUD" = "aac" ]    && ok "audio is aac (AVPro-compatible)"  || bad "audio codec: $AUD"
else
  bad "HLS client could not play the stream"
  tail -3 "$TMP/play.log" | sed 's/^/        /'
fi

echo
echo "short alias routes"
# The same playlist must be reachable from all three paths, and reserved
# endpoints must never be shadowed by the /<stream> alias.
for path in "/$STREAM" "/$STREAM.m3u8" "/live/$STREAM.m3u8"; do
  if curl -fsS "$BASE$path" | grep -q '^#EXTM3U'; then
    ok "$path serves the playlist"
  else
    bad "$path did not serve a playlist"
  fi
done
for path in /healthz /status/$STREAM /; do
  c=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE$path")
  [ "$c" = "200" ] && ok "$path still reachable (alias did not shadow it)" \
                   || bad "$path returned $c"
done

echo
echo "auth and validation"
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
[ "$(code -X PUT -d x "$BASE/ingest/wrongkey/$STREAM/s.ts")" = "403" ] \
  && ok "wrong key rejected (403)" || bad "wrong key not rejected"
[ "$(code "$BASE/live/$STREAM/seg99999999.ts")" = "404" ] \
  && ok "missing segment is 404" || bad "missing segment wrong status"
# An unknown stream must return a valid empty playlist, not 404: a 404 makes
# AVPro give up permanently instead of polling until the broadcast starts.
[ "$(code "$BASE/live/definitelynotlive.m3u8")" = "200" ] \
  && ok "unknown stream returns playable empty playlist" \
  || bad "unknown stream should be 200, not 404"

echo
echo "status shape"
S=$(curl -fsS "$BASE/status/$STREAM")
for f in live segmentsBuffered mediaSequence totalSegments bufferedBytes secondsSinceLastIngest; do
  echo "$S" | grep -q "\"$f\"" && ok "status has $f" || bad "status missing $f"
done

echo
echo "-------------------------------"
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ] || exit 1
