"""Fast image resolution probing with a small persistent metadata cache."""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import threading
from pathlib import Path

from . import config
from .houdini import executable as _houdini_executable
from .houdini import run_subprocess as _run_subprocess
from .jobs import JobCancelled


__all__ = ["probe_fast", "store", "probe_authoritative"]

_READ_LIMIT = 64 * 1024
_CACHE_FILE = "resolutions.json"
_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: dict[tuple[str, int, int], tuple[int, int]] = {}
_CACHE_LOADED = False
_RESOLUTION_RE = re.compile(r"(?:^|\s)(\d+)\s*x\s*(\d+)(?:\s|,|$)", re.IGNORECASE)
_HDR_RESOLUTION_RE = re.compile(rb"(?m)^\s*-Y\s+(\d+)\s+\+X\s+(\d+)\s*$")
_CACHE_ONLY_SUFFIXES = {".rat", ".tex", ".tx"}


class _ProbeError(RuntimeError):
    pass


def _identity(path: str | os.PathLike[str]) -> tuple[Path, tuple[str, int, int]] | None:
    source = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    try:
        stat = source.stat()
    except OSError:
        return None
    return source, (str(source), stat.st_mtime_ns, stat.st_size)


def _cache_path() -> Path:
    return config.config_dir() / _CACHE_FILE


def _read_cache_file() -> dict[tuple[str, int, int], tuple[int, int]]:
    try:
        with _cache_path().open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        return {}
    result = {}
    entries = data.get("entries")
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            key = (str(entry["path"]), int(entry["mtime_ns"]), int(entry["size"]))
            value = (int(entry["width"]), int(entry["height"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key[0] and key[1] >= 0 and key[2] >= 0 and value[0] > 0 and value[1] > 0:
            result[key] = value
    return result


def _load_cache_locked() -> None:
    global _CACHE_LOADED
    if not _CACHE_LOADED:
        _MEMORY_CACHE.update(_read_cache_file())
        _CACHE_LOADED = True


def _cached(key: tuple[str, int, int]) -> tuple[int, int] | None:
    with _CACHE_LOCK:
        _load_cache_locked()
        return _MEMORY_CACHE.get(key)


def _write_cache_locked() -> None:
    target = _cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "path": key[0],
            "mtime_ns": key[1],
            "size": key[2],
            "width": value[0],
            "height": value[1],
        }
        for key, value in sorted(_MEMORY_CACHE.items())
    ]
    descriptor, temporary = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "entries": entries}, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def store(path, width, height) -> None:
    """Persist dimensions for the file's current absolute path, mtime, and size."""

    identity = _identity(path)
    try:
        dimensions = int(width), int(height)
    except (TypeError, ValueError):
        return
    if identity is None or dimensions[0] <= 0 or dimensions[1] <= 0:
        return
    _source, key = identity
    with _CACHE_LOCK:
        _load_cache_locked()
        # Merge another process's most recently completed atomic write before ours.
        _MEMORY_CACHE.update(_read_cache_file())
        for stale in [cached_key for cached_key in _MEMORY_CACHE if cached_key[0] == key[0]]:
            _MEMORY_CACHE.pop(stale, None)
        _MEMORY_CACHE[key] = dimensions
        try:
            _write_cache_locked()
        except OSError:
            # A read-only/unavailable config directory must not break image jobs.
            pass


def _exr_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 8 or data[:4] != b"\x76\x2f\x31\x01":
        return None
    offset = 8
    while offset < len(data):
        name_end = data.find(b"\0", offset)
        if name_end < 0:
            return None
        if name_end == offset:
            return None
        name = data[offset:name_end]
        offset = name_end + 1
        type_end = data.find(b"\0", offset)
        if type_end < 0 or type_end + 5 > len(data):
            return None
        attribute_type = data[offset:type_end]
        offset = type_end + 1
        size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if size > len(data) - offset:
            return None
        if name == b"dataWindow" and attribute_type == b"box2i" and size == 16:
            minimum_x, minimum_y, maximum_x, maximum_y = struct.unpack_from("<4i", data, offset)
            width = maximum_x - minimum_x + 1
            height = maximum_y - minimum_y + 1
            return (width, height) if width > 0 and height > 0 else None
        offset += size
    return None


def _hdr_dimensions(data: bytes) -> tuple[int, int] | None:
    if not (data.startswith(b"#?RADIANCE") or data.startswith(b"#?RGBE")):
        return None
    match = _HDR_RESOLUTION_RE.search(data)
    return (int(match.group(2)), int(match.group(1))) if match else None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return (width, height) if width and height else None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    offset = 2
    sof_markers = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
    while offset < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]
        offset += 1
        if marker == 0xD9 or marker == 0xDA:
            return None
        if marker == 0x00 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            return None
        segment_size = struct.unpack_from(">H", data, offset)[0]
        if segment_size < 2 or offset + segment_size > len(data):
            return None
        if marker in sof_markers and segment_size >= 7:
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return (width, height) if width and height else None
        offset += segment_size
    return None


def _tiff_value(
    data: bytes, endian: str, field_type: int, count: int, raw: bytes
) -> int | None:
    sizes = {3: 2, 4: 4}
    item_size = sizes.get(field_type)
    if item_size is None or count < 1:
        return None
    if item_size * count <= 4:
        value_data = raw
    else:
        value_offset = struct.unpack(endian + "I", raw)[0]
        if value_offset + item_size > len(data):
            return None
        value_data = data[value_offset : value_offset + item_size]
    return struct.unpack(endian + ("H" if field_type == 3 else "I"), value_data[:item_size])[0]


def _tiff_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if data[:2] == b"II" else ">"
    if struct.unpack_from(endian + "H", data, 2)[0] != 42:
        return None
    ifd = struct.unpack_from(endian + "I", data, 4)[0]
    if ifd + 2 > len(data):
        return None
    count = struct.unpack_from(endian + "H", data, ifd)[0]
    width = height = None
    for index in range(count):
        offset = ifd + 2 + index * 12
        if offset + 12 > len(data):
            return None
        tag, field_type, value_count = struct.unpack_from(endian + "HHI", data, offset)
        if tag in (256, 257):
            value = _tiff_value(
                data, endian, field_type, value_count, data[offset + 8 : offset + 12]
            )
            if tag == 256:
                width = value
            else:
                height = value
    return (width, height) if width and height else None


def probe_fast(path) -> tuple[int, int] | None:
    """Return cached/header dimensions without ever launching a subprocess."""

    identity = _identity(path)
    if identity is None:
        return None
    source, key = identity
    cached = _cached(key)
    if cached is not None:
        return cached
    if source.suffix.lower() in _CACHE_ONLY_SUFFIXES:
        return None
    try:
        with source.open("rb") as stream:
            data = stream.read(_READ_LIMIT)
    except OSError:
        return None
    dimensions = (
        _exr_dimensions(data)
        or _hdr_dimensions(data)
        or _png_dimensions(data)
        or _jpeg_dimensions(data)
        or _tiff_dimensions(data)
    )
    if dimensions is not None:
        store(source, *dimensions)
    return dimensions


def _info_command(executable: str, source: Path) -> list[str]:
    return [executable, "--info", os.fspath(source)]


def _rat_bridge_command(executable: str, source: Path, output: str) -> list[str]:
    return [
        executable,
        "--force_rat_conversion",
        "-d",
        "float",
        "-g",
        "off",
        os.fspath(source),
        output,
    ]


def _external_probe(
    path,
    cancel_event=None,
    hoiiotool: str | None = None,
    iconvert: str | None = None,
    timeout: float = 180.0,
) -> tuple[int, int]:
    source = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled("Resolution probe cancelled")
    if not source.is_file():
        raise _ProbeError("Source image does not exist: {}".format(source))
    oiio = hoiiotool or _houdini_executable("hoiiotool")
    if not oiio:
        raise _ProbeError("Could not find $HFS/bin/hoiiotool")
    probe_source = source
    bridge_name = None
    try:
        if source.suffix.lower() == ".rat":
            rat_reader = iconvert or _houdini_executable("iconvert")
            if not rat_reader:
                raise _ProbeError("RAT resolution probing requires $HFS/bin/iconvert")
            descriptor, bridge_name = tempfile.mkstemp(prefix="hdrilib-info-", suffix=".exr")
            os.close(descriptor)
            os.unlink(bridge_name)
            ok, detail = _run_subprocess(
                _rat_bridge_command(rat_reader, source, bridge_name), timeout, cancel_event
            )
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("Resolution probe cancelled")
            if not ok:
                raise _ProbeError("iconvert RAT bridge failed: {}".format(detail))
            probe_source = Path(bridge_name)
        ok, detail = _run_subprocess(_info_command(oiio, probe_source), timeout, cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("Resolution probe cancelled")
        if not ok:
            raise _ProbeError("hoiiotool resolution probe failed: {}".format(detail))
        match = _RESOLUTION_RE.search(detail)
        if not match:
            raise _ProbeError("Could not parse resolution for {}: {}".format(source, detail))
        result = int(match.group(1)), int(match.group(2))
        if result[0] <= 0 or result[1] <= 0:
            raise _ProbeError("Invalid resolution for {}: {}x{}".format(source, *result))
        store(source, *result)
        return result
    finally:
        if bridge_name:
            try:
                os.unlink(bridge_name)
            except OSError:
                pass


def _probe_authoritative_with_tools(
    path,
    cancel_event=None,
    hoiiotool: str | None = None,
    iconvert: str | None = None,
    timeout: float = 180.0,
) -> tuple[int, int]:
    fast = probe_fast(path)
    if fast is not None:
        return fast
    return _external_probe(
        path,
        cancel_event=cancel_event,
        hoiiotool=hoiiotool,
        iconvert=iconvert,
        timeout=timeout,
    )


def probe_authoritative(path, cancel_event=None) -> tuple[int, int] | None:
    """Return dimensions from the fast path or an authoritative Houdini probe."""

    try:
        return _probe_authoritative_with_tools(path, cancel_event=cancel_event)
    except (OSError, _ProbeError):
        return None
