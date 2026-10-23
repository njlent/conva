---
name: conva
description: Convert images and image sequences between file formats and color spaces with the `conva` CLI (OpenColorIO/ACES, ffmpeg-style flags). Use when asked to convert EXR/DPX/PNG/JPG/WebP/AVIF/HEIC/TIFF/camera raw files, change color space (ACEScg, ACES2065-1, ACEScct, sRGB, Rec.709, P3, Rec.2020, camera log like LogC/S-Log3/V-Log), apply an ACES output transform (display/view), make review JPGs or web proxies from renders or plates, convert frame sequences (####, %04d), resize or change bit depth/compression, or inspect an image's format and metadata.
---

# conva

`conva` is an ffmpeg-style CLI for still images and image sequences. It uses OpenImageIO for I/O
and OpenColorIO for color, and the ACES configs are built in.

## Before you start

1. Check it is installed: `conva -version`. If it's missing, install it with
   `uv tool install git+https://github.com/njlent/conva` (a private repo, so it needs GitHub access).
2. Inspect the input first: `conva -i <file or pattern>`. This prints the format, resolution,
   channels, bit depth, frame range, `oiio:ColorSpace` metadata and the config's color space guess.
3. Look up names instead of guessing them: `conva -colorspaces <filter>`, `conva -displays`,
   `conva -formats`, `conva -configs`.

## Command shape

```
conva [options] -i INPUT [options] OUTPUT
```

You can put options before or after `-i`. OUTPUT is a file, a sequence pattern, or a directory.
A directory either ends with `/` or has no extension; for a directory, give `-f EXT` to choose
the format.

```bash
conva -i render.exr -ics acescg -ocs srgb_tx render.png                       # color space -> color space
conva -i plate.####.exr -ics acescg -display srgb -view "sdr 100" out/plate.####.jpg   # ACES output transform
conva -i A001.%06d.dpx -ics "ARRI LogC4" -ocs acescg -c dwaa:45 out/A001.%06d.exr
conva -i "tex/*.png" -ics srgb_tx -ocs acescg -f exr -d half tex_acescg/      # batch; quote the glob
conva -i graded.jpg -idisplay srgb -ocs acescg graded.exr                     # invert an output transform
conva -i IMG_0001.CR3 -ocs srgb_tx -s 2048x-1 -q 85 IMG_0001.webp             # camera raw -> web proxy
conva -config aces1.3 -i in.exr -ics acescg -display srgb out.png             # ACES 1.3 instead of 2.1
```

## Choosing color settings

- **Always pass `-ics`.** Without it, conva falls back to the config's file rules (EXR becomes
  ACES2065-1, everything else sRGB - Display) and prints a warning. If you don't know the input
  space, check the probe output and the file's origin, and ask the user if it is still unclear.
  Don't silently guess.
- **Scene-linear deliverables** (comp, CG, lighting): `-ocs acescg` (working space) or `-ocs aces2065_1`
  (archival/interchange, e.g. ACES EXR delivery).
- **Something meant to be looked at** (review JPGs, web proxies, thumbnails): use `-display` and `-view`,
  e.g. `-display srgb` (the default view is ACES 2.0 SDR 100 nits). This runs the ACES tone mapping.
- **Textures and plain color math**: `-ocs srgb_tx` is the sRGB transfer curve on Rec.709 primaries,
  with no tone mapping. Values above 1.0 clip.
- An `-ocs` that names a display color space (e.g. `"sRGB - Display"`) uses the config's default view
  transform. If you want tone mapping, it is clearer to use `-display`/`-view`.
- Names are matched loosely: `acescg`, `aces2065_1`, `acescct`, `lin_srgb`, `lin_rec2020`, `srgb_tx`,
  `"slog3 sgamut3 cine"`, `"ARRI LogC4"`, `"V-Log V-Gamut"`. Displays and views match on substrings
  (`srgb`, `"sdr 100"`, `"hdr 1000"`). For an unknown name, conva prints suggestions.
- `-config` picks the config: `aces2.1` (default), `aces2.0`, `aces1.3`, `cg`, a path or an `ocio://`
  URI. If `$OCIO` is set, it is used by default.
- Camera raw is always decoded to linear ACES2065-1, and `-ics` is ignored for raw files.

## Sequences

- Patterns: `####` (one `#` per digit), `%04d`, `@@@@`, `$F4`. If the input is a sequence, the output
  must be a pattern or a directory.
- Without `-frames`, conva converts every frame on disk. `-frames 1001-1100`, `1001-1100x2` and
  `1,5,10-20` select frames. Missing frames are skipped with a warning.
- `-renumber 1` makes the output start at frame 1 (gaps are kept).
- Frames run in parallel. Default is `min(8, cores)`; set it with `-j N`.

## Encoding and image options

| flag | meaning |
|---|---|
| `-d 8\|10\|12\|16\|half\|float` | bit depth. Defaults: exr half (float stays float), tiff matches the source, dpx 10, web formats 8 |
| `-q 1-100` | quality for jpg, webp, jxl, avif, heic |
| `-c` | compression: exr `zip` `piz` `dwaa:45` `dwab` `b44` `none`; tiff `lzw` `zip` |
| `-s` | resize `1920x1080`, `1920x-1`, `50%`, `0.5`; `-filter lanczos3` etc. |
| `-an` | drop alpha (jpg/hdr/ppm drop it automatically, over black) |
| `-layer NAME` | read `NAME.R/G/B/A` from a multi-layer EXR (otherwise only the main RGBA is read) |
| `-attrib K=V` | set output metadata |
| `-look L` | apply OCIO looks |

## Running non-interactively

- conva asks before overwriting. When stdin is not a terminal (the case for agents), it exits with
  an error instead. **Pass `-y` to overwrite or `-n` to skip existing files.** `-n` is also useful
  for resuming an interrupted sequence.
- For large or unfamiliar jobs, run `-dry_run` first. It prints every `input -> output` mapping and
  writes nothing.
- Use `-v error` to cut log output. Use `-v verbose` to see per-frame lines and a full traceback on
  unexpected errors.
- Exit code 0 means every frame was written. 1 means an error or at least one failed frame; failures
  are listed on stderr.

## After converting

Check one output with `conva -i <output>`. The resolution, channel count, bit depth and
`oiio:ColorSpace` should match what was asked for. Tell the user which conversion ran; conva prints
it on the `Color:` line.

## Limits

- Stills and image sequences only; no video containers.
- No ICC profile is embedded. P3 or Rec.2020 PNG/JPEG output looks untagged (sRGB) in browsers and
  most viewers.
- psd, dds, cineon, softimage, xbm and camera raw are read-only.
- JPEG 2000 can't be written for images smaller than about 32 px.
