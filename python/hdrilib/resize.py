"""Low-resolution texture variants."""

from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import convert, resolution
from .houdini import executable as houdini_executable
from .houdini import run_subprocess
from .jobs import JobCancelled, run_parallel


STANDARD_RUNGS = (16384, 8192, 4096, 2048, 1024)
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
    source: str | os.PathLike[str],
    target_width: int,
    mode: str = "alongside",
    source_root: str | os.PathLike[str] | None = None,
    output_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Build a same-format low-res target without stacking ``_NNk`` suffixes."""

    source_path = Path(source).expanduser()
    label = rung_label(target_width)
    stem, suffix = _variant_stem_and_suffix(source_path)
    if mode == "alongside":
        return source_path.parent / (stem + "_" + label + suffix)
    if mode == "subfolder":
        if source_root is not None or output_root is not None:
            if source_root is None or output_root is None:
                raise ValueError("source_root and output_root must be provided together")
            source_base = Path(source_root).expanduser().resolve()
            output_base = Path(output_root).expanduser().resolve()
            try:
                relative_parent = source_path.resolve().parent.relative_to(source_base)
            except ValueError as error:
                raise ValueError("Resize source must be inside source_root") from error
            return output_base / label / relative_parent / (stem + suffix)
        return source_path.parent / label / (stem + suffix)
    raise ValueError("Low-res output mode must be 'alongside' or 'subfolder'")


def build_resize_rat_target(
    source: str | os.PathLike[str],
    target_width: int,
    mode: str = "alongside",
    source_root: str | os.PathLike[str] | None = None,
    output_root: str | os.PathLike[str] | None = None,
) -> Path:
    native = build_resize_target(
        source, target_width, mode, source_root=source_root, output_root=output_root
    )
    return native if Path(source).suffix.lower() == ".rat" else Path(str(native) + ".rat")


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
    """Compatibility adapter for the shared cached resolution probe."""

    if cancel_event is not None and cancel_event.is_set():
        raise ResizeCancelled("Resolution probe cancelled")
    try:
        if hoiiotool is not None or iconvert is not None or timeout != 180.0:
            result = resolution._probe_authoritative_with_tools(
                source,
                cancel_event=cancel_event,
                hoiiotool=hoiiotool,
                iconvert=iconvert,
                timeout=timeout,
            )
        else:
            result = resolution.probe_authoritative(source, cancel_event=cancel_event)
    except JobCancelled as error:
        raise ResizeCancelled(str(error)) from error
    except Exception as error:
        raise ResizeError(str(error)) from error
    if result is None:
        raise ResizeError(
            "Could not determine resolution for {}".format(Path(source).expanduser().resolve())
        )
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
    source_root: str | os.PathLike[str] | None = None,
    output_root: str | os.PathLike[str] | None = None,
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
    native_target = build_resize_target(
        source_path,
        width,
        mode,
        source_root=source_root,
        output_root=output_root,
    )
    source_is_rat = source_path.suffix.lower() == ".rat"
    targets = [native_target]
    if also_rat and not source_is_rat:
        targets.append(
            build_resize_rat_target(
                source_path,
                width,
                mode,
                source_root=source_root,
                output_root=output_root,
            )
        )
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
            source_path,
            width,
            mode,
            source_root=source_root,
            output_root=output_root,
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
    source_root: str | os.PathLike[str] | None = None,
    output_root: str | os.PathLike[str] | None = None,
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
            source_root=source_root,
            output_root=output_root,
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
