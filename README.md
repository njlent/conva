# conva

An image and image sequence converter with ffmpeg-style options. It is built on OpenImageIO and
OpenColorIO, and the ACES configs ship with OCIO, so it needs no config files.

```
conva -i render.exr -ics acescg -ocs srgb_tx out.png
```

## Install

```
uv tool install git+https://github.com/njlent/conva      # or, from a checkout: uv tool install .
```

To work on it: `uv sync`, then `uv run conva ...` and `uv run pytest`.

## Examples

```bash
# ACEScg EXR -> sRGB texture PNG
conva -i render.exr -ics acescg -ocs srgb_tx render.png

# Sequence through the ACES 2.0 SDR output transform (display + view)
conva -i plate.####.exr -ics acescg -display srgb -view "sdr 100" -q 92 jpg/plate.####.jpg

# Camera log -> ACEScg, a frame range, renumbered from 1
conva -i A001.%06d.dpx -ics "ARRI LogC4" -ocs acescg -frames 86400-86500 -renumber 1 -c dwaa:45 out.%04d.exr

# Batch: every PNG in a folder -> half-float ACEScg EXRs (quote the glob)
conva -i "textures/*.png" -ics srgb_tx -ocs acescg -f exr -d half textures_acescg/

# Display-referred image back to scene-linear (inverse output transform)
conva -i graded.jpg -idisplay srgb -ocs acescg graded.exr

# Camera raw (decoded to linear ACES2065-1), resized for the web
conva -i IMG_0001.CR3 -ocs srgb_tx -s 2048x-1 -q 85 IMG_0001.webp

# ACES 1.3 instead of the default ACES 2.1 config
conva -config aces1.3 -i plate.####.exr -ics acescg -display srgb out/plate.####.png

# File info / listings
conva -i plate.####.exr
conva -colorspaces sony
conva -displays
conva -formats
conva -configs
```

## Options

| option | |
|---|---|
| `-i INPUT` | file, sequence pattern or quoted glob |
| `OUTPUT` | file, sequence pattern, or directory (ends with `/`, or has no extension) |
| `-ics CS` / `-ocs CS` | input / output color space. Any name, alias or role in the config: `acescg`, `aces2065_1`, `acescct`, `srgb_tx`, `lin_srgb`, `"S-Log3 S-Gamut3.Cine"`, ... |
| `-display D` `-view V` | apply a display/view (output transform). Names are matched loosely: `srgb`, `"sdr 100"`. Either one alone uses the config default for the other |
| `-idisplay D` `-iview V` | input is display-referred: invert that display/view |
| `-look L` | apply OCIO looks |
| `-config CFG` | config path, `ocio://` URI, built-in name, or `aces2.1` `aces2.0` `aces1.3` `cg`. Default: `$OCIO`, else the ACES 2.1 studio config |
| `-frames R` | `1001-1100`, `1001-1100x2`, `1,5,10-20`. Without it, every frame on disk is converted |
| `-renumber N` | output frames start at N (gaps kept) |
| `-f EXT` | output format, for directory outputs (otherwise the extension decides) |
| `-d DEPTH` | `8` `10` `12` `16` `half` `float` |
| `-q Q` | quality 1-100 for jpg, webp, jxl, avif, heic |
| `-c C` | compression: `zip` `piz` `dwaa:45` `dwab` `b44` `none` (exr), `lzw` `zip` (tiff), ... |
| `-s SIZE` | resize: `1920x1080`, `1920x-1` (keep aspect), `50%`, `0.5`. `-filter` picks the filter |
| `-an` | drop alpha |
| `-layer NAME` | read `NAME.R/G/B/A` from a multi-layer EXR |
| `-attrib K=V` | set an output metadata attribute (repeatable) |
| `-dither` | dither 8-bit output |
| `-no_unpremult` | don't unpremultiply around the color transform |
| `-y` / `-n` | overwrite / skip existing outputs (without either flag, conva asks) |
| `-j N` | frames processed in parallel (default `min(8, cores)`) |
| `-v LEVEL` | `quiet` `error` `warning` `info` `verbose` |
| `-dry_run` | print the file mapping only |

Sequence tokens: `####` (one `#` per digit), `%04d`, `@@@@` and `$F4`.

## Formats

Run `conva -formats` for the full list. The main ones:

- **OpenImageIO**: exr, tiff, png, jpg, webp, jxl, jp2, dpx, hdr, tga, bmp, gif, sgi, iff, pnm/pfm, fits, rla, ico.
  These are read only: psd, dds, cineon, softimage.
- **Pillow**: avif, heic/heif, qoi, pcx, icns (read/write), xbm (read).
- **rawpy / LibRaw** (read only): cr2, cr3, nef, arw, dng, raf, orf, rw2, pef, iiq, 3fr and more.

## Behavior

- **Input color space**: give `-ics`. Without it, conva uses the config's file rules and prints a warning.
  In the ACES studio config those rules map EXR to ACES2065-1 and everything else to sRGB - Display.
- **Camera raw** files are decoded with camera white balance to linear ACES2065-1, and `-ics` is ignored for them.
- **Alpha**: colors are unpremultiplied before the transform and premultiplied again after it. Formats
  without alpha (jpg, hdr, ppm) get the image composited over black.
- **Default bit depth**: exr is half (float stays float), tiff matches the source, dpx is 10-bit, png is
  16-bit when the source is 16-bit int and 8-bit otherwise. Web formats are 8-bit.
- Output files get `oiio:ColorSpace` metadata. Most other metadata is copied over. ICC profiles are not
  embedded, so a P3 or Rec.2020 PNG/JPEG shows as untagged (sRGB) in browsers.
- The OCIO transform runs in row chunks across all cores. One 4K frame through the ACES 2.0 output
  transform takes under a second.
- Only RGB(A) or Y(A) channels are read. Other EXR layers are dropped unless you pick one with `-layer`.
