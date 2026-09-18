#!/usr/bin/env python3
"""Generate the animated OFFLINE slate frames for cf-live.

Pure-Python PNG writer with a 5x7 bitmap font, so the build needs no fonts,
no Pillow, and no ffmpeg drawtext (which this platform's build lacks).

Writes frames/f%04d.png. The animation is a ping-pong sweep: the motion is
symmetric in time, so the last frame leads back into the first and the 2s
segment loops without a visible jump.
"""
import struct, zlib, sys, os, math

W, H = 854, 480
FPS = 15
SECONDS = 2
FRAMES = FPS * SECONDS

BG = (10, 11, 14)
FG = (150, 154, 163)
DIM = (52, 55, 62)
ACCENT = (246, 130, 31)   # Cloudflare orange, matches the web UI

F = {
 'A':["01110","10001","10001","11111","10001","10001","10001"],
 'C':["01110","10001","10000","10000","10000","10001","01110"],
 'D':["11110","10001","10001","10001","10001","10001","11110"],
 'E':["11111","10000","10000","11110","10000","10000","11111"],
 'F':["11111","10000","10000","11110","10000","10000","10000"],
 'G':["01110","10001","10000","10111","10001","10001","01110"],
 'H':["10001","10001","10001","11111","10001","10001","10001"],
 'I':["11111","00100","00100","00100","00100","00100","11111"],
 'K':["10001","10010","10100","11000","10100","10010","10001"],
 'L':["10000","10000","10000","10000","10000","10000","11111"],
 'M':["10001","11011","10101","10101","10001","10001","10001"],
 'N':["10001","11001","10101","10011","10001","10001","10001"],
 'O':["01110","10001","10001","10001","10001","10001","01110"],
 'R':["11110","10001","10001","11110","10100","10010","10001"],
 'S':["01111","10000","10000","01110","00001","00001","11110"],
 'T':["11111","00100","00100","00100","00100","00100","00100"],
 'V':["10001","10001","10001","10001","10001","01010","00100"],
 'W':["10001","10001","10001","10101","10101","11011","10001"],
 'Z':["11111","00001","00010","00100","01000","10000","11111"],
 'P':["11110","10001","10001","11110","10000","10000","10000"],
 'U':["10001","10001","10001","10001","10001","10001","01110"],
 'Y':["10001","10001","01010","00100","00100","00100","00100"],
 'B':["11110","10001","10001","11110","10001","10001","11110"],
 'J':["00111","00010","00010","00010","00010","10010","01100"],
 'Q':["01110","10001","10001","10001","10101","10010","01101"],
 'X':["10001","10001","01010","00100","01010","10001","10001"],
 '-':["00000","00000","00000","11111","00000","00000","00000"],
 '.':["00000","00000","00000","00000","00000","01100","01100"],
 ':':["00000","01100","01100","00000","01100","01100","00000"],
 '/':["00001","00010","00010","00100","01000","01000","10000"],
 ' ':["00000"]*7,
}

def blend(a, b, t):
    """Linear mix, t=0 -> a, t=1 -> b."""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))

def render(frame):
    rows = [bytearray(bytes(BG) * W) for _ in range(H)]

    def px(x, y, c):
        if 0 <= x < W and 0 <= y < H:
            rows[y][x*3:x*3+3] = bytes(c)

    def rect(x0, y0, x1, y1, c):
        for y in range(max(0, y0), min(H, y1)):
            for x in range(max(0, x0), min(W, x1)):
                px(x, y, c)

    def text_width(s, scale, track=1):
        """Rendered width in pixels, so callers can align blocks by edge."""
        return len(s) * (5 + track) * scale - track * scale

    def text(s, cx, cy, scale, color, track=1, align='center'):
        gw = (5 + track) * scale
        total = text_width(s, scale, track)
        x0 = cx if align == 'left' else (cx - total if align == 'right'
                                         else cx - total // 2)
        y0 = cy - (7 * scale) // 2
        for i, ch in enumerate(s.upper()):
            g = F.get(ch)
            if g is None:
                print(f"warning: no glyph for {ch!r}", file=sys.stderr)
                g = F[' ']
            for r, line in enumerate(g):
                for col, bit in enumerate(line):
                    if bit == '1':
                        for dy in range(scale):
                            for dx in range(scale):
                                px(x0 + i*gw + col*scale + dx,
                                   y0 + r*scale + dy, color)

    # Phase runs 0..1 over the segment. A cosine gives a ping-pong sweep whose
    # velocity is zero at each end, so the loop point is not just continuous in
    # position but in motion too -- no visible jerk every 2s.
    phase = frame / FRAMES
    sweep = (1 - math.cos(2 * math.pi * phase)) / 2   # 0 -> 1 -> 0

    # Frame
    rect(0, 0, W, 2, DIM)
    rect(0, H-2, W, H, DIM)

    # Sweep bar: a soft highlight travelling along a track under the heading.
    track_y = 300
    track_x0, track_x1 = W//2 - 200, W//2 + 200
    rect(track_x0, track_y, track_x1, track_y + 2, (26, 28, 33))
    bar_w = 110
    bar_x = round(track_x0 + (track_x1 - track_x0 - bar_w) * sweep)
    for i in range(bar_w):
        # Triangular falloff so the bar fades at both ends.
        edge = 1 - abs(i - bar_w/2) / (bar_w/2)
        rect(bar_x + i, track_y, bar_x + i + 1, track_y + 2,
             blend((26, 28, 33), ACCENT, edge))

    # Breathing dot trio under the sweep, each offset in phase.
    for k in range(3):
        p = (phase + k / 3.0) % 1.0
        glow = (1 - math.cos(2 * math.pi * p)) / 2
        c = blend(DIM, FG, glow)
        cx = W//2 - 30 + k * 30
        rect(cx - 4, 340, cx + 4, 348, c)

    # Top-left lockup. The tagline is letter-spaced to match the width of the
    # line above it, so the two form an aligned block rather than two
    # independently centred strings.
    WM_X, WM_SCALE = 40, 2
    brand = "KRSZ LIVE"
    tag = "HIGH PERFORMANCE"
    brand_w = text_width(brand, WM_SCALE, 1)
    # Solve for the tracking that makes the tagline span the same width:
    #   len*(5+t)*s - t*s = brand_w  ->  t = (brand_w/s - 5*len) / (len - 1)
    tag_scale = 1
    tag_track = max(1, round((brand_w / tag_scale - 5 * len(tag)) / (len(tag) - 1)))
    text(brand, WM_X, 40, WM_SCALE, blend(BG, ACCENT, 0.75), track=1, align='left')
    text(tag, WM_X, 62, tag_scale, DIM, track=tag_track, align='left')

    text("OFFLINE", W//2, 190, 7, FG)
    text("WAITING FOR STREAM", W//2, 258, 3, DIM)

    # Attribution, bottom-right, same size as the top-left brand.
    text("HTTPS://KRSZ.IN", W - 40, H - 40, WM_SCALE,
         blend(BG, ACCENT, 0.75), track=1, align='right')

    raw = b''.join(b'\x00' + bytes(r) for r in rows)

    def chunk(t, d):
        return (struct.pack('>I', len(d)) + t + d
                + struct.pack('>I', zlib.crc32(t+d) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6))
            + chunk(b'IEND', b''))

out = sys.argv[1] if len(sys.argv) > 1 else 'frames'
os.makedirs(out, exist_ok=True)
for n in range(FRAMES):
    open(os.path.join(out, f'f{n:04d}.png'), 'wb').write(render(n))
print(f"{FRAMES} frames -> {out}/ ({W}x{H} @{FPS}fps, {SECONDS}s loop)")
