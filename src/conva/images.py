"""Image reading and writing.

OpenImageIO handles most formats. Pillow (with pillow-heif) covers AVIF, HEIC, QOI and a few
others, and rawpy decodes camera raw files.

Pixels inside conva are always float32 arrays of shape (H, W, C), with *associated* (premultiplied)
alpha, which is what OpenImageIO gives you by default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import OpenImageIO as oiio

RAW_EXTS = {
    "3fr", "ari", "arw", "bay", "cr2", "cr3", "crw", "dcr", "dng", "erf", "fff", "iiq", "k25", "kdc",
    "mef", "mos", "mrw", "nef", "nrw", "orf", "pef", "raf", "raw", "rw2", "rwl", "sr2", "srf", "srw", "x3f",
}
# Formats routed through Pillow: ext -> (Pillow format name, can write)
PIL_FORMATS = {
    "avif": ("AVIF", True),
    "heic": ("HEIF", True),
    "heif": ("HEIF", True),
    "qoi": ("QOI", True),
    "pcx": ("PCX", True),
    "icns": ("ICNS", True),
    "xbm": ("XBM", False),
}

# Formats that can't store alpha.
NO_ALPHA = {"jpeg", "hdr", "pnm", "cineon"}

DEPTHS = {
    "8": ("uint8", None), "uint8": ("uint8", None), "u8": ("uint8", None),
    "10": ("uint16", 10), "uint10": ("uint16", 10),
    "12": ("uint16", 12), "uint12": ("uint16", 12),
    "16": ("uint16", None), "uint16": ("uint16", None), "u16": ("uint16", None),
    "half": ("half", None), "16f": ("half", None), "f16": ("half", None),
    "float": ("float", None), "32f": ("float", None), "f32": ("float", None), "32": ("float", None),
}


class ImageError(Exception):
    pass


@dataclass
class Image:
    pixels: np.ndarray  # float32 (H, W, C), associated alpha
    channels: list[str]
    format_name: str  # e.g. "openexr", "png", "avif", "raw"
    src_depth: str  # e.g. "uint8", "half"
    attribs: list[tuple[str, object, object]] = field(default_factory=list)  # (name, TypeDesc, value)
    origin: tuple[int, int] = (0, 0)
    full: tuple[int, int, int, int] | None = None  # full_x, full_y, full_w, full_h
    colorspace: str | None = None  # known/forced color space (raw files)
    bits: int | None = None

    @property
    def height(self) -> int:
        return self.pixels.shape[0]

    @property
    def width(self) -> int:
        return self.pixels.shape[1]

    @property
    def nchannels(self) -> int:
        return self.pixels.shape[2]

    def alpha_index(self) -> int | None:
        for i, c in enumerate(self.channels):
            if c.split(".")[-1].upper() == "A":
                return i
        return None


def ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


# ---------------------------------------------------------------------------------------- formats

def _oiio_extension_map() -> dict[str, str]:
    m = {}
    for entry in oiio.get_string_attribute("extension_list").split(";"):
        if ":" not in entry:
            continue
        fmt, exts = entry.split(":", 1)
        if fmt in ("null", "term"):
            continue
        for e in exts.split(","):
            m[e.lower()] = fmt
    return m


OIIO_EXTS = _oiio_extension_map()


def format_for(path: str, forced: str | None = None) -> str:
    """Name of the format used for a path: an OIIO format name, 'pil:<NAME>' or 'raw'."""
    e = (forced or ext_of(path)).lower().lstrip(".")
    if e in PIL_FORMATS:
        return "pil:" + PIL_FORMATS[e][0]
    if e in RAW_EXTS:
        return "raw"
    if e in OIIO_EXTS:
        return OIIO_EXTS[e]
    if forced and e in OIIO_EXTS.values():
        return e
    raise ImageError(f"unsupported image format '.{e}' (see: conva -formats)")


def oiio_can_write(fmt: str) -> bool:
    out = oiio.ImageOutput.create(fmt)
    if out is None:
        oiio.geterror()  # clear it, otherwise OIIO complains at exit
        return False
    return True


def list_formats() -> list[tuple[str, list[str], bool, bool, str]]:
    """(format, extensions, read, write, backend)"""
    by_fmt: dict[str, list[str]] = {}
    for e, f in OIIO_EXTS.items():
        by_fmt.setdefault(f, []).append(e)
    rows = [(f, sorted(exts), True, oiio_can_write(f), "oiio") for f, exts in sorted(by_fmt.items())]
    pil: dict[str, tuple[list[str], bool]] = {}
    for e, (name, w) in PIL_FORMATS.items():
        exts, _ = pil.get(name, ([], w))
        pil[name] = (exts + [e], w)
    rows += [(name.lower(), exts, True, w, "pillow") for name, (exts, w) in sorted(pil.items())]
    rows.append(("camera raw", sorted(RAW_EXTS), True, False, "rawpy"))
    return rows


# ---------------------------------------------------------------------------------------- reading

_SKIP_ATTRIB_PREFIXES = (
    "oiio:", "ICCProfile", "compression", "Compression", "chromaticities", "png:", "jpeg:", "webp:",
    "openexr:", "tiff:", "dpx:", "jpeg2000:", "jpegxl:", "heif:", "planarconfig", "PixelAspectRatio",
    "XResolution", "YResolution", "ResolutionUnit", "Orientation",
)


def _keep_attrib(name: str) -> bool:
    return not name.startswith(_SKIP_ATTRIB_PREFIXES)


def read_image(path: str, layer: str | None = None) -> Image:
    if not os.path.isfile(path):
        raise ImageError(f"no such file: {path}")
    fmt = format_for(path)
    if fmt == "raw":
        return _read_raw(path)
    if fmt.startswith("pil:"):
        return _read_pil(path, fmt)
    return _read_oiio(path, layer)


def _read_oiio(path: str, layer: str | None) -> Image:
    inp = oiio.ImageInput.open(path)
    if inp is None:
        raise ImageError(f"could not open {path}: {oiio.geterror()}")
    try:
        spec = inp.spec()
        names = list(spec.channelnames)
        sel = _select_channels(names, layer)
        px = inp.read_image(0, 0, 0, spec.nchannels, oiio.FLOAT)
        if px is None:
            raise ImageError(f"could not read {path}: {inp.geterror()}")
    finally:
        inp.close()
    px = np.asarray(px, dtype=np.float32)
    if px.ndim == 2:
        px = px[:, :, None]
    if px.ndim == 4:  # volume image, take first slice
        px = px[0]
    px = np.ascontiguousarray(px[:, :, sel])
    chans = [names[i].split(".")[-1] if layer else names[i] for i in sel]
    bits = spec.get_int_attribute("oiio:BitsPerSample", 0) or None
    return Image(
        pixels=px,
        channels=chans,
        format_name=inp.format_name(),
        src_depth=str(spec.format),
        attribs=[(p.name, p.type, p.value) for p in spec.extra_attribs if _keep_attrib(p.name)],
        origin=(spec.x, spec.y),
        full=(spec.full_x, spec.full_y, spec.full_width, spec.full_height),
        colorspace=None,
        bits=bits,
    )


def _select_channels(names: list[str], layer: str | None) -> list[int]:
    """Pick R,G,B(,A) (or Y(,A)) channels; optionally from a named EXR layer."""
    def find(*cands: str) -> int | None:
        for c in cands:
            for i, n in enumerate(names):
                if n.lower() == c.lower():
                    return i
        return None

    prefix = f"{layer}." if layer else ""
    r, g, b = find(prefix + "R", prefix + "red"), find(prefix + "G", prefix + "green"), find(prefix + "B", prefix + "blue")
    a = find(prefix + "A", prefix + "alpha")
    if r is not None and g is not None and b is not None:
        return [r, g, b] + ([a] if a is not None else [])
    y = find(prefix + "Y")
    if y is not None:
        return [y] + ([a] if a is not None else [])
    if layer:
        layers = sorted({n.rsplit(".", 1)[0] for n in names if "." in n})
        raise ImageError(f"layer '{layer}' not found. Layers in file: {', '.join(layers) or 'none'}")
    # Unknown naming: take first 4 / 3 / all channels
    return list(range(min(len(names), 4)))


def _read_pil(path: str, fmt: str) -> Image:
    from PIL import Image as PILImage

    if fmt == "pil:HEIF":
        import pillow_heif

        pillow_heif.register_heif_opener()
    with PILImage.open(path) as im:
        im.load()
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            arr = np.asarray(im, dtype=np.float32) / 65535.0
            chans, depth = ["Y"], "uint16"
            arr = arr[:, :, None]
        else:
            has_alpha = "A" in im.getbands() or (im.mode == "P" and "transparency" in im.info)
            if im.mode in ("L", "LA"):
                target = im.mode
            else:
                target = "RGBA" if has_alpha else "RGB"
            im2 = im.convert(target)
            arr = np.asarray(im2, dtype=np.float32) / 255.0
            if arr.ndim == 2:
                arr = arr[:, :, None]
            chans = {"L": ["Y"], "LA": ["Y", "A"], "RGB": ["R", "G", "B"], "RGBA": ["R", "G", "B", "A"]}[target]
            depth = "uint8"
    arr = np.ascontiguousarray(arr)
    img = Image(pixels=arr, channels=chans, format_name=fmt[4:].lower(), src_depth=depth)
    _premultiply(img)
    return img


def _read_raw(path: str) -> Image:
    import rawpy

    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(
            output_color=rawpy.ColorSpace.ACES,
            gamma=(1, 1),
            no_auto_bright=True,
            use_camera_wb=True,
            output_bps=16,
        )
    arr = np.ascontiguousarray(rgb.astype(np.float32) / 65535.0)
    return Image(pixels=arr, channels=["R", "G", "B"], format_name="raw", src_depth="uint16", colorspace="ACES2065-1")


# ---------------------------------------------------------------------------------------- alpha

def _premultiply(img: Image) -> None:
    a = img.alpha_index()
    if a is None:
        return
    alpha = img.pixels[:, :, a : a + 1]
    color = [i for i in range(img.nchannels) if i != a]
    img.pixels[:, :, color] *= alpha


def unpremultiplied(pixels: np.ndarray, alpha_idx: int) -> np.ndarray:
    out = pixels.copy()
    alpha = out[:, :, alpha_idx : alpha_idx + 1]
    color = [i for i in range(out.shape[2]) if i != alpha_idx]
    with np.errstate(divide="ignore", invalid="ignore"):
        c = out[:, :, color]
        out[:, :, color] = np.where(alpha > 0, c / np.where(alpha > 0, alpha, 1), c)
    return out


# ---------------------------------------------------------------------------------------- writing

@dataclass
class WriteOptions:
    fmt: str  # result of format_for()
    depth: str | None = None  # key of DEPTHS
    quality: int | None = None
    compression: str | None = None
    dither: bool = False
    colorspace: str | None = None
    attribs: list[tuple[str, str]] = field(default_factory=list)


def default_depth(fmt: str, src_depth: str) -> str:
    float_src = src_depth in ("half", "float", "double")
    hi_src = src_depth in ("uint16", "int16", "uint32", "int32") or float_src
    if fmt == "openexr":
        return "float" if src_depth in ("float", "double") else "half"
    if fmt in ("hdr", "fits", "zfile"):
        return "float"
    if fmt == "tiff":
        return "float" if float_src else ("uint16" if hi_src else "uint8")
    if fmt == "dpx":
        return "10"
    if fmt in ("png", "sgi", "iff", "rla", "jpeg2000", "pnm") and src_depth in ("uint16", "int16"):
        return "uint16"
    return "uint8"


def write_image(path: str, img: Image, opts: WriteOptions) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if opts.fmt == "raw":
        raise ImageError("camera raw formats are read-only")
    if opts.fmt.startswith("pil:"):
        _write_pil(path, img, opts)
    else:
        _write_oiio(path, img, opts)


def _write_oiio(path: str, img: Image, opts: WriteOptions) -> None:
    depth_key = opts.depth or default_depth(opts.fmt, img.src_depth)
    if depth_key not in DEPTHS:
        raise ImageError(f"unknown bit depth '{depth_key}' (use one of: 8, 10, 12, 16, half, float)")
    dtype_name, bits = DEPTHS[depth_key]
    dtype = {"uint8": oiio.UINT8, "uint16": oiio.UINT16, "half": oiio.HALF, "float": oiio.FLOAT}[dtype_name]

    h, w, c = img.pixels.shape
    spec = oiio.ImageSpec(w, h, c, oiio.FLOAT)
    spec.channelnames = tuple(img.channels)
    a = img.alpha_index()
    spec.alpha_channel = a if a is not None else -1
    if img.full is not None:
        spec.x, spec.y = img.origin
        spec.full_x, spec.full_y, spec.full_width, spec.full_height = img.full
    for name, typ, value in img.attribs:
        try:
            spec.attribute(name, typ, value)
        except Exception:
            pass
    if bits:
        spec.attribute("oiio:BitsPerSample", bits)
    if opts.colorspace:
        spec.attribute("oiio:ColorSpace", opts.colorspace)
    if opts.dither and dtype_name == "uint8":
        spec.attribute("oiio:dither", 1)
    if opts.compression:
        spec.attribute("compression", opts.compression)
    elif opts.quality is not None and opts.fmt in ("jpeg", "webp", "jpegxl", "heif"):
        spec.attribute("compression", f"{opts.fmt}:{opts.quality}")
    if opts.quality is not None:
        spec.attribute("CompressionQuality", opts.quality)
    for k, v in opts.attribs:
        spec.attribute(k, _coerce(v))

    buf = oiio.ImageBuf(spec)
    buf.set_pixels(oiio.ROI(spec.x, spec.x + w, spec.y, spec.y + h, 0, 1, 0, c), img.pixels)
    if not buf.write(path, dtype, fileformat=opts.fmt):
        raise ImageError(f"could not write {path}: {buf.geterror() or oiio.geterror()}")


def _coerce(v: str) -> object:
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def _write_pil(path: str, img: Image, opts: WriteOptions) -> None:
    from PIL import Image as PILImage

    name = opts.fmt[4:]
    ext = ext_of(path)
    if ext in PIL_FORMATS and not PIL_FORMATS[ext][1]:
        raise ImageError(f".{ext} is read-only")
    if name == "HEIF":
        import pillow_heif

        pillow_heif.register_heif_opener()
    a = img.alpha_index()
    px = unpremultiplied(img.pixels, a) if a is not None else img.pixels
    arr = (np.clip(px, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[arr.shape[2]]
    pim = PILImage.fromarray(arr[:, :, 0] if mode == "L" else arr, mode)
    kwargs = {}
    if opts.quality is not None and name in ("AVIF", "HEIF"):
        kwargs["quality"] = opts.quality
    pim.save(path, format=name, **kwargs)
