"""Job planning (single files, sequences, globs) and per-image processing."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field

import numpy as np
import OpenImageIO as oiio

from . import color, images, sequence
from .sequence import SeqPattern


class PlanError(Exception):
    pass


@dataclass
class Job:
    src: str
    dst: str
    frame: int | None = None


@dataclass
class Plan:
    jobs: list[Job]
    input_label: str
    output_label: str
    frames: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)


def _looks_like_dir(path: str) -> bool:
    if path.endswith(("/", "\\")) or os.path.isdir(path):
        return True
    return not os.path.splitext(path)[1] and sequence.parse_pattern(path) is None


def _swap_ext(name: str, ext: str | None) -> str:
    if not ext:
        return name
    return os.path.splitext(name)[0] + "." + ext.lstrip(".")


def plan(
    input_path: str,
    output_path: str,
    *,
    frames: str | None = None,
    renumber: int | None = None,
    out_ext: str | None = None,
) -> Plan:
    in_seq = sequence.parse_pattern(input_path)
    out_is_dir = _looks_like_dir(output_path)

    if in_seq is not None:
        found = in_seq.existing_frames()
        if frames:
            wanted = sequence.parse_frame_range(frames)
            have = set(found)
            missing = [f for f in wanted if f not in have]
            todo = [f for f in wanted if f in have]
        else:
            todo, missing = found, []
        if not todo:
            raise PlanError(f"no frames found for {in_seq.display()}" + (f" in range {frames}" if frames else ""))
        if out_is_dir:
            base = os.path.basename(in_seq.head) + "#" * in_seq.pad + in_seq.tail
            out_seq = sequence.parse_pattern(os.path.join(output_path, _swap_ext(base, out_ext)))
        else:
            out_seq = sequence.parse_pattern(output_path)
            if out_seq is None:
                raise PlanError(
                    "input is a sequence, so the output needs a frame pattern too "
                    "(e.g. out.####.png or out.%04d.png) or a directory"
                )
        assert out_seq is not None
        first = todo[0]
        jobs = []
        for f in todo:
            of = f if renumber is None else renumber + (f - first)
            jobs.append(Job(in_seq.path(f), out_seq.path(of), f))
        return Plan(jobs, in_seq.display(), out_seq.display(), todo, missing)

    if sequence.is_glob(input_path):
        files = sequence.expand_glob(input_path)
        if not files:
            raise PlanError(f"no files match {input_path}")
        if not out_is_dir:
            if len(files) == 1:
                return Plan([Job(files[0], output_path)], files[0], output_path)
            raise PlanError("input matches several files, so the output must be a directory (end it with /)")
        jobs = [Job(f, os.path.join(output_path, _swap_ext(os.path.basename(f), out_ext))) for f in files]
        return Plan(jobs, f"{input_path} ({len(files)} files)", output_path)

    if not os.path.isfile(input_path):
        raise PlanError(f"no such file: {input_path}")
    if out_is_dir:
        dst = os.path.join(output_path, _swap_ext(os.path.basename(input_path), out_ext))
    elif sequence.parse_pattern(output_path) is not None:
        raise PlanError(
            "output has a frame pattern but the input is a single file; "
            "use a pattern for the input too, e.g. -i plate.####.exr"
        )
    else:
        dst = output_path
    return Plan([Job(input_path, dst)], input_path, dst)


# ---------------------------------------------------------------------------------------- processing

_SIZE_RE = re.compile(r"^(-?\d+)[x:](-?\d+)$")


def parse_size(spec: str, w: int, h: int) -> tuple[int, int]:
    """``1920x1080``, ``1920x-1`` (keep aspect), ``50%`` or ``0.5``."""
    s = spec.strip().lower()
    if s.endswith("%"):
        f = float(s[:-1]) / 100.0
        return max(1, round(w * f)), max(1, round(h * f))
    m = _SIZE_RE.match(s)
    if m:
        nw, nh = int(m.group(1)), int(m.group(2))
        if nw <= 0 and nh <= 0:
            raise ValueError(f"invalid size '{spec}'")
        if nw <= 0:
            nw = max(1, round(w * nh / h))
        if nh <= 0:
            nh = max(1, round(h * nw / w))
        return nw, nh
    try:
        f = float(s)
    except ValueError:
        raise ValueError(f"invalid size '{spec}' (use WxH, Wx-1, 50% or 0.5)") from None
    return max(1, round(w * f)), max(1, round(h * f))


@dataclass
class Settings:
    config: object
    input_cs: str | None
    output_cs: str | None
    display: str | None
    view: str | None
    looks: str | None
    inv_display: str | None
    inv_view: str | None
    write: images.WriteOptions
    layer: str | None = None
    size: str | None = None
    resize_filter: str | None = None
    drop_alpha: bool = False
    unpremult: bool = True


class Converter:
    def __init__(self, settings: Settings):
        self.s = settings
        self._pipelines: dict[str, color.ColorPipeline] = {}
        self._lock = threading.Lock()

    def input_space_for(self, path: str) -> str | None:
        """Explicit -ics, the forced space of raw files, or the config's file rules."""
        if images.format_for(path) == "raw":
            return "ACES2065-1"
        if self.s.input_cs:
            return self.s.input_cs
        if self.s.inv_display or self.s.inv_view:
            return None
        return color.guess_input_colorspace(self.s.config, path)

    def pipeline_for(self, input_cs: str | None) -> color.ColorPipeline:
        key = input_cs or ""
        with self._lock:
            if key not in self._pipelines:
                self._pipelines[key], _ = color.build_pipeline(
                    self.s.config,
                    input_cs=input_cs,
                    output_cs=self.s.output_cs,
                    display=self.s.display,
                    view=self.s.view,
                    looks=self.s.looks,
                    inv_display=self.s.inv_display,
                    inv_view=self.s.inv_view,
                )
            return self._pipelines[key]

    def process(self, job: Job) -> None:
        img = images.read_image(job.src, self.s.layer)
        pipe = self.pipeline_for(self.input_space_for(job.src))

        a = img.alpha_index()
        if a is not None and (self.s.drop_alpha or self.s.write.fmt in images.NO_ALPHA):
            keep = [i for i in range(img.nchannels) if i != a]
            img.pixels = np.ascontiguousarray(img.pixels[:, :, keep])
            img.channels = [img.channels[i] for i in keep]
            a = None

        if self.s.size:
            self._resize(img)

        if not pipe.is_noop:
            self._apply_color(img, pipe)

        opts = self.s.write
        if opts.colorspace is None:
            opts = images.WriteOptions(**{**opts.__dict__, "colorspace": pipe.output_label})
        images.write_image(job.dst, img, opts)

    def _resize(self, img: images.Image) -> None:
        nw, nh = parse_size(self.s.size, img.width, img.height)
        if (nw, nh) == (img.width, img.height):
            return
        c = img.nchannels
        src = oiio.ImageBuf(oiio.ImageSpec(img.width, img.height, c, oiio.FLOAT))
        src.set_pixels(oiio.ROI(0, img.width, 0, img.height, 0, 1, 0, c), img.pixels)
        dst = oiio.ImageBufAlgo.resize(src, filtername=self.s.resize_filter or "", roi=oiio.ROI(0, nw, 0, nh, 0, 1, 0, c))
        if dst.has_error:
            raise images.ImageError(f"resize failed: {dst.geterror()}")
        img.pixels = np.ascontiguousarray(dst.get_pixels(oiio.FLOAT).reshape(nh, nw, c), dtype=np.float32)
        img.origin, img.full = (0, 0), None

    def _apply_color(self, img: images.Image, pipe: color.ColorPipeline) -> None:
        a = img.alpha_index()
        color_idx = [i for i in range(img.nchannels) if i != a]
        if len(color_idx) == 1:  # grayscale: expand to RGB
            y = img.pixels[:, :, color_idx[0] : color_idx[0] + 1]
            parts = [y, y, y] + ([img.pixels[:, :, a : a + 1]] if a is not None else [])
            img.pixels = np.ascontiguousarray(np.concatenate(parts, axis=2))
            img.channels = ["R", "G", "B"] + (["A"] if a is not None else [])
            a = 3 if a is not None else None
            color_idx = [0, 1, 2]
        rgb_idx = color_idx[:3]

        px = img.pixels
        if a is not None and self.s.unpremult:
            px = images.unpremultiplied(px, a)
        rgb = np.ascontiguousarray(px[:, :, rgb_idx], dtype=np.float32)
        pipe.apply(rgb)
        px[:, :, rgb_idx] = rgb
        if a is not None and self.s.unpremult:
            # alpha == 0 pixels were left as-is by unpremultiplied(), so leave them as-is here too
            alpha = px[:, :, a : a + 1]
            c = px[:, :, color_idx]
            px[:, :, color_idx] = np.where(alpha > 0, c * alpha, c)
        img.pixels = px
