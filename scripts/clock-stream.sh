#!/usr/bin/env bash
# Push a latency test pattern: Beijing and Sydney wall clocks burnt into the
# picture, so end-to-end delay can be read by comparing the screen against a
# clock beside the player.
#
# The clock is rendered in Python because ffmpeg's drawtext filter is absent
# from some builds (including this project's dev machine). clock.py writes
# raw frames to stdout; ffmpeg encodes and uploads them.
#
# Usage:
#   scripts/clock-stream.sh                          # defaults below
#   scripts/clock-stream.sh <host> <key> <stream> <minutes>
set -uo pipefail
cd "$(dirname "$0")/.."

HOST="${1:-https://live.krsz.in}"
KEY="${2:-cxk114514}"
STREAM="${3:-main}"
MINUTES="${4:-30}"
SECONDS_TOTAL=$(python3 -c "print(int(${MINUTES}*60))")

# Must match clock.py. The keyframe interval is what actually decides
# segment length -- a segment can only be cut on a keyframe, so a mismatch
# silently produces longer segments than hls_time asks for.
FPS=15
SEG=0.5
GOP=$(python3 -c "print(max(1,int($FPS*$SEG)))")

command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }

echo "pushing clock test pattern"
echo "  target : $HOST/ingest/$KEY/$STREAM/live.m3u8"
echo "  play   : $HOST/$STREAM"
echo "  video  : 1280x720 @ ${FPS}fps, ${SEG}s segments, keyframe every $GOP frames"
echo "  runs   : ${MINUTES} min (Ctrl-C to stop)"
echo

python3 scripts/clock.py "$SECONDS_TOTAL" 2>/dev/null | ffmpeg -hide_banner -loglevel warning \
  -f rawvideo -pix_fmt rgb24 -s 1280x720 -r "$FPS" -i - \
  -f lavfi -i "sine=frequency=440" \
  -map 0:v -map 1:a \
  -c:v libx264 -preset veryfast -profile:v main -bf 0 \
  -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
  -b:v 800k -pix_fmt yuv420p \
  -c:a aac -b:a 64k -ar 48000 -ac 2 \
  -f hls -hls_time "$SEG" -hls_list_size 6 \
  -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \
  -method PUT -http_persistent 1 -ignore_io_errors 1 \
  -hls_segment_filename "$HOST/ingest/$KEY/$STREAM/seg%05d.ts" \
  "$HOST/ingest/$KEY/$STREAM/live.m3u8"
