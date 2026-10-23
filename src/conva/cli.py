"""ffmpeg-style command line: ``conva -i in.exr -ics acescg -ocs srgb_tx out.png``."""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import __version__, color, convert, images, sequence

LEVELS = {"quiet": 0, "error": 1, "warning": 2, "info": 3, "verbose": 4}
_level = LEVELS["info"]


def log(level: str, msg: str) -> None:
    if LEVELS[level] <= _level:
        prefix = {"error": "error: ", "warning": "warning: "}.get(level, "")
        print(prefix + msg, file=sys.stderr, flush=True)


EPILOG = """\
examples:
  conva -i render.exr -ics acescg -ocs srgb_tx out.png
  conva -i plate.####.exr -ics acescg -display srgb -view "sdr 100" out/plate.####.jpg
  conva -i shot.%04d.dpx -ics "ARRI LogC4" -ocs acescg -frames 1001-1100 shot.%04d.exr
  conva -i "textures/*.png" -ics srgb_tx -ocs acescg -f exr -d half textures_acescg/
  conva -i photo.jpg -idisplay srgb -ocs acescg photo.exr     (inverse ACES output transform)
  conva -i IMG_0001.CR3 -ocs srgb_tx -s 1920x-1 -q 90 IMG_0001.webp
  conva -i in.exr                                              (show file info)
  conva -colorspaces sony | conva -displays | conva -formats | conva -configs
  conva -install_skill                                         (agent skill for Claude Code + Codex)

sequences: patterns ####, %04d, @@@@ and $F4 are supported for input and output.
config:    default is $OCIO if set, else the built-in ACES studio config (ACES 2.1).
           -config accepts a path, an ocio:// URI, a built-in name or: aces2.1, aces2.0, aces1.3, cg.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="conva",
        usage="conva [options] -i INPUT [options] OUTPUT",
        description="Convert images and image sequences between formats and color spaces (OCIO / ACES).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        add_help=False,
    )
    p.add_argument("output", nargs="?", help="output file, sequence pattern or directory")

    g = p.add_argument_group("input / output")
    g.add_argument("-i", dest="input", metavar="INPUT", help="input file, sequence pattern (####, %%04d) or glob")
    g.add_argument("-frames", metavar="RANGE", help="frames to convert: 1001-1100, 1001-1100x2, 1,5,10-20")
    g.add_argument("-renumber", "-start_number", type=int, metavar="N", help="renumber output frames starting at N")
    g.add_argument("-f", dest="format", metavar="EXT", help="output format/extension (needed for directory outputs)")
    g.add_argument("-y", action="store_true", help="overwrite existing outputs without asking")
    g.add_argument("-n", action="store_true", help="never overwrite; skip existing outputs")

    g = p.add_argument_group("color")
    g.add_argument("-config", metavar="CFG", help="OCIO config: path, ocio:// URI, built-in name or aces2.1/aces2.0/aces1.3/cg")
    g.add_argument("-ics", "-input_colorspace", dest="ics", metavar="CS", help="input color space (name or alias)")
    g.add_argument("-ocs", "-output_colorspace", dest="ocs", metavar="CS", help="output color space (name or alias)")
    g.add_argument("-display", metavar="D", help="output display (applies display/view transform)")
    g.add_argument("-view", metavar="V", help="output view, e.g. 'ACES 2.0 - SDR 100 nits (Rec.709)'")
    g.add_argument("-look", metavar="LOOKS", help="look(s) to apply, e.g. '+mylook' or 'a,b'")
    g.add_argument("-idisplay", metavar="D", help="input is display-referred: invert this display/view")
    g.add_argument("-iview", metavar="V", help="view to invert with -idisplay")
    g.add_argument("-no_unpremult", action="store_true", help="don't unpremultiply alpha around the color transform")

    g = p.add_argument_group("encoding")
    g.add_argument("-d", "-depth", dest="depth", metavar="DEPTH", help="bit depth: 8, 10, 12, 16, half, float")
    g.add_argument("-q", "-quality", dest="quality", type=int, metavar="Q", help="quality 1-100 (jpg, webp, avif, heic, jxl)")
    g.add_argument("-c", "-compression", dest="compression", metavar="C", help="compression, e.g. zip, piz, dwaa:45, lzw, none")
    g.add_argument("-dither", action="store_true", help="dither when writing 8-bit")
    g.add_argument("-attrib", action="append", default=[], metavar="K=V", help="set an output metadata attribute (repeatable)")
    g.add_argument("-an", "-noalpha", dest="drop_alpha", action="store_true", help="drop the alpha channel")
    g.add_argument("-layer", metavar="NAME", help="EXR layer to read (e.g. diffuse for diffuse.R/G/B)")

    g = p.add_argument_group("image")
    g.add_argument("-s", "-size", dest="size", metavar="SIZE", help="resize: 1920x1080, 1920x-1, 50%%, 0.5")
    g.add_argument("-filter", metavar="F", help="resize filter: lanczos3, blackman-harris, gaussian, box, ...")

    g = p.add_argument_group("general")
    g.add_argument("-j", "-threads", dest="jobs", type=int, metavar="N", help="frames to process in parallel (default: min(8, cores))")
    g.add_argument("-v", "-loglevel", dest="loglevel", choices=list(LEVELS), default="info", help="log level")
    g.add_argument("-dry_run", action="store_true", help="show what would be done, write nothing")
    g.add_argument("-h", "-help", "--help", action="help", help="show this help")
    g.add_argument("-version", "--version", action="version", version=f"conva {__version__}")

    g = p.add_argument_group("listing")
    g.add_argument("-colorspaces", nargs="?", const="", metavar="FILTER", help="list color spaces (optional filter)")
    g.add_argument("-displays", action="store_true", help="list displays and views")
    g.add_argument("-looks", action="store_true", help="list looks")
    g.add_argument("-formats", action="store_true", help="list supported file formats")
    g.add_argument("-configs", action="store_true", help="list built-in OCIO configs")
    g.add_argument("-install_skill", nargs="?", const="all", choices=["claude", "codex", "all"],
                   help="install the conva agent skill for Claude Code and/or Codex (default: all)")
    return p


# ---------------------------------------------------------------------------------------- listing

def list_colorspaces(cfg, filt: str) -> None:
    f = filt.lower()
    print(f"color spaces in {cfg.getName()}:")
    for cs in cfg.getColorSpaces():
        names = [cs.getName(), *cs.getAliases()]
        if f and not any(f in n.lower() for n in names + [cs.getFamily()]):
            continue
        aliases = ", ".join(list(cs.getAliases())[:3])
        print(f"  {cs.getName():<34} {cs.getFamily():<20} {aliases}")
    for nt in cfg.getNamedTransforms():
        if not f or f in nt.getName().lower():
            print(f"  {nt.getName():<34} {'named transform':<20}")


def list_displays(cfg) -> None:
    print(f"displays in {cfg.getName()} (default first):")
    default = cfg.getDefaultDisplay()
    for d in [default] + [d for d in cfg.getDisplays() if d != default]:
        print(f"  {d}")
        dv = cfg.getDefaultView(d)
        for v in cfg.getViews(d):
            print(f"      {'*' if v == dv else ' '} {v}")


def list_looks(cfg) -> None:
    looks = list(cfg.getLookNames())
    print("looks:" if looks else "no looks in this config")
    for name in looks:
        print(f"  {name}")


def list_formats() -> None:
    print(" R W  format       extensions")
    for fmt, exts, r, w, backend in images.list_formats():
        flags = f" {'R' if r else '-'} {'W' if w else '-'}"
        shown = ", ".join(exts[:12]) + (", ..." if len(exts) > 12 else "")
        print(f"{flags}  {fmt:<12} {shown}" + ("" if backend == "oiio" else f"   [{backend}]"))


def list_configs() -> None:
    reg = color.ocio.BuiltinConfigRegistry()
    print("built-in configs (use with -config):")
    for name in reg:
        print(f"  {name}")
    print("shortcuts: " + ", ".join(f"{k}" for k in color.CONFIG_ALIASES))


def install_skill(target: str) -> int:
    from importlib.resources import files

    text = files("conva").joinpath("skill/SKILL.md").read_text(encoding="utf-8")
    homes = {
        "claude": os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
        "codex": os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
    }
    for name in (["claude", "codex"] if target == "all" else [target]):
        path = os.path.join(homes[name], "skills", "conva", "SKILL.md")
        existed = os.path.exists(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"{'updated' if existed else 'installed'} {name} skill: {path}")
    return 0


# ---------------------------------------------------------------------------------------- probe

def probe(path: str, cfg) -> int:
    seq = sequence.parse_pattern(path)
    target = path
    if seq is not None:
        frames = seq.existing_frames()
        if not frames:
            log("error", f"no frames found for {seq.display()}")
            return 1
        print(f"Sequence: {seq.display()}  frames {sequence.summarize_frames(frames)} ({len(frames)} files)")
        target = seq.path(frames[0])
    try:
        fmt = images.format_for(target)
    except images.ImageError as e:
        log("error", str(e))
        return 1
    if fmt == "raw" or fmt.startswith("pil:"):
        img = images.read_image(target)
        print(f"Input: {target}\n  format: {img.format_name}  {img.width}x{img.height}  {img.nchannels} ch "
              f"({','.join(img.channels)})  {img.src_depth}")
    else:
        import OpenImageIO as oiio

        inp = oiio.ImageInput.open(target)
        if inp is None:
            log("error", f"could not open {target}: {oiio.geterror()}")
            return 1
        sub = 0
        while inp.seek_subimage(sub, 0):
            spec = inp.spec()
            label = f"  part {sub}:" if sub else f"Input: {target}\n "
            print(f"{label} format: {inp.format_name()}  {spec.width}x{spec.height}  {spec.nchannels} ch "
                  f"({','.join(spec.channelnames)})  {spec.format}")
            if (spec.x, spec.y, spec.full_width, spec.full_height) != (0, 0, spec.width, spec.height):
                print(f"  data window: {spec.x},{spec.y} {spec.width}x{spec.height}  "
                      f"display window: {spec.full_x},{spec.full_y} {spec.full_width}x{spec.full_height}")
            for p in spec.extra_attribs:
                if p.name in ("oiio:ColorSpace", "oiio:BitsPerSample", "compression", "ICCProfile") or p.name.startswith(("smpte:", "FramesPerSecond")):
                    val = f"<{len(p.value)} bytes>" if p.name == "ICCProfile" else p.value
                    print(f"  {p.name}: {val}")
            sub += 1
        inp.close()
    print(f"  config file rule guess: {color.guess_input_colorspace(cfg, target)}")
    return 0


# ---------------------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    global _level
    parser = build_parser()
    args = parser.parse_args(argv)
    _level = LEVELS[args.loglevel]

    try:
        return _run(parser, args)
    except (color.ColorError, images.ImageError, convert.PlanError, ValueError) as e:
        log("error", str(e))
        return 1
    except KeyboardInterrupt:
        log("error", "interrupted")
        return 130
    except Exception as e:  # unexpected: short message, traceback with -v verbose
        if _level >= LEVELS["verbose"]:
            raise
        log("error", f"{type(e).__name__}: {e} (run with -v verbose for a traceback)")
        return 1


def _run(parser: argparse.ArgumentParser, args) -> int:
    if args.install_skill:
        return install_skill(args.install_skill)
    listing = args.colorspaces is not None or args.displays or args.looks or args.formats or args.configs
    if args.formats:
        list_formats()
    if args.configs:
        list_configs()
    if args.colorspaces is not None or args.displays or args.looks:
        cfg = color.load_config(args.config)
        if args.colorspaces is not None:
            list_colorspaces(cfg, args.colorspaces)
        if args.displays:
            list_displays(cfg)
        if args.looks:
            list_looks(cfg)
    if listing and not args.input:
        return 0

    if not args.input:
        parser.print_usage(sys.stderr)
        log("error", "no input given (-i). See conva -h")
        return 1

    cfg = color.load_config(args.config)
    if not args.output:
        return probe(args.input, cfg)

    if args.y and args.n:
        raise ValueError("-y and -n are mutually exclusive")
    if args.depth and args.depth.lower() not in images.DEPTHS:
        raise ValueError(f"unknown bit depth '{args.depth}' (use one of: 8, 10, 12, 16, half, float)")

    pl = convert.plan(args.input, args.output, frames=args.frames, renumber=args.renumber, out_ext=args.format)
    fmt = images.format_for(pl.jobs[0].dst, args.format)
    if fmt == "raw":
        raise ValueError("camera raw formats can only be read")
    if not fmt.startswith("pil:") and not images.oiio_can_write(fmt):
        raise ValueError(f"format '{fmt}' can only be read (see conva -formats)")

    attribs = []
    for kv in args.attrib:
        if "=" not in kv:
            raise ValueError(f"-attrib expects KEY=VALUE, got '{kv}'")
        attribs.append(tuple(kv.split("=", 1)))

    settings = convert.Settings(
        config=cfg,
        input_cs=args.ics,
        output_cs=args.ocs,
        display=args.display,
        view=args.view,
        looks=args.look,
        inv_display=args.idisplay,
        inv_view=args.iview,
        write=images.WriteOptions(
            fmt=fmt,
            depth=args.depth.lower() if args.depth else None,
            quality=args.quality,
            compression=args.compression,
            dither=args.dither,
            attribs=attribs,
        ),
        layer=args.layer,
        size=args.size,
        resize_filter=args.filter,
        drop_alpha=args.drop_alpha,
        unpremult=not args.no_unpremult,
    )
    conv = convert.Converter(settings)

    # Validate the color settings up front (and report the conversion).
    first_src = pl.jobs[0].src
    in_cs = conv.input_space_for(first_src)
    pipe = conv.pipeline_for(in_cs)
    if images.format_for(first_src) == "raw" and args.ics:
        log("warning", "camera raw is decoded to ACES2065-1; ignoring -ics")
    elif not args.ics and not (args.idisplay or args.iview) and images.format_for(first_src) != "raw":
        log("warning", f"no -ics given, using the config's file rule guess: '{in_cs}'")

    depth = args.depth or ("8" if fmt.startswith("pil:") else "auto depth")
    log("info", f"Input:  {pl.input_label}" + (f"  [{sequence.summarize_frames(pl.frames)}]" if pl.frames else ""))
    log("info", f"Color:  {pipe.description}   ({cfg.getName()})")
    log("info", f"Output: {pl.output_label}  ({fmt.removeprefix('pil:').lower()}"
        + (f", {depth}" if depth else "") + (f", q={args.quality}" if args.quality else "")
        + (f", {args.compression}" if args.compression else "") + ")")
    for f in pl.missing:
        log("warning", f"frame {f} is missing, skipping")

    jobs = _filter_existing(pl.jobs, args)
    if jobs is None:
        return 1
    if args.dry_run:
        for j in jobs:
            print(f"{j.src} -> {j.dst}")
        return 0
    if not jobs:
        log("info", "nothing to do")
        return 0

    return _execute(conv, jobs, args.jobs)


def _filter_existing(jobs: list[convert.Job], args) -> list[convert.Job] | None:
    existing = [j for j in jobs if os.path.exists(j.dst)]
    if not existing or args.y:
        return jobs
    if args.n:
        log("info", f"skipping {len(existing)} existing output(s)")
        return [j for j in jobs if not os.path.exists(j.dst)]
    what = f"'{existing[0].dst}'" if len(jobs) == 1 else f"{len(existing)} of {len(jobs)} output files"
    if not sys.stdin.isatty():
        log("error", f"{what} already exist(s). Use -y to overwrite or -n to skip")
        return None
    ans = input(f"{what} already exist(s). Overwrite? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        log("error", "not overwriting - exiting")
        return None
    return jobs


def _execute(conv: convert.Converter, jobs: list[convert.Job], workers: int | None) -> int:
    n = len(jobs)
    workers = max(1, workers or min(8, os.cpu_count() or 1))
    workers = min(workers, n)
    tty = sys.stderr.isatty()
    t0 = time.perf_counter()
    done = failed = 0

    def report(job: convert.Job, err: Exception | None) -> None:
        nonlocal done, failed
        done += 1
        if err is not None:
            failed += 1
            if tty and _level >= LEVELS["info"]:
                print(file=sys.stderr)
            log("error", f"{job.src}: {err}")
        log("verbose", f"{job.src} -> {job.dst}")
        if n > 1 and tty and _level == LEVELS["info"]:
            el = time.perf_counter() - t0
            print(f"\rframe={done:>6}/{n}  fps={done / el:6.2f}  time={el:6.1f}s", end="", file=sys.stderr, flush=True)

    if workers == 1:
        for job in jobs:
            try:
                conv.process(job)
                report(job, None)
            except Exception as e:  # keep going, report at the end
                report(job, e)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(conv.process, j): j for j in jobs}
            for fut in as_completed(futs):
                report(futs[fut], fut.exception())
    if n > 1 and tty and _level == LEVELS["info"]:
        print(file=sys.stderr)

    el = time.perf_counter() - t0
    log("info", f"done: {done - failed}/{n} written in {el:.1f}s" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0
