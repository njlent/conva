"""Per-format encoding defaults: built-in values, overridable in a user defaults file.

The file is TOML, one table per format, for example::

    [exr]
    compression = "dwab"
    depth = "float"

    [jpg]
    quality = 92

Location: ``$CONVA_DEFAULTS`` if set, else ``~/.config/conva/defaults.toml``.
Command line flags (-c, -d, -q) always win over these.
"""

from __future__ import annotations

import os
import tomllib

from . import images

SETTINGS = ("compression", "depth", "quality")

BUILTIN: dict[str, dict[str, object]] = {
    "openexr": {"compression": "dwab", "depth": "float"},
}

# Short names used as table keys in the defaults file.
_SHORT = {"openexr": "exr", "tiff": "tif", "jpeg": "jpg", "jpeg2000": "jp2", "jpegxl": "jxl", "targa": "tga"}


def defaults_path() -> str:
    return os.environ.get("CONVA_DEFAULTS") or os.path.join(os.path.expanduser("~"), ".config", "conva", "defaults.toml")


def format_of(name: str) -> str:
    """Format for an extension (exr, jpg) or format name (openexr, jpeg)."""
    name = name.lower().lstrip(".")
    try:
        return images.format_for("x." + name)
    except images.ImageError:
        if name in images.OIIO_EXTS.values():
            return name
        raise images.ImageError(f"unknown format '{name}' (see: conva -formats)") from None


def short_name(fmt: str) -> str:
    return _SHORT.get(fmt) or fmt.removeprefix("pil:").lower()


def load_user() -> dict[str, dict[str, object]]:
    """User defaults keyed by format name. A broken file is an error, not silently ignored."""
    path = defaults_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"could not parse defaults file {path}: {e}") from None
    out: dict[str, dict[str, object]] = {}
    for key, table in data.items():
        if not isinstance(table, dict):
            raise ValueError(f"{path}: '{key}' must be a table like [exr]")
        fmt = format_of(key)
        for setting, value in table.items():
            out.setdefault(fmt, {})[setting] = validate(fmt, setting, value)
    return out


def effective(fmt: str) -> dict[str, object]:
    return {**BUILTIN.get(fmt, {}), **load_user().get(fmt, {})}


def validate(fmt: str, setting: str, value: object) -> object:
    if setting not in SETTINGS:
        raise ValueError(f"unknown setting '{setting}' (use one of: {', '.join(SETTINGS)})")
    if setting == "quality":
        try:
            q = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"quality must be a number 1-100, got '{value}'") from None
        if not 1 <= q <= 100:
            raise ValueError(f"quality must be 1-100, got {q}")
        return q
    value = str(value).strip().lower()
    if setting == "depth":
        if value != "auto" and value not in images.DEPTHS:
            raise ValueError(f"unknown bit depth '{value}' (use one of: 8, 10, 12, 16, half, float, auto)")
        return value
    return images.check_compression(fmt, value)


def _split_key(key: str) -> tuple[str, str]:
    fmt_name, sep, setting = key.partition(".")
    if not sep or not fmt_name or not setting:
        raise ValueError(f"expected FORMAT.SETTING, e.g. exr.compression, got '{key}'")
    return format_of(fmt_name), setting.lower()


def _save(user: dict[str, dict[str, object]]) -> str:
    path = defaults_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    lines = ["# conva encoding defaults. Edit by hand or with: conva -set_default exr.compression=dwab", ""]
    for fmt in sorted(user, key=short_name):
        if not user[fmt]:
            continue
        lines.append(f"[{short_name(fmt)}]")
        for setting in SETTINGS:
            if setting in user[fmt]:
                v = user[fmt][setting]
                lines.append(f"{setting} = {v}" if isinstance(v, int) else f'{setting} = "{v}"')
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return path


def set_values(pairs: list[str]) -> str:
    user = load_user()
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"-set_default expects FORMAT.SETTING=VALUE, got '{pair}'")
        fmt, setting = _split_key(key.strip())
        user.setdefault(fmt, {})[setting] = validate(fmt, setting, value.strip())
    return _save(user)


def unset_values(keys: list[str]) -> str:
    user = load_user()
    for key in keys:
        fmt, setting = _split_key(key.strip())
        if setting not in SETTINGS:
            raise ValueError(f"unknown setting '{setting}' (use one of: {', '.join(SETTINGS)})")
        user.get(fmt, {}).pop(setting, None)
    return _save(user)


def describe() -> list[str]:
    """Effective defaults, one line per format that has any."""
    user = load_user()
    fmts = sorted(set(BUILTIN) | set(user), key=short_name)
    rows = []
    for fmt in fmts:
        merged = {**BUILTIN.get(fmt, {}), **user.get(fmt, {})}
        parts = []
        for s in SETTINGS:
            if s in merged:
                src = "user" if s in user.get(fmt, {}) else "built-in"
                parts.append(f"{s}={merged[s]} ({src})")
        rows.append(f"  {short_name(fmt):<6} " + ", ".join(parts))
    return rows
