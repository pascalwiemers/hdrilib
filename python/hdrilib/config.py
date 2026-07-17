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

SCHEMA_VERSION = 9
DEFAULT_THUMBNAIL_WORKERS = min(8, os.cpu_count() or 1)
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_CONFIG: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "roots": [],
    "location_ui_mode": "sidebar",
    "thumbnail_size": 256,
    "thumbnail_workers": DEFAULT_THUMBNAIL_WORKERS,
    "display_icon_size": 256,
    "view_mode": "grid",
    "thumbnail_tonemap": "neutral",
    "group_resolutions": False,
    "assign_resolution": "highest",
    "rat_output_mode": "alongside",
    "rat_subfolder_name": "rat",
    "rat_overwrite_existing": False,
    "lowres_output_mode": "alongside",
    "lowres_also_rat": False,
    "lowres_overwrite_existing": False,
    "prepare_auto_add_subfolders": True,
    "prepare_generate_thumbnails": True,
    "prepare_lowres_format": "both",
    "import_destination": "",
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


def _normalise_root(
    value: object, extension_fallback: list[str]
) -> dict[str, Any] | None:
    """Return one strict current root entry, accepting a v1 path string."""

    if isinstance(value, (str, os.PathLike)):
        raw_path = os.fspath(value)
        label = ""
        color = ""
        extensions = list(extension_fallback)
        include_in_all = True
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
        extensions = _normalise_extensions(
            value.get("extensions"), extension_fallback
        )
        include_in_all = value.get("include_in_all") is not False
    else:
        return None

    if not raw_path or not os.fspath(raw_path).strip():
        return None
    return {
        "path": os.path.abspath(os.path.expanduser(os.fspath(raw_path))),
        "label": label,
        "color": color,
        "extensions": extensions,
        "include_in_all": include_in_all,
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

    Version-1 root strings and version-2 root objects are accepted deliberately.
    All other malformed values are discarded or replaced with bounded defaults,
    and unknown keys never leak into the saved representation.
    """

    result = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(data, Mapping):
        return result

    source_version = data.get("version")
    legacy_extensions = list(DEFAULT_EXTENSIONS)
    if (
        isinstance(source_version, int)
        and not isinstance(source_version, bool)
        and source_version in (1, 2)
    ):
        legacy_extensions = _normalise_extensions(
            data.get("enabled_extensions"), legacy_extensions
        )

    roots = data.get("roots")
    if isinstance(roots, (list, tuple)):
        clean_roots = []
        seen_paths = set()
        for root in roots:
            clean_root = _normalise_root(root, legacy_extensions)
            if clean_root and clean_root["path"] not in seen_paths:
                seen_paths.add(clean_root["path"])
                clean_roots.append(clean_root)
        result["roots"] = clean_roots

    mode = data.get("location_ui_mode")
    if mode in ("sidebar", "dropdown"):
        result["location_ui_mode"] = mode

    size = data.get("thumbnail_size")
    if isinstance(size, int) and not isinstance(size, bool):
        result["thumbnail_size"] = max(64, min(1024, size))

    workers = data.get("thumbnail_workers")
    if isinstance(workers, int) and not isinstance(workers, bool):
        result["thumbnail_workers"] = max(1, min(64, workers))

    display_size = data.get("display_icon_size")
    if isinstance(display_size, int) and not isinstance(display_size, bool):
        result["display_icon_size"] = max(48, min(512, display_size))

    view_mode = data.get("view_mode")
    if view_mode in ("grid", "list"):
        result["view_mode"] = view_mode

    tonemap = data.get("thumbnail_tonemap")
    if tonemap in ("neutral", "aces"):
        result["thumbnail_tonemap"] = tonemap

    group_resolutions = data.get("group_resolutions")
    if isinstance(group_resolutions, bool):
        result["group_resolutions"] = group_resolutions

    assign_resolution = data.get("assign_resolution")
    if assign_resolution in ("highest", "lowest", "1024", "2048", "4096", "8192", "16384"):
        result["assign_resolution"] = assign_resolution

    rat_output_mode = data.get("rat_output_mode")
    if rat_output_mode in ("alongside", "subfolder"):
        result["rat_output_mode"] = rat_output_mode

    rat_subfolder_name = data.get("rat_subfolder_name")
    if isinstance(rat_subfolder_name, str):
        rat_subfolder_name = rat_subfolder_name.strip()
        if (
            rat_subfolder_name
            and rat_subfolder_name not in (".", "..")
            and "/" not in rat_subfolder_name
            and "\\" not in rat_subfolder_name
        ):
            result["rat_subfolder_name"] = rat_subfolder_name

    rat_overwrite_existing = data.get("rat_overwrite_existing")
    if isinstance(rat_overwrite_existing, bool):
        result["rat_overwrite_existing"] = rat_overwrite_existing

    lowres_output_mode = data.get("lowres_output_mode")
    if lowres_output_mode in ("alongside", "subfolder"):
        result["lowres_output_mode"] = lowres_output_mode

    for key in (
        "lowres_also_rat",
        "lowres_overwrite_existing",
        "prepare_auto_add_subfolders",
        "prepare_generate_thumbnails",
    ):
        value = data.get(key)
        if isinstance(value, bool):
            result[key] = value

    prepare_lowres_format = data.get("prepare_lowres_format")
    if prepare_lowres_format in ("native", "rat", "both"):
        result["prepare_lowres_format"] = prepare_lowres_format

    include_subfolders = data.get("include_subfolders")
    if isinstance(include_subfolders, bool):
        result["include_subfolders"] = include_subfolders
    for key in ("last_folder", "search_text", "import_destination"):
        value = data.get(key)
        if isinstance(value, str):
            result[key] = (
                os.path.abspath(os.path.expanduser(value))
                if key == "import_destination" and value.strip()
                else value
            )
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
