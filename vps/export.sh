#!/usr/bin/env bash
# Export the Worker build's assets as plain files for the VPS.
#
# Reuses two things that took real effort to get right: the animated OFFLINE
# slate (correct codecs, seamless loop) and the setup page (player, one-click
# OBS config, live status).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> slate segment"
# Decode the base64 blob the Worker embeds back into a .ts file.
node -e "
global.atob = s => Buffer.from(s, 'base64').toString('binary');
import('./src/slate.js').then(m => {
  require('fs').writeFileSync('vps/offline.ts', Buffer.from(m.slateBytes()));
  console.log('    vps/offline.ts', m.slateBytes().length, 'bytes');
});
"

echo "==> setup page"
# Render the page with VPS-appropriate defaults. It is a pure function of
# config, so this produces the same HTML the Worker would serve.
node --input-type=module -e "
import { landingPage } from './src/page.js';
import { writeFileSync } from 'node:fs';
const res = landingPage(new URL('https://REPLACE_ME/'), {
  SEGMENT_DURATION: '1',
  PLAYLIST_SIZE: '3',
  MAX_SEGMENTS: '8',
});
const html = await res.text();
// The page builds absolute URLs from location.origin at runtime, so the
// placeholder origin only needs stripping where it was baked in.
const patched = html.replaceAll('https://REPLACE_ME', '');
writeFileSync('vps/index.html', patched);
console.log('    vps/index.html', patched.length, 'bytes');
"

echo
echo "copy vps/ to the server, then:"
echo "  sudo ./install.sh <ingest-key>"
