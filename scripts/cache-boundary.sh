#!/usr/bin/env bash
# Cache-correctness tests for KRSZ Live behind the Cloudflare edge.
#
# A Cache Rule that caches .ts segments is what lifts capacity from ~14
# viewers to effectively unbounded. But caching a live stream is exactly
# where staleness bugs live: a cached playlist freezes the stream, a cached
# offline slate hides a broadcast that already started, and a segment served
# after eviction shows the wrong picture.
#
# These tests drive the real edge, not a local server, because the bugs being
# hunted are edge behaviour. Run against the deployed host.
#
# Usage: scripts/cache-boundary.sh <base-url> <ingest-key>
set -uo pipefail

BASE="${1:-https://live.krsz.in}"
KEY="${2:-cxk114514}"
TMP="$(mktemp -d)"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done
  wait 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

pass=0; fail=0; warn=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
wrn()  { echo "  WARN  $1"; warn=$((warn+1)); }
note() { echo "        $1"; }
sec()  { echo; echo "── $1"; }

# cf-cache-status for a URL, lowercased. Empty if absent.
cfstatus() {
  curl -sS -D- -o /dev/null "$1" 2>/dev/null \
    | tr -d '\r' | grep -i '^cf-cache-status:' | awk '{print tolower($2)}'
}
hdrval() {
  curl -sS -D- -o /dev/null "$2" 2>/dev/null \
    | tr -d '\r' | grep -i "^$1:" | head -1 | cut -d' ' -f2-
}

publish() {
  local name="$1" dur="$2"
  ffmpeg -hide_banner -loglevel error -re \
    -f lavfi -i "testsrc2=size=640x360:rate=30" \
    -f lavfi -i "sine=frequency=440" -t "$dur" \
    -c:v libx264 -preset ultrafast -profile:v main -bf 0 \
    -g 30 -keyint_min 30 -sc_threshold 0 -b:v 600k -pix_fmt yuv420p \
    -c:a aac -b:a 64k -ar 48000 -ac 2 \
    -f hls -hls_time 1 -hls_list_size 6 \
    -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \
    -method PUT -http_persistent 1 -ignore_io_errors 1 \
    -hls_segment_filename "$BASE/ingest/$KEY/$name/seg%05d.ts" \
    "$BASE/ingest/$KEY/$name/live.m3u8" >"$TMP/$name.log" 2>&1 &
  PIDS+=($!)
  echo $!
}

is_live() {
  curl -fsS "$BASE/status/$1" 2>/dev/null \
    | grep -o '"live": *[a-z]*' | awk '{print $2}'
}
seq_of() {
  curl -fsS "$BASE/live/$1/index.m3u8" 2>/dev/null \
    | grep -o '#EXT-X-MEDIA-SEQUENCE:[0-9]*' | cut -d: -f2
}
first_seg() {
  curl -fsS "$BASE/live/$1/index.m3u8" 2>/dev/null \
    | grep -v '^#' | head -1 | tr -d '\r'
}
wait_live() {
  for _ in $(seq 1 45); do
    [ "$(is_live "$1")" = "true" ] && return 0
    sleep 1
  done
  return 1
}

STREAM="cbtest$$"
echo "target: $BASE   stream: $STREAM"

# ─────────────────────────────────────────────────────────────────────
sec "1. Playlist must never be cached"
# This is the single most dangerous thing to cache. A frozen manifest means
# the player re-reads identical bytes and stops fetching new fragments.
for path in "/live/$STREAM/index.m3u8" "/$STREAM.m3u8" "/live/neverused/index.m3u8"; do
  st=$(cfstatus "$BASE$path")
  cc=$(hdrval "cache-control" "$BASE$path")
  case "$st" in
    hit|stale|updating|revalidated)
      bad "$path is edge-cached (cf-cache-status: $st) — will freeze players" ;;
    *)
      ok "$path not edge-cached (${st:-none})" ;;
  esac
  case "$cc" in
    *no-store*) ok "$path sends no-store" ;;
    "")         bad "$path has NO Cache-Control (Cloudflare may have stripped it)" ;;
    *)          wrn "$path Cache-Control: $cc" ;;
  esac
done

# ─────────────────────────────────────────────────────────────────────
sec "2. Offline slate playlist rolls while cached-adjacent"
# The offline playlist advances its sequence with wall time. If it were
# cached, a viewer sitting on the offline screen would never see a stream
# start. Two reads a few seconds apart must differ.
a=$(seq_of neverused); sleep 4; b=$(seq_of neverused)
if [ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ]; then
  ok "offline sequence advanced ($a → $b)"
elif [ "$a" = "$b" ]; then
  bad "offline sequence stuck at $a — offline viewers will never see a stream start"
else
  bad "could not read offline sequence"
fi

# ─────────────────────────────────────────────────────────────────────
sec "3. Segments are edge-cached (the capacity win)"
pid=$(publish "$STREAM" 150)
if ! wait_live "$STREAM"; then
  bad "stream never went live — aborting"
  echo; echo "pass=$pass fail=$fail warn=$warn"; exit 1
fi
ok "stream is live"

seg=$(first_seg "$STREAM")
if [ -z "$seg" ]; then
  bad "no segment in playlist"
else
  url="$BASE/live/$STREAM/$seg"
  s1=$(cfstatus "$url"); s2=$(cfstatus "$url"); s3=$(cfstatus "$url")
  note "$seg: $s1 → $s2 → $s3"
  if [ "$s2" = "hit" ] || [ "$s3" = "hit" ]; then
    ok "segments edge-cached — origin serves one pull per region"
  elif [ "$s1" = "dynamic" ]; then
    wrn "segments NOT cached (DYNAMIC) — Cache Rule missing; capacity ~14 viewers"
  else
    wrn "unexpected segment cache status: $s1/$s2/$s3"
  fi
fi

# ─────────────────────────────────────────────────────────────────────
sec "4. Live playlist keeps advancing (not frozen by any cache layer)"
p1=$(seq_of "$STREAM"); sleep 5; p2=$(seq_of "$STREAM")
if [ -n "$p1" ] && [ -n "$p2" ] && [ "$p2" -gt "$p1" ] 2>/dev/null; then
  ok "media sequence advanced ($p1 → $p2)"
else
  bad "media sequence did not advance ($p1 → $p2) — stream appears frozen"
fi

# ─────────────────────────────────────────────────────────────────────
sec "5. Viewer leaves, returns later — must resume at the live edge"
# A returning viewer re-reads the playlist. If anything served them a stale
# manifest they would get segments that no longer exist.
before=$(seq_of "$STREAM")
note "viewer away for 20s (window is ~3 segments, so it fully rolls over)"
sleep 20
pl="$TMP/return.m3u8"
curl -fsS "$BASE/live/$STREAM/index.m3u8" -o "$pl"
after=$(grep -o '#EXT-X-MEDIA-SEQUENCE:[0-9]*' "$pl" | cut -d: -f2)
if [ -n "$after" ] && [ "$after" -gt "$before" ] 2>/dev/null; then
  ok "returning viewer sees a fresh window ($before → $after)"
else
  bad "returning viewer got a stale window ($before → $after)"
fi
# Every segment the returning viewer is told to fetch must actually exist.
missing=0; total=0
while read -r s; do
  [ -z "$s" ] && continue
  total=$((total+1))
  c=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/live/$STREAM/$s")
  [ "$c" = "200" ] || { missing=$((missing+1)); note "  $s → HTTP $c"; }
done < <(grep -v '^#' "$pl" | tr -d '\r')
if [ "$missing" = "0" ] && [ "$total" -gt 0 ]; then
  ok "all $total advertised segments fetchable"
else
  bad "$missing of $total advertised segments unfetchable"
fi

# ─────────────────────────────────────────────────────────────────────
sec "6. Encoder stops, then resumes on the same stream"
kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
note "publisher stopped; waiting 12s"
sleep 12
st=$(is_live "$STREAM")
note "status after stop: live=$st"
pl2="$TMP/afterstop.m3u8"
curl -fsS "$BASE/live/$STREAM/index.m3u8" -o "$pl2" 2>/dev/null
if grep -q '^#EXTM3U' "$pl2" 2>/dev/null; then
  ok "playlist still valid HLS after encoder stopped"
else
  bad "playlist malformed after encoder stopped"
fi

note "restarting publisher on the same stream"
seq_pre=$(seq_of "$STREAM")
pid2=$(publish "$STREAM" 60)
if wait_live "$STREAM"; then
  ok "stream went live again after restart"
else
  bad "stream did not recover after restart"
fi
sleep 6
seq_post=$(seq_of "$STREAM")
note "sequence across restart: $seq_pre → $seq_post"
# ffmpeg restarts its counter at seg00000. The server derives sequence from
# the filename, so a restart can move it backwards — legal only if the player
# is told the timeline broke, otherwise it stalls.
if [ -n "$seq_post" ] && [ "$seq_post" -lt "${seq_pre:-0}" ] 2>/dev/null; then
  if grep -q 'EXT-X-DISCONTINUITY' "$TMP/disc.m3u8" 2>/dev/null; then
    ok "sequence went backwards but discontinuity is signalled"
  else
    wrn "sequence went backwards ($seq_pre → $seq_post) with no DISCONTINUITY"
    note "players may stall; a fresh stream name avoids this"
  fi
else
  ok "sequence did not regress across restart"
fi
# Whatever the sequence did, the advertised segments must be fetchable.
curl -fsS "$BASE/live/$STREAM/index.m3u8" -o "$TMP/disc.m3u8"
miss2=0; tot2=0
while read -r s; do
  [ -z "$s" ] && continue
  tot2=$((tot2+1))
  c=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/live/$STREAM/$s")
  [ "$c" = "200" ] || { miss2=$((miss2+1)); note "  $s → HTTP $c"; }
done < <(grep -v '^#' "$TMP/disc.m3u8" | tr -d '\r')
if [ "$miss2" = "0" ] && [ "$tot2" -gt 0 ]; then
  ok "all $tot2 segments fetchable after restart"
else
  bad "$miss2 of $tot2 segments unfetchable after restart"
fi
kill "$pid2" 2>/dev/null; wait "$pid2" 2>/dev/null

# ─────────────────────────────────────────────────────────────────────
sec "7. Offline viewer sees a stream that starts mid-watch"
# The scenario you named: someone is staring at the OFFLINE slate when the
# broadcast begins. If the offline response is cached anywhere, they stay on
# the slate forever.
FRESH="cbstart$$"
note "polling $FRESH while offline, then starting a broadcast"
off1=$(curl -fsS "$BASE/live/$FRESH/index.m3u8" | grep -c 'offline' || true)
pid3=$(publish "$FRESH" 45)
switched=""
for i in $(seq 1 40); do
  body=$(curl -fsS "$BASE/live/$FRESH/index.m3u8" 2>/dev/null)
  if ! echo "$body" | grep -q 'offline'; then
    switched="$i"; break
  fi
  sleep 1
done
if [ -n "$switched" ]; then
  ok "offline viewer saw live content after ${switched}s of polling"
  if [ "$switched" -le 8 ]; then
    ok "switchover was prompt (${switched}s)"
  else
    wrn "switchover took ${switched}s — slower than a segment cycle"
  fi
else
  bad "offline viewer never saw live content — offline response is stale/cached"
fi
kill "$pid3" 2>/dev/null; wait "$pid3" 2>/dev/null

# ─────────────────────────────────────────────────────────────────────
sec "8. Stream ends — viewer returns to offline, not a dead playlist"
note "waiting for reap/offline fallback (up to 40s)"
back=""
for i in $(seq 1 40); do
  body=$(curl -fsS "$BASE/live/$FRESH/index.m3u8" 2>/dev/null)
  if echo "$body" | grep -q 'offline'; then back="$i"; break; fi
  sleep 1
done
if [ -n "$back" ]; then
  ok "fell back to the offline slate after ${back}s"
else
  wrn "did not fall back to offline within 40s"
  note "stale-stream reaping is set to ${REAP:-300}s, so this may be expected"
fi

# ─────────────────────────────────────────────────────────────────────
sec "9. Slate asset is versioned so a new slate is never pinned"
sl=$(curl -fsS "$BASE/live/neverused/index.m3u8" | grep -v '^#' | head -1 | tr -d '\r')
note "slate segment: $sl"
case "$sl" in
  *_offline.*.ts|*offline.*.ts)
    ok "slate URL carries a version/fingerprint" ;;
  *offline.ts)
    wrn "slate URL is unversioned — a new slate can stay pinned in caches" ;;
  *)
    wrn "unrecognised slate URL: $sl" ;;
esac
sc=$(cfstatus "$BASE/live/neverused/$sl")
ccs=$(hdrval "cache-control" "$BASE/live/neverused/$sl")
note "slate cache: status=${sc:-none} cache-control=${ccs:-none}"

echo
echo "───────────────────────────────────────"
echo "pass=$pass  fail=$fail  warn=$warn"
[ "$fail" = "0" ] || exit 1
