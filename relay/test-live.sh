#!/usr/bin/env bash
# End-to-end tests for the cross-border relay, run against the deployed pair.
#
# The link is the thing under test as much as the code: ~20% datagram loss,
# 280 ms RTT, and a stateful pinhole that only exists while the receiver
# keeps poking. So these check integrity and continuity over time rather
# than a single successful fetch.
set -uo pipefail

CN="${1:-http://47.116.180.38}"
ORIGIN="${2:-https://live.krsz.in}"
STREAM="${3:-main}"
UA=(-H "User-Agent: krsz-test/1")

pass=0; fail=0; warn=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }
wrn() { echo "  WARN  $1"; warn=$((warn+1)); }
note(){ echo "        $1"; }
sec() { echo; echo "-- $1"; }

pl_cn()  { curl -sS --max-time 10 "$CN/$STREAM" 2>/dev/null; }
pl_org() { curl -sS --max-time 10 "${UA[@]}" "$ORIGIN/live/$STREAM/index.m3u8" 2>/dev/null; }
code()   { curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null; }
seq_of() { echo "$1" | grep -o 'MEDIA-SEQUENCE:[0-9]*' | cut -d: -f2; }

sec "1. Relay serves valid HLS on bare-IP port 80"
B=$(pl_cn)
echo "$B" | grep -q '^#EXTM3U' && ok "playlist is valid HLS" || bad "playlist malformed"
echo "$B" | grep -q 'EXT-X-MEDIA-SEQUENCE' && ok "carries a media sequence" || bad "no media sequence"
echo "$B" | grep -q 'EXT-X-ENDLIST' && bad "playlist announces an end (players stop polling)" \
                                    || ok "playlist never announces an end"
N=$(echo "$B" | grep -vc '^#')
[ "${N:-0}" -ge 1 ] && ok "advertises $N fragments" || bad "advertises no fragments"

sec "2. Every advertised fragment is fetchable"
miss=0; tot=0
while read -r u; do
  [ -z "$u" ] && continue
  tot=$((tot+1))
  c=$(code "$CN/$u")
  [ "$c" = "200" ] || { miss=$((miss+1)); note "$u -> HTTP $c"; }
done < <(echo "$B" | grep -v '^#' | tr -d '\r')
[ "$miss" = "0" ] && [ "$tot" -gt 0 ] && ok "all $tot fragments fetchable" \
  || bad "$miss of $tot fragments unfetchable"

sec "3. Relayed bytes are identical to the origin"
same=0; diff=0; skip=0
while read -r u; do
  [ -z "$u" ] && continue
  a=$(curl -sS --max-time 10 "$CN/$u" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
  b=$(curl -sS --max-time 10 "${UA[@]}" "$ORIGIN/live/$STREAM/$u" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
  if [ -z "$b" ] || [ "$b" = "$(printf '' | shasum -a 256 | cut -d' ' -f1)" ]; then
    skip=$((skip+1)); continue        # rolled out of the origin window
  fi
  if [ "$a" = "$b" ]; then same=$((same+1)); else
    diff=$((diff+1)); note "$u differs: $a vs $b"; fi
done < <(echo "$B" | grep -v '^#' | tr -d '\r' | head -4)
[ "$diff" = "0" ] && [ "$same" -gt 0 ] && ok "$same fragments byte-identical ($skip rolled out)" \
  || bad "$diff fragments differ from origin"

sec "4. Playlist keeps advancing (the stream is live, not frozen)"
S1=$(seq_of "$(pl_cn)"); sleep 6; S2=$(seq_of "$(pl_cn)")
if [ -n "$S1" ] && [ -n "$S2" ] && [ "$S2" -gt "$S1" ] 2>/dev/null; then
  ok "sequence advanced ($S1 -> $S2)"
else
  bad "sequence did not advance ($S1 -> $S2)"
fi

sec "5. Sequence never rewinds (a rewind forces players to rebuild)"
prev=-1; rew=0; polls=0
for i in $(seq 1 20); do
  q=$(seq_of "$(pl_cn)")
  if [ -n "$q" ]; then
    polls=$((polls+1))
    [ "$prev" -ge 0 ] && [ "$q" -lt "$prev" ] && { rew=$((rew+1)); note "rewind $prev -> $q"; }
    prev="$q"
  fi
  sleep 1
done
[ "$rew" = "0" ] && ok "no rewind across $polls polls" || bad "$rew rewinds"

sec "6. A viewer can follow the live edge without stalling"
# The real failure mode: fragments rolling out of the window before a
# viewer fetches them, which is exactly what killed HLS-over-TCP here.
seen=""; stalls=0; got=0
END=$(( $(date +%s) + 45 ))
while [ "$(date +%s)" -lt "$END" ]; do
  P=$(pl_cn)
  newest=$(echo "$P" | grep -v '^#' | tail -1 | tr -d '\r')
  if [ -n "$newest" ] && ! echo "$seen" | grep -qx "$newest"; then
    seen="$seen
$newest"
    c=$(code "$CN/$newest")
    if [ "$c" = "200" ]; then got=$((got+1)); else stalls=$((stalls+1)); note "stall $newest -> $c"; fi
  fi
  sleep 0.5
done
if [ "$got" -gt 0 ] && [ "$stalls" = "0" ]; then
  ok "followed the live edge for 45s: $got fragments, 0 stalls"
elif [ "$got" -gt 0 ]; then
  bad "$stalls stalls while following the edge ($got ok)"
else
  bad "no fragments retrieved while following the edge"
fi

sec "7. Cache headers are correct for live HLS"
H=$(curl -sS -D- -o /dev/null --max-time 10 "$CN/$STREAM" 2>/dev/null | tr -d '\r')
echo "$H" | grep -qi 'cache-control:.*no-store' && ok "playlist sends no-store" \
  || wrn "playlist cache-control: $(echo "$H" | grep -i cache-control | head -1)"
FRAG=$(echo "$B" | grep -v '^#' | head -1 | tr -d '\r')
HF=$(curl -sS -D- -o /dev/null --max-time 10 "$CN/$FRAG" 2>/dev/null | tr -d '\r')
echo "$HF" | grep -qi 'cache-control:.*max-age' && ok "fragments are cacheable" \
  || wrn "fragment cache-control missing"
echo "$HF" | grep -qi 'content-type:.*MP2T' && ok "fragment MIME is video/MP2T" \
  || bad "wrong fragment MIME: $(echo "$HF" | grep -i content-type | head -1)"
echo "$H" | grep -qi 'access-control-allow-origin' && ok "CORS header present" \
  || wrn "no CORS header (browsers will refuse)"

sec "8. The pre-existing site on this host is untouched"
c=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -H "Host: jjylffz.shljs365.com" "$CN/" 2>/dev/null)
[ "$c" = "301" ] && ok "domain on port 80 still 301s" || bad "domain port 80 returned $c (expected 301)"
c=$(curl -sSk -o /dev/null -w '%{http_code}' --max-time 10 --resolve jjylffz.shljs365.com:443:47.116.180.38 "https://jjylffz.shljs365.com/" 2>/dev/null)
[ "$c" = "200" ] && ok "site on 443 still serves 200" || bad "443 returned $c (expected 200)"
c=$(curl -sSk -o /dev/null -w '%{http_code}' --max-time 10 --resolve jjylffz.shljs365.com:443:47.116.180.38 "https://jjylffz.shljs365.com/supplier" 2>/dev/null)
[ "$c" = "301" ] && ok "/supplier still 301s" || bad "/supplier returned $c (expected 301)"

echo
echo "-----------------------------------"
echo "pass=$pass fail=$fail warn=$warn"
[ "$fail" = "0" ] || exit 1
