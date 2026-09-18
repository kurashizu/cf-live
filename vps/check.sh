#!/usr/bin/env sh
# Run this on the VPS first. It reports whether the box can host the stream
# with nothing but nginx and cloudflared.
echo "=== system ==="
. /etc/os-release 2>/dev/null && echo "  $PRETTY_NAME" || uname -a

echo "=== memory ==="
free -m 2>/dev/null | awk '/^Mem:/ {printf "  total %sMB  used %sMB  available %sMB\n", $2, $3, $7}'

echo "=== nginx ==="
if command -v nginx >/dev/null 2>&1; then
  nginx -v 2>&1 | sed 's/^/  /'
  # WebDAV is what lets OBS PUT segments straight in; without it, ingest needs
  # a different approach entirely.
  if nginx -V 2>&1 | tr ' ' '\n' | grep -q '\-\-with-http_dav_module'; then
    echo "  dav module: built in"
  elif [ -f /usr/lib/nginx/modules/ngx_http_dav_module.so ]; then
    echo "  dav module: available as a loadable module"
  else
    echo "  dav module: NOT FOUND — ingest will not work as designed"
  fi
else
  echo "  not installed"
fi

echo "=== cloudflared ==="
command -v cloudflared >/dev/null 2>&1 \
  && cloudflared --version 2>&1 | head -1 | sed 's/^/  /' \
  || echo "  not installed"

echo "=== tmpfs for segments ==="
# Segments are written once and read for seconds; keeping them in RAM avoids
# thrashing a cheap VPS's disk.
df -h /dev/shm 2>/dev/null | tail -1 | awk '{printf "  /dev/shm  size %s  avail %s\n", $2, $4}' \
  || echo "  /dev/shm not present"

echo "=== disk ==="
df -h / 2>/dev/null | tail -1 | awk '{printf "  /  size %s  avail %s\n", $2, $4}'
