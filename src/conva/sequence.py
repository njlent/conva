"""Frame sequence patterns: ``%04d``, ``####``, ``@@@@`` and ``$F4``."""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

# Order matters: the first alternative that matches wins.
_TOKEN_RE = re.compile(r"%0?(\d*)d|(#+)|(@+)|\$F(\d*)")
_GLOB_CHARS = set("*?[")


@dataclass(frozen=True)
class SeqPattern:
    head: str  # everything before the frame number (including directory)
    pad: int
    tail: str  # everything after the frame number

    def path(self, frame: int) -> str:
        return f"{self.head}{frame:0{self.pad}d}{self.tail}"

    def display(self) -> str:
        return f"{self.head}{'#' * self.pad}{self.tail}"

    def existing_frames(self) -> list[int]:
        """Frames of this pattern that exist on disk, sorted."""
        directory, base_head = os.path.split(self.head)
        directory = directory or "."
        if not os.path.isdir(directory):
            return []
        digits = rf"-?\d{{{self.pad},}}" if self.pad > 1 else r"-?\d+"
        rx = re.compile(rf"^{re.escape(base_head)}({digits}){re.escape(self.tail)}$", re.IGNORECASE)
        frames = set()
        for name in os.listdir(directory):
            m = rx.match(name)
            if not m:
                continue
            num = m.group(1)
            # With padding N, a number shorter than N can't match; longer is fine only if it has no leading zero.
            if len(num.lstrip("-")) > self.pad and num.lstrip("-").startswith("0"):
                continue
            frames.add(int(num))
        return sorted(frames)


def parse_pattern(path: str) -> SeqPattern | None:
    """Return a SeqPattern if the file name contains a frame token, else None."""
    directory, base = os.path.split(path)
    matches = list(_TOKEN_RE.finditer(base))
    if not matches:
        return None
    m = matches[-1]  # last token in the file name is the frame number
    if m.group(1) is not None:
        pad = int(m.group(1) or 1)
    elif m.group(2):
        pad = len(m.group(2))
    elif m.group(3):
        pad = len(m.group(3))
    else:
        pad = int(m.group(4) or 1)
    head = os.path.join(directory, base[: m.start()]) if directory else base[: m.start()]
    return SeqPattern(head=head, pad=pad, tail=base[m.end():])


def is_glob(path: str) -> bool:
    return any(c in _GLOB_CHARS for c in os.path.basename(path))


def expand_glob(path: str) -> list[str]:
    return sorted(p for p in glob.glob(path) if os.path.isfile(p))


_RANGE_RE = re.compile(r"^(-?\d+)(?:(?:-|:)(-?\d+)(?:[x:](\d+))?)?$")


def parse_frame_range(spec: str) -> list[int]:
    """Parse ``1001-1100``, ``1001-1100x2``, ``1,5,10-20`` or ``1001:1100:2``."""
    frames: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        m = _RANGE_RE.match(part)
        if not m:
            raise ValueError(f"invalid frame range '{part}' (expected e.g. 1001-1100, 1001-1100x2, 1,5,10-20)")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        step = int(m.group(3) or 1)
        if step < 1:
            raise ValueError(f"invalid frame step in '{part}'")
        if end < start:
            raise ValueError(f"frame range end is before start in '{part}'")
        frames.extend(range(start, end + 1, step))
    # de-duplicate, keep order
    return list(dict.fromkeys(frames))


def summarize_frames(frames: list[int]) -> str:
    """Compact human readable frame list: ``1001-1010, 1012, 1020-1030x2``."""
    if not frames:
        return "none"
    out = []
    i = 0
    fs = sorted(frames)
    while i < len(fs):
        j = i
        step = fs[i + 1] - fs[i] if i + 1 < len(fs) else 1
        while j + 1 < len(fs) and fs[j + 1] - fs[j] == step:
            j += 1
        if j == i:
            out.append(str(fs[i]))
        elif j == i + 1 and step != 1:
            out.append(str(fs[i]))
            j = i
        else:
            out.append(f"{fs[i]}-{fs[j]}" + (f"x{step}" if step != 1 else ""))
        i = j + 1
    return ", ".join(out)
