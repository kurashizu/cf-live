#!/usr/bin/env bash
# Set up KRSZ Live on a VPS. Idempotent — safe to re-run.
#
# Expects nginx (with the dav module) and cloudflared already installed.
# Run as root, or with sudo.
set -euo pipefail

KEY="${1:-}"
if [ -z "$KEY" ]; then
  echo "usage: $0 <ingest-key>" >&2
  echo "  the key OBS puts in its URL; use a long random string" >&2
  exit 1
fi

SEG_DIR=/dev/shm/cf-live
WWW_DIR=/var/www/cf-live
CONF=/etc/nginx/conf.d/cf-live.conf
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> checking prerequisites"
command -v nginx >/dev/null || { echo "nginx not installed" >&2; exit 1; }
if ! nginx -V 2>&1 | tr ' ' '\n' | grep -q -- '--with-http_dav_module' \
   && [ ! -f /usr/lib/nginx/modules/ngx_http_dav_module.so ]; then
  echo "nginx has no dav module; ingest cannot work as designed" >&2
  exit 1
fi

echo "==> creating directories"
# Segments go in RAM: written once, read for seconds, then deleted. Putting
# them on disk only wears out a cheap VPS for no benefit.
mkdir -p "$SEG_DIR/offline" "$WWW_DIR"
chown -R "${NGINX_USER:-www-data}:${NGINX_USER:-www-data}" "$SEG_DIR" 2>/dev/null || true

echo "==> installing the offline slate"
# Reuses the segment generated for the Worker build, so the offline screen is
# identical to what it was there.
if [ -f "$HERE/offline.ts" ]; then
  cp "$HERE/offline.ts" "$SEG_DIR/offline/offline.ts"
  # A rolling playlist, not a single static entry: a one-segment playlist with
  # a fixed sequence looks finished to a player, which stops polling and so
  # never notices the broadcast starting.
  cat > "$SEG_DIR/offline/live.m3u8" <<'EOF'
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-TARGETDURATION:2
#EXTINF:2.000, no desc
/live/offline.ts
#EXTINF:2.000, no desc
/live/offline.ts
#EXTINF:2.000, no desc
/live/offline.ts
EOF
else
  echo "    offline.ts not found; skipping (streams will 404 when idle)"
fi

echo "==> installing the setup page"
[ -f "$HERE/index.html" ] && cp "$HERE/index.html" "$WWW_DIR/index.html" \
  || echo "    index.html not found; skipping"

echo "==> installing the OBS overlay"
[ -f "$HERE/overlay.html" ] && cp "$HERE/overlay.html" "$WWW_DIR/overlay.html" \
  || echo "    overlay.html not found; skipping (/overlay will 404)"

echo "==> installing nginx config"
sed "s/CHANGE_ME_INGEST_KEY/$KEY/" "$HERE/nginx.conf" > "$CONF"
nginx -t
systemctl reload nginx

echo "==> persisting the segment directory across reboots"
# /dev/shm is cleared on boot, so recreate the tree via tmpfiles.
cat > /etc/tmpfiles.d/cf-live.conf <<EOF
d $SEG_DIR 0755 ${NGINX_USER:-www-data} ${NGINX_USER:-www-data} -
d $SEG_DIR/offline 0755 ${NGINX_USER:-www-data} ${NGINX_USER:-www-data} -
EOF

echo
echo "done. nginx listens on 127.0.0.1:8080 — point cloudflared at it:"
echo
echo "  ingress:"
echo "    - hostname: <your-hostname>"
echo "      service: http://127.0.0.1:8080"
echo "    - service: http_status:404"
echo
echo "OBS URL:  https://<your-hostname>/ingest/$KEY/main/live.m3u8"
echo "Playback: https://<your-hostname>/main"
