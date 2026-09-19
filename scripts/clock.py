#!/usr/bin/env python3
"""Generate a test stream that burns Beijing and Sydney wall-clock into the
picture, so end-to-end latency can be read off the screen.

Pure Python: this platform's ffmpeg build has no drawtext filter, so the
frames are drawn here and piped in as raw video. A 5x7 bitmap font keeps it
dependency-free, matching scripts/slate.py.

Read the latency by comparing the burnt-in time against a clock beside the
player. Both zones are shown because the origin is in Sydney and the
viewers are in Beijing, so whichever you can check locally works.
"""
import datetime
import struct
import sys
import zlib

W, H = 1280, 720
# 15 fps: a clock needs legibility, not motion, and halving the rate halves
# the frame-drawing cost on a machine whose ffmpeg cannot do this in C.
FPS = 15

BG = (8, 9, 11)
FG = (235, 235, 235)
DIM = (120, 125, 132)
ACCENT = (74, 222, 128)

# 5x7 glyphs. Digits and colon are what a clock needs; letters label the rows.
F = {
 '0': ["01110","10001","10011","10101","11001","10001","01110"],
 '1': ["00100","01100","00100","00100","00100","00100","01110"],
 '2': ["01110","10001","00001","00010","00100","01000","11111"],
 '3': ["11110","00001","00001","01110","00001","00001","11110"],
 '4': ["00010","00110","01010","10010","11111","00010","00010"],
 '5': ["11111","10000","10000","11110","00001","00001","11110"],
 '6': ["00110","01000","10000","11110","10001","10001","01110"],
 '7': ["11111","00001","00010","00100","01000","01000","01000"],
 '8': ["01110","10001","10001","01110","10001","10001","01110"],
 '9': ["01110","10001","10001","01111","00001","00010","01100"],
 ':': ["00000","01100","01100","00000","01100","01100","00000"],
 '.': ["00000","00000","00000","00000","00000","01100","01100"],
 '-': ["00000","00000","00000","11111","00000","00000","00000"],
 ' ': ["00000"] * 7,
 'B': ["11110","10001","10001","11110","10001","10001","11110"],
 'E': ["11111","10000","10000","11110","10000","10000","11111"],
 'I': ["11111","00100","00100","00100","00100","00100","11111"],
 'J': ["00111","00010","00010","00010","00010","10010","01100"],
 'N': ["10001","11001","10101","10011","10001","10001","10001"],
 'G': ["01110","10001","10000","10111","10001","10001","01110"],
 'S': ["01111","10000","10000","01110","00001","00001","11110"],
 'Y': ["10001","10001","01010","00100","00100","00100","00100"],
 'D': ["11110","10001","10001","10001","10001","10001","11110"],
 'F': ["11111","10000","10000","11110","10000","10000","10000"],
 'R': ["11110","10001","10001","11110","10100","10010","10001"],
 'M': ["10001","11011","10101","10101","10001","10001","10001"],
 'A': ["01110","10001","10001","11111","10001","10001","10001"],
 'K': ["10001","10010","10100","11000","10100","10010","10001"],
 'Z': ["11111","00001","00010","00100","01000","10000","11111"],
 'L': ["10000","10000","10000","10000","10000","10000","11111"],
 'V': ["10001","10001","10001","10001","10001","01010","00100"],
 'C': ["01110","10001","10000","10000","10000","10001","01110"],
 'H': ["10001","10001","10001","11111","10001","10001","10001"],
 'O': ["01110","10001","10001","10001","10001","10001","01110"],
 'T': ["11111","00100","00100","00100","00100","00100","00100"],
 'U': ["10001","10001","10001","10001","10001","10001","01110"],
 'P': ["11110","10001","10001","11110","10000","10000","10000"],
 'W': ["10001","10001","10001","10101","10101","11011","10001"],
}


def draw(buf, text, x0, y0, scale, colour):
    """Blit text with a 5x7 font scaled by an integer factor."""
    for ch in text.upper():
        rows = F.get(ch, F[' '])
        for ry, row in enumerate(rows):
            for rx, bit in enumerate(row):
                if bit != '1':
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px = x0 + rx * scale + dx
                        py = y0 + ry * scale + dy
                        if 0 <= px < W and 0 <= py < H:
                            o = (py * W + px) * 3
                            buf[o:o + 3] = bytes(colour)
        x0 += (len(rows[0]) + 1) * scale


_BLANK = bytes(BG) * (W * H)


def frame(now):
    # Copy a prebuilt background: filling it per pixel per frame cost more
    # than the entire frame budget.
    buf = bytearray(_BLANK)

    bj = now + datetime.timedelta(hours=8)     # UTC+8
    syd = now + datetime.timedelta(hours=10)   # UTC+10, AEST

    def hms(t):
        return "%02d:%02d:%02d.%d" % (t.hour, t.minute, t.second,
                                      t.microsecond // 100000)

    draw(buf, "KRSZ LIVE  LATENCY TEST", 60, 60, 3, ACCENT)
    draw(buf, "BEIJING", 60, 170, 4, DIM)
    draw(buf, hms(bj), 60, 220, 9, FG)
    draw(buf, "SYDNEY", 60, 400, 4, DIM)
    draw(buf, hms(syd), 60, 450, 9, FG)
    # A moving bar gives a coarse read even if the digits blur in transit.
    phase = (now.microsecond / 1e6 + now.second) % 4.0 / 4.0
    bx = int(60 + phase * (W - 220))
    for y in range(640, 680):
        for x in range(bx, min(bx + 160, W)):
            o = (y * W + x) * 3
            buf[o:o + 3] = bytes(ACCENT)
    return bytes(buf)


def main():
    # Raw frames on stdout are unreadable in a terminal, and piping them
    # there by accident is a confusing way to find that out.
    if sys.stdout.isatty():
        sys.stderr.write(
            "clock.py writes raw RGB frames to stdout; it does not stream "
            "by itself.\n"
            "Use scripts/clock-stream.sh to encode and push, or pipe this "
            "into ffmpeg.\n")
        return 1
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    import time
    t_end = time.time() + seconds
    interval = 1.0 / FPS
    nxt = time.time()
    out = sys.stdout.buffer
    while time.time() < t_end:
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            out.write(frame(now))
            out.flush()
        except (BrokenPipeError, ValueError):
            return
        nxt += interval
        delay = nxt - time.time()
        if delay > 0:
            time.sleep(delay)
        else:
            nxt = time.time()


if __name__ == "__main__":
    sys.exit(main() or 0)
