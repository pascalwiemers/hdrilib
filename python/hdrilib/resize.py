"""Low-resolution texture variants with cached resolution probing."""

from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import convert
from .houdini import executable as houdini_executable
from .houdini import run_subprocess
from .jobs import JobCancelled, run_parallel


STANDARD_RUNGS = (16384, 8192, 4096, 2048, 1024)
_RESOLUTION_RE = re.compile(r"(?:^|\s)(\d+)\s*x\s*(\d+)(?:\s|,|$)", re.IGNORECASE)
_RUNG_SUFFIX_RE = re.compile(r"_[0-9]+k$", re.IGNORECASE)
_RAT_SOURCE_SUFFIXES = {
    ".exr",
    ".hdr",
    ".tex",
    ".tx",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
}
_RESOLUTION_CACHE: dict[tuple[str, int], tuple[int, int]] = {}
_CACHE_LOCK = threading.Lock()


class ResizeError(RuntimeError):
    pass


class ResizeCancelled(JobCancelled, ResizeError):
    pass


@dataclass(frozen=True)
class ResizeResult:
    source: str
    targets: tuple[str, ...]
    status: str
    reason: str = ""

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def target(self) -> str:
        return self.targets[0] if self.targets else ""


def rung_label(width: int) -> str:
    if int(width) not in STANDARD_RUNGS:
        raise ValueError("Low-res width must be a standard rung")
    return "{}k".format(int(width) // 1024)


def rungs_below(source_width: int) -> tuple[int, ...]:
    """Return standard widths strictly smaller than ``source_width``."""

    return tuple(width for width in STANDARD_RUNGS if width < int(source_width))


def rungs_below_largest(widths: Iterable[int]) -> tuple[int, ...]:
    values = [int(width) for width in widths]
    return rungs_below(max(values)) if values else ()


def partition_by_width(
    widths: Mapping[str, int], target_width: int
) -> tuple[list[str], list[str]]:
    """Split paths into resize candidates and at-or-below-rung skips."""

    eligible = []
    skipped = []
    for path, source_width in widths.items():
        (eligible if int(source_width) > int(target_width) else skipped).append(path)
    return eligible, skipped


def strip_rung_suffix(stem: str) -> str:
    return _RUNG_SUFFIX_RE.sub("", stem)


def _variant_stem_and_suffix(source: Path) -> tuple[str, str]:
    """Keep a source extension embedded by the existing ``name.ext.rat`` UX."""

    stem = source.stem
    suffix = source.suffix
    embedded = Path(stem).suffix
    if suffix.lower() == ".rat" and embedded.lower() in _RAT_SOURCE_SUFFIXES:
        stem = Path(stem).stem
        suffix = embedded + suffix
    return strip_rung_suffix(stem), suffix


def build_resize_target(
    source: str | os.PathLike[str], target_width: int, mode: str = "alongside"
) -> Path:
    """Build a same-format low-res target without stacking ``_NNk`` suffixes."""

    source_path = Path(source).expanduser()
    label = rung_label(target_width)
    stem, suffix = _variant_stem_and_suffix(source_path)
    if mode == "alongside":
        return source_path.parent / (stem + "_" + label + suffix)
    if mode == "subfolder":
        return source_path.parent / label / (stem + suffix)
    raise ValueError("Low-res output mode must be 'alongside' or 'subfolder'")


def build_resize_rat_target(
    source: str | os.PathLike[str], target_width: int, mode: str = "alongside"
) -> Path:
    native = build_resize_target(source, target_width, mode)
    return native if Path(source).suffix.lower() == ".rat" else Path(str(native) + ".rat")


def hoiiotool_info_command(executable: str, source: str | os.PathLike[str]) -> list[str]:
    return [executable, "--info", os.fspath(source)]


def hoiiotool_resize_command(
    executable: str,
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    width: int,
    force_float: bool = True,
) -> list[str]:
    command = [executable, os.fspath(source), "--resize", "{}x0".format(int(width))]
    if force_float:
        command.extend(["-d", "float"])
    command.extend(["-o", os.fspath(output)])
    return command


def iconvert_rat_bridge_command(
    executable: str, source: str | os.PathLike[str], output: str | os.PathLike[str]
) -> list[str]:
    return [
        executable,
        "--force_rat_conversion",
        "-d",
        "float",
        "-g",
        "off",
        os.fspath(source),
        os.fspath(output),
    ]


def _parse_resolution(detail: str, source: Path) -> tuple[int, int]:
    match = _RESOLUTION_RE.search(detail)
    if not match:
        raise ResizeError("Could not parse resolution for {}: {}".format(source, detail))
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ResizeError("Invalid resolution for {}: {}x{}".format(source, width, height))
    return width, height


def _run_checked(
    command: list[str],
    description: str,
    timeout: float,
    cancel_event: threading.Event | None,
) -> str:
    ok, detail = run_subprocess(command, timeout, cancel_event)
    if cancel_event is not None and cancel_event.is_set():
        raise ResizeCancelled("Low-res creation cancelled")
    if not ok:
        raise ResizeError("{} failed: {}".format(description, detail))
    return detail


def get_resolution(
    source: str | os.PathLike[str],
    hoiiotool: str | None = None,
    iconvert: str | None = None,
    timeout: float = 180.0,
    cancel_event: threading.Event | None = None,
) -> tuple[int, int]:
    """Probe an image once per path/mtime, bridging RAT through float EXR."""

    if cancel_event is not None and cancel_event.is_set():
        raise ResizeCancelled("Resolution probe cancelled")
    source_path = Path(source).expanduser().resolve()
    try:
        mtime = source_path.stat().st_mtime_ns
    except OSError as error:
        raise ResizeError(str(error)) from error
    key = (str(source_path), mtime)
    with _CACHE_LOCK:
        cached = _RESOLUTION_CACHE.get(key)
    if cached is not None:
        return cached

    oiio = hoiiotool or houdini_executable("hoiiotool")
    if not oiio:
        raise ResizeError("Could not find $HFS/bin/hoiiotool")
    probe_source = source_path
    bridge_name = None
    try:
        if source_path.suffix.lower() == ".rat":
            rat_reader = iconvert or houdini_executable("iconvert")
            if not rat_reader:
                raise ResizeError("RAT resolution probing requires $HFS/bin/iconvert")
            descriptor, bridge_name = tempfile.mkstemp(prefix="hdrilib-info-", suffix=".exr")
            os.close(descriptor)
            os.unlink(bridge_name)
            _run_checked(
                iconvert_rat_bridge_command(rat_reader, source_path, bridge_name),
                "iconvert RAT bridge",
                timeout,
                cancel_event,
            )
            probe_source = Path(bridge_name)
        detail = _run_checked(
            hoiiotool_info_command(oiio, probe_source),
            "hoiiotool resolution probe",
            timeout,
            cancel_event,
        )
        result = _parse_resolution(detail, source_path)
    finally:
        if bridge_name:
            try:
                os.unlink(bridge_name)
            except OSError:
                pass
    with _CACHE_LOCK:
        for stale_key in [value for value in _RESOLUTION_CACHE if value[0] == str(source_path)]:
            _RESOLUTION_CACHE.pop(stale_key, None)
        _RESOLUTION_CACHE[key] = result
    return result


def probe_resolutions(
    paths: Iterable[str | os.PathLike[str]],
) -> tuple[dict[str, tuple[int, int]], list[tuple[str, Exception]]]:
    """Probe paths for menu construction, retaining individual failures."""

    resolutions = {}
    errors = []
    for path in paths:
        source = os.path.abspath(os.path.expanduser(os.fspath(path)))
        try:
            resolutions[source] = get_resolution(source)
        except Exception as error:
            errors.append((source, error))
    return resolutions, errors


def _atomic_native_resize(
    hoiiotool: str,
    source: Path,
    target: Path,
    width: int,
    timeout: float,
    cancel_event: threading.Event | None,
    force_float: bool,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.stem + ".", suffix=".tmp" + target.suffix, dir=str(target.parent)
    )
    os.close(descriptor)
    try:
        os.unlink(temporary_name)
        _run_checked(
            hoiiotool_resize_command(
                hoiiotool, source, temporary_name, width, force_float=force_float
            ),
            "hoiiotool resize",
            timeout,
            cancel_event,
        )
        temporary = Path(temporary_name)
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise ResizeError("hoiiotool did not create {}".format(target))
        os.replace(str(temporary), str(target))
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass


def resize_to_rung(
    source: str | os.PathLike[str],
    target_width: int,
    mode: str = "alongside",
    also_rat: bool = False,
    overwrite: bool = False,
    hoiiotool: str | None = None,
    iconvert: str | None = None,
    rat_executable: str | None = None,
    timeout: float = 600.0,
    cancel_event: threading.Event | None = None,
) -> ResizeResult:
    """Create one low-res variant, plus an optional mipmapped RAT companion."""

    source_path = Path(source).expanduser().resolve()
    width = int(target_width)
    rung_label(width)
    source_width, _source_height = get_resolution(
        source_path,
        hoiiotool=hoiiotool,
        iconvert=iconvert,
        timeout=timeout,
        cancel_event=cancel_event,
    )
    native_target = build_resize_target(source_path, width, mode)
    source_is_rat = source_path.suffix.lower() == ".rat"
    targets = [native_target]
    if also_rat and not source_is_rat:
        targets.append(build_resize_rat_target(source_path, width, mode))
    if source_width <= width:
        return ResizeResult(
            str(source_path), tuple(str(target) for target in targets), "skipped", "source_too_small"
        )

    try:
        source_mtime = source_path.stat().st_mtime_ns
        needed = [
            target
            for target in targets
            if overwrite
            or not target.is_file()
            or target.stat().st_mtime_ns < source_mtime
        ]
    except OSError as error:
        raise ResizeError(str(error)) from error
    if not needed:
        return ResizeResult(
            str(source_path), tuple(str(target) for target in targets), "skipped", "target_newer"
        )

    oiio = hoiiotool or houdini_executable("hoiiotool")
    if not oiio:
        raise ResizeError("Could not find $HFS/bin/hoiiotool")
    for target in needed:
        target.parent.mkdir(parents=True, exist_ok=True)

    # RAT input and RAT companion paths both resize through a float EXR so
    # imaketx receives un-clamped linear pixels and can build its mip pyramid.
    needs_rat = source_is_rat or any(target.suffix.lower() == ".rat" for target in needed)
    if not needs_rat:
        _atomic_native_resize(
            oiio,
            source_path,
            native_target,
            width,
            timeout,
            cancel_event,
            force_float=source_path.suffix.lower() in (".hdr", ".exr"),
        )
        return ResizeResult(str(source_path), tuple(str(target) for target in targets), "resized")

    with tempfile.TemporaryDirectory(prefix="hdrilib-resize-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        resize_source = source_path
        if source_is_rat:
            rat_reader = iconvert or houdini_executable("iconvert")
            if not rat_reader:
                raise ResizeError("RAT resizing requires $HFS/bin/iconvert")
            bridge = temporary_root / "source.exr"
            _run_checked(
                iconvert_rat_bridge_command(rat_reader, source_path, bridge),
                "iconvert RAT bridge",
                timeout,
                cancel_event,
            )
            resize_source = bridge

        resized_exr = temporary_root / "resized.exr"
        _run_checked(
            hoiiotool_resize_command(oiio, resize_source, resized_exr, width, force_float=True),
            "hoiiotool resize",
            timeout,
            cancel_event,
        )
        if not resized_exr.is_file() or resized_exr.stat().st_size <= 0:
            raise ResizeError("hoiiotool did not create the resized float EXR")

        rat_target = native_target if source_is_rat else build_resize_rat_target(
            source_path, width, mode
        )
        if rat_target in needed:
            convert.write_rat(
                resized_exr,
                rat_target,
                executable=rat_executable,
                timeout=timeout,
                cancel_event=cancel_event,
            )
        if not source_is_rat and native_target in needed:
            _atomic_native_resize(
                oiio,
                source_path,
                native_target,
                width,
                timeout,
                cancel_event,
                force_float=source_path.suffix.lower() in (".hdr", ".exr"),
            )
    return ResizeResult(str(source_path), tuple(str(target) for target in targets), "resized")


def resize_to_rung_parallel(
    paths: Iterable[str | os.PathLike[str]],
    target_width: int,
    mode: str = "alongside",
    also_rat: bool = False,
    overwrite: bool = False,
    workers: int = 1,
    cancel_event: threading.Event | None = None,
    on_result: Callable[[str, ResizeResult], None] | None = None,
    on_skipped: Callable[[str, str, str], None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, bool]:
    sources = []
    seen = set()
    for path in paths:
        source = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if source not in seen:
            seen.add(source)
            sources.append(source)

    def worker(source: str, event: threading.Event) -> ResizeResult:
        return resize_to_rung(
            source,
            target_width,
            mode=mode,
            also_rat=also_rat,
            overwrite=overwrite,
            cancel_event=event,
        )

    def result(source: str, resized: ResizeResult) -> None:
        if resized.skipped:
            if on_skipped is not None:
                on_skipped(source, resized.target, resized.reason)
        elif on_result is not None:
            on_result(source, resized)

    return run_parallel(
        sources,
        worker,
        workers=workers,
        cancel_event=cancel_event,
        on_result=result,
        on_error=on_error,
        on_progress=on_progress,
        thread_name_prefix="hdrilib-resize",
    )
