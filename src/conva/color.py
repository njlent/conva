"""OpenColorIO config loading, name resolution and processor building."""

from __future__ import annotations

import difflib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import PyOpenColorIO as ocio

DEFAULT_CONFIG = "ocio://studio-config-latest"

CONFIG_ALIASES = {
    "aces": "ocio://studio-config-latest",
    "studio": "ocio://studio-config-latest",
    "cg": "ocio://cg-config-latest",
    "aces2": "ocio://studio-config-v5.0.0_aces-v2.1_ocio-v2.6",
    "aces2.1": "ocio://studio-config-v5.0.0_aces-v2.1_ocio-v2.6",
    "aces2.0": "ocio://studio-config-v4.0.0_aces-v2.0_ocio-v2.5",
    "aces1": "ocio://studio-config-v2.2.0_aces-v1.3_ocio-v2.4",
    "aces1.3": "ocio://studio-config-v2.2.0_aces-v1.3_ocio-v2.4",
    "cg-aces2": "ocio://cg-config-v5.0.0_aces-v2.1_ocio-v2.6",
    "cg-aces1.3": "ocio://cg-config-v2.2.0_aces-v1.3_ocio-v2.4",
}


class ColorError(Exception):
    pass


# OCIO releases the GIL while applying, so large images are processed in row chunks across threads.
_POOL = ThreadPoolExecutor(max_workers=os.cpu_count() or 1, thread_name_prefix="conva-ocio")
_CHUNK_PIXELS = 256 * 1024


def builtin_config_names() -> list[str]:
    return list(ocio.BuiltinConfigRegistry())


def resolve_config_uri(spec: str | None) -> str:
    if not spec:
        return os.environ.get("OCIO") or DEFAULT_CONFIG
    key = spec.lower()
    if key in CONFIG_ALIASES:
        return CONFIG_ALIASES[key]
    if spec in builtin_config_names():
        return "ocio://" + spec
    return spec


def load_config(spec: str | None) -> ocio.Config:
    uri = resolve_config_uri(spec)
    try:
        return ocio.Config.CreateFromFile(uri)
    except Exception as e:  # OCIO raises its own Exception type
        raise ColorError(f"could not load OCIO config '{uri}': {e}") from None


def _all_colorspace_names(config: ocio.Config) -> list[str]:
    names = []
    for cs in config.getColorSpaces():
        names.append(cs.getName())
        names.extend(cs.getAliases())
    return names


def resolve_colorspace(config: ocio.Config, name: str) -> str:
    """Return the canonical color space name for a name, alias or role."""
    cs = config.getColorSpace(name)
    if cs is not None:
        return cs.getName()
    # loose match: ignore case, spaces, dashes, underscores, dots
    norm = _norm(name)
    hits = {c.getName() for c in config.getColorSpaces() for n in [c.getName(), *c.getAliases()] if _norm(n) == norm}
    if len(hits) == 1:
        return hits.pop()
    by_lower = {n.lower(): n for n in _all_colorspace_names(config)}
    suggestions = [by_lower[s] for s in difflib.get_close_matches(name.lower(), list(by_lower), n=5, cutoff=0.5)]
    hint = f" Did you mean: {', '.join(repr(s) for s in suggestions)}?" if suggestions else ""
    raise ColorError(f"unknown color space '{name}'.{hint} (list them with: conva -colorspaces)")


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _pick(name: str, options: list[str], kind: str, list_hint: str) -> str:
    if name in options:
        return name
    for o in options:
        if o.lower() == name.lower():
            return o
    subs = [o for o in options if _norm(name) in _norm(o)]
    if len(subs) == 1:
        return subs[0]
    if len(subs) > 1:
        raise ColorError(f"{kind} '{name}' is ambiguous, matches: {', '.join(repr(s) for s in subs)}")
    raise ColorError(f"unknown {kind} '{name}'. Available: {', '.join(repr(o) for o in options)} ({list_hint})")


def resolve_display(config: ocio.Config, name: str | None) -> str:
    if not name:
        return config.getDefaultDisplay()
    return _pick(name, list(config.getDisplays()), "display", "conva -displays")


def resolve_view(config: ocio.Config, display: str, name: str | None) -> str:
    if not name:
        return config.getDefaultView(display)
    return _pick(name, list(config.getViews(display)), f"view for display '{display}'", "conva -displays")


def guess_input_colorspace(config: ocio.Config, path: str) -> str:
    """Use the config's file rules to guess the input space of a file."""
    name = config.getColorSpaceFromFilepath(path)
    return name[0] if isinstance(name, tuple) else name


@dataclass
class ColorPipeline:
    """A resolved conversion, ready to apply to pixel arrays."""

    config: ocio.Config
    description: str
    output_label: str  # color space name written into file metadata
    is_noop: bool
    _cpu: ocio.CPUProcessor | None = None

    def apply(self, rgb: np.ndarray) -> None:
        """Apply in place to a float32 C-contiguous (..., 3) array."""
        if self.is_noop or self._cpu is None:
            return
        rows = rgb.shape[0] if rgb.ndim == 3 else 1
        nchunks = min(rows, max(1, (rgb.size // 3) // _CHUNK_PIXELS))
        if nchunks <= 1:
            self._cpu.applyRGB(rgb)
            return
        step = -(-rows // nchunks)
        # row slices of a C-contiguous array are contiguous views, so OCIO writes straight into rgb
        list(_POOL.map(self._cpu.applyRGB, [rgb[i : i + step] for i in range(0, rows, step)]))


def build_pipeline(
    config: ocio.Config,
    *,
    input_cs: str | None,
    output_cs: str | None,
    display: str | None,
    view: str | None,
    looks: str | None,
    inv_display: str | None,
    inv_view: str | None,
    input_path: str | None = None,
) -> tuple[ColorPipeline, str]:
    """Build the pipeline. Returns (pipeline, resolved input label)."""
    group = ocio.GroupTransform()
    steps = []

    if inv_display or inv_view:
        if input_cs:
            raise ColorError("use either -ics or -idisplay/-iview for the input, not both")
        idisp = resolve_display(config, inv_display)
        iview = resolve_view(config, idisp, inv_view)
        src = config.getCanonicalName(ocio.ROLE_SCENE_LINEAR) or config.getColorSpaceNames()[0]
        group.appendTransform(
            ocio.DisplayViewTransform(src=src, display=idisp, view=iview, direction=ocio.TRANSFORM_DIR_INVERSE)
        )
        in_label = f"{idisp} / {iview} (inverse)"
        steps.append(in_label)
    else:
        if input_cs:
            src = resolve_colorspace(config, input_cs)
        elif input_path is not None:
            src = guess_input_colorspace(config, input_path)
        else:
            raise ColorError("no input color space given (-ics)")
        in_label = src

    if display or view:
        disp = resolve_display(config, display)
        v = resolve_view(config, disp, view)
        group.appendTransform(ocio.DisplayViewTransform(src=src, display=disp, view=v, looksBypass=False))
        if looks:
            # DisplayViewTransform only applies the view's own looks; add explicit ones before it.
            group.prependTransform(ocio.LookTransform(src=src, dst=src, looks=looks))
        out_label = config.getDisplayViewColorSpaceName(disp, v) or disp
        steps.append(f"{disp} / {v}")
        if looks:
            steps[-1] += f" (looks: {looks})"
    elif output_cs:
        dst = resolve_colorspace(config, output_cs)
        if looks:
            group.appendTransform(ocio.LookTransform(src=src, dst=dst, looks=looks))
        else:
            group.appendTransform(ocio.ColorSpaceTransform(src=src, dst=dst))
        out_label = dst
        steps.append(dst + (f" (looks: {looks})" if looks else ""))
    else:
        out_label = src
        if not (inv_display or inv_view):
            steps.append(f"{src} (unchanged)")

    is_noop = len(group) == 0
    cpu = None
    if not is_noop:
        try:
            proc = config.getProcessor(group)
        except Exception as e:
            raise ColorError(f"could not build color transform: {e}") from None
        is_noop = proc.isNoOp()
        cpu = proc.getOptimizedCPUProcessor(ocio.BIT_DEPTH_F32, ocio.BIT_DEPTH_F32, ocio.OPTIMIZATION_DEFAULT)

    desc = " -> ".join([in_label] + steps) if not (inv_display or inv_view) else " -> ".join(steps)
    return ColorPipeline(config, desc, out_label, is_noop, cpu), in_label

