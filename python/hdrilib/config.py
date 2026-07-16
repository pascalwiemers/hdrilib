"""Persistent, version-independent settings for HDRI Library."""

from __future__ import annotations

import copy
import json
import os
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

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "roots": [],
    "enabled_extensions": list(DEFAULT_EXTENSIONS),
    "thumbnail_size": 256,
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


def normalise_config(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge user data with defaults and discard malformed values."""

    result = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(data, Mapping):
        return result

    roots = data.get("roots")
    if isinstance(roots, (list, tuple)):
        clean_roots = []
        for root in roots:
            if isinstance(root, (str, os.PathLike)):
                path = os.path.abspath(os.path.expanduser(os.fspath(root)))
                if path not in clean_roots:
                    clean_roots.append(path)
        result["roots"] = clean_roots

    extensions = data.get("enabled_extensions")
    if isinstance(extensions, (list, tuple, set)):
        clean_extensions = []
        for extension in extensions:
            extension = _normalise_extension(extension)
            if extension and extension in DEFAULT_EXTENSIONS and extension not in clean_extensions:
                clean_extensions.append(extension)
        result["enabled_extensions"] = clean_extensions

    size = data.get("thumbnail_size")
    if isinstance(size, int) and not isinstance(size, bool):
        result["thumbnail_size"] = max(64, min(1024, size))

    result["include_subfolders"] = bool(data.get("include_subfolders", False))
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
