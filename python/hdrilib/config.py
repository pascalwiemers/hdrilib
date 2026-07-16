"""Persistent, version-independent settings for HDRI Library."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping


DEFAULT_EXTENSIONS = (
    ".rat",
    ".exr",
    ".hdr",
    ".tex",
    ".tx",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
)

SCHEMA_VERSION = 2
DEFAULT_THUMBNAIL_WORKERS = min(8, os.cpu_count() or 1)
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_CONFIG: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "roots": [],
    "location_ui_mode": "sidebar",
    "enabled_extensions": list(DEFAULT_EXTENSIONS),
    "quick_filter_extensions": list(DEFAULT_EXTENSIONS),
    "thumbnail_size": 256,
    "thumbnail_workers": DEFAULT_THUMBNAIL_WORKERS,
    "include_subfolders": False,
    "last_folder": "",
    "search_text": "",
}


def config_dir() -> Path:
    """Return the application data directory.

    ``HDRILIB_CONFIG_DIR`` is primarily useful for tests and managed deployments.
    The normal location intentionally does not depend on a Houdini version.
    """

    override = os.environ.get("HDRILIB_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".houdini_hdrilib"


def config_path() -> Path:
    return config_dir() / "config.json"


def thumbs_dir() -> Path:
    return config_dir() / "thumbs"


def _normalise_extension(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if not value:
        return None
    return value if value.startswith(".") else "." + value


def _normalise_root(value: object) -> dict[str, str] | None:
    """Return one strict schema-v2 root entry, accepting a v1 path string."""

    if isinstance(value, (str, os.PathLike)):
        raw_path = os.fspath(value)
        label = ""
        color = ""
    elif isinstance(value, Mapping):
        raw_path = value.get("path")
        if not isinstance(raw_path, (str, os.PathLike)):
            return None
        raw_label = value.get("label", "")
        raw_color = value.get("color", "")
        label = raw_label.strip() if isinstance(raw_label, str) else ""
        color = (
            raw_color.lower()
            if isinstance(raw_color, str) and _COLOR_RE.match(raw_color)
            else ""
        )
    else:
        return None

    if not raw_path or not os.fspath(raw_path).strip():
        return None
    return {
        "path": os.path.abspath(os.path.expanduser(os.fspath(raw_path))),
        "label": label,
        "color": color,
    }


def _normalise_extensions(values: object, fallback: list[str]) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return list(fallback)
    clean = []
    for value in values:
        extension = _normalise_extension(value)
        if extension and extension in DEFAULT_EXTENSIONS and extension not in clean:
            clean.append(extension)
    return clean


def normalise_config(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Migrate, validate, and return only the current schema's known fields.

    Version-1 root strings are accepted deliberately. All other malformed values
    are discarded or replaced with bounded defaults, and unknown keys never leak
    into the saved representation.
    """

    result = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(data, Mapping):
        return result

    roots = data.get("roots")
    if isinstance(roots, (list, tuple)):
        clean_roots = []
        seen_paths = set()
        for root in roots:
            clean_root = _normalise_root(root)
            if clean_root and clean_root["path"] not in seen_paths:
                seen_paths.add(clean_root["path"])
                clean_roots.append(clean_root)
        result["roots"] = clean_roots

    enabled = _normalise_extensions(
        data.get("enabled_extensions"), result["enabled_extensions"]
    )
    result["enabled_extensions"] = enabled
    quick = _normalise_extensions(data.get("quick_filter_extensions"), enabled)
    result["quick_filter_extensions"] = [value for value in quick if value in enabled]

    mode = data.get("location_ui_mode")
    if mode in ("sidebar", "dropdown"):
        result["location_ui_mode"] = mode

    size = data.get("thumbnail_size")
    if isinstance(size, int) and not isinstance(size, bool):
        result["thumbnail_size"] = max(64, min(1024, size))

    workers = data.get("thumbnail_workers")
    if isinstance(workers, int) and not isinstance(workers, bool):
        result["thumbnail_workers"] = max(1, min(64, workers))

    include_subfolders = data.get("include_subfolders")
    if isinstance(include_subfolders, bool):
        result["include_subfolders"] = include_subfolders
    for key in ("last_folder", "search_text"):
        value = data.get(key)
        if isinstance(value, str):
            result[key] = value
    return result


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load settings, returning defaults for a missing or invalid file."""

    target = Path(path).expanduser() if path else config_path()
    try:
        with target.open("r", encoding="utf-8") as stream:
            return normalise_config(json.load(stream))
    except (OSError, ValueError, TypeError):
        return normalise_config(None)


def save_config(
    data: Mapping[str, Any], path: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Validate and atomically save settings; return the saved representation."""

    target = Path(path).expanduser() if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = normalise_config(data)
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(clean, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return clean
