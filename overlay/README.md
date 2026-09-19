# OBS overlay

A single transparent HTML file that draws the KRSZ Live brand lockup over a
scene, styled to match the OFFLINE slate (`scripts/slate.py`): same palette,
same 5×7 bitmap font, same ping-pong sweep. Live and offline look like one
identity instead of two.

No build step, no fonts, no network — everything is in `overlay.html`.

## Add it to OBS

The server hosts it, so there is no file to manage:

1. Sources → **+** → **Browser**
2. Leave **Local file** unticked and paste the URL:
   `https://live.krsz.in/overlay?live=1&clock=1&tzlabel=SYD`
3. Width `1920`, Height `1080`
4. Leave **Shutdown source when not visible** unticked, so the animation does
   not restart every scene change.

The page is transparent; OBS composites the alpha, so only the drawn pixels
land on the scene.

## Options

Append them to the URL, e.g.
`https://live.krsz.in/overlay?live=1&clock=1&tzlabel=SYD`.

| Option | Default | What it does |
|---|---|---|
| `brand` | `KRSZ LIVE` | Headline text |
| `tag` | `HIGH PERFORMANCE` | Letter-spaced subtitle, auto-tracked to the brand's width |
| `url` | `HTTPS://KRSZ.IN` | Opposite-corner attribution; `url=` hides it |
| `pos` | `tl` | Lockup corner: `tl`, `tr`, `bl`, `br` |
| `scale` | `1` | Size multiplier |
| `accent` | `f6821f` | Accent colour, hex without `#` |
| `live` | `0` | `1` adds a LIVE pill with a breathing dot |
| `clock` | `0` | `1` adds a running clock (handy for proving latency on stream) |
| `fade` | `0.6` | Fade-in seconds on load |
| `shadow` | `1` | Drop shadow; `0` turns it off |
| `tz` | `Australia/Sydney` | Clock timezone, any IANA name; `local` uses the machine's own |
| `tzlabel` | *(none)* | Short label before the digits, e.g. `SYD` |
| `secs` | `1` | `0` shows `HH:MM` instead of `HH:MM:SS` |

Example — right corner, custom brand, LIVE pill, no attribution:

```
https://live.krsz.in/overlay?pos=tr&brand=MY%20CHANNEL&tag=TOKYO&live=1&url=
```

Spaces must be `%20`.

## Notes

- Only characters in the slate's font render; anything else becomes a space.
  That is A–Z, 0–9, and `- . : /` — the font has no lowercase, so text is
  upper-cased automatically.
- The shadow is on by default. The slate sits on its own dark background, but
  an overlay sits on whatever the camera sees, and the dim tagline is
  unreadable over a bright shot without it.
- The sweep uses the slate's 2.1333 s period, so both animations breathe at
  the same rate.
