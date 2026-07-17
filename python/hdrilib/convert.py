"""Batch conversion of supported textures to Houdini RAT files."""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import config, files
from .houdini import executable as houdini_executable
from .houdini import run_subprocess
from .jobs import JobCancelled, run_parallel


HDR_EXTENSIONS = (".hdr", ".exr")


class RatConversionError(RuntimeError):
    pass


class RatConversionCancelled(JobCancelled, RatConversionError):
    pass


@dataclass(frozen=True)
class RatConversionResult:
    source: str
    target: str
    status: str
    reason: str = ""

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


def _subfolder_name(value: object) -> str:
    name = str(value).strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("RAT subfolder name must be one folder name")
    return name


def build_rat_target(
    source: str | os.PathLike[str], mode: str, subfolder_name: str
) -> Path:
    """Build a RAT target by replacing the source filename extension."""

    source_path = Path(source).expanduser()
    if mode == "alongside":
        directory = source_path.parent
    elif mode == "subfolder":
        directory = source_path.parent / _subfolder_name(subfolder_name)
    else:
        raise ValueError("RAT output mode must be 'alongside' or 'subfolder'")
    return directory / (source_path.stem + ".rat")


def build_legacy_rat_target(
    source: str | os.PathLike[str], mode: str, subfolder_name: str
) -> Path:
    """Build the historic appended-extension RAT target used by older libraries."""

    source_path = Path(source).expanduser()
    target = build_rat_target(source_path, mode, subfolder_name)
    return target.parent / (source_path.name + ".rat")


def allocate_rat_targets(
    paths: Iterable[str | os.PathLike[str]], mode: str, subfolder_name: str
) -> dict[str, Path]:
    """Allocate collision-free targets, retaining input order for precedence.

    The first source for a replaced-extension name receives that name. Later
    same-stem sources use the legacy appended-extension spelling so concurrent
    jobs can never write the same file.
    """

    result: dict[str, Path] = {}
    claimed = set()
    for path in paths:
        source = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if source in result:
            continue
        target = build_rat_target(source, mode, subfolder_name)
        key = os.path.normcase(os.path.abspath(os.fspath(target)))
        if key in claimed:
            target = build_legacy_rat_target(source, mode, subfolder_name)
            key = os.path.normcase(os.path.abspath(os.fspath(target)))
        if key in claimed:
            raise RatConversionError(
                "RAT target collision for {}: {}".format(source, target)
            )
        claimed.add(key)
        result[source] = target
    return result


def rat_collision_sources(source: str | os.PathLike[str]) -> list[str]:
    """Return supported same-folder sources that share a replaced RAT stem."""

    source_path = Path(os.path.abspath(os.path.expanduser(os.fspath(source))))
    stem = os.path.normcase(source_path.stem)
    peers = [
        path
        for path in files.scan_files(
            source_path.parent,
            extensions=config.DEFAULT_EXTENSIONS,
            recursive=False,
        )
        if Path(path).suffix.lower() != ".rat"
        and os.path.normcase(Path(path).stem) == stem
    ]
    source_text = str(source_path)
    if source_text not in peers:
        peers.append(source_text)
        peers.sort(key=lambda value: (value.lower(), value))
    return peers


def rat_collision_source_union(
    paths: Iterable[str | os.PathLike[str]],
) -> list[str]:
    """Expand requested sources with same-stem siblings, preserving scan order."""

    result = []
    seen = set()
    for path in paths:
        for peer in rat_collision_sources(path):
            if peer not in seen:
                seen.add(peer)
                result.append(peer)
    return result


def iconvert_rat_command(
    executable: str,
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
) -> list[str]:
    """Build an iconvert RAT-write command, preserving linear HDR range."""

    command = [executable]
    if Path(source).suffix.lower() in HDR_EXTENSIONS:
        command.extend(["-d", "float", "-g", "off"])
    command.extend([os.fspath(source), os.fspath(output)])
    return command


def imaketx_rat_command(
    executable: str,
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
) -> list[str]:
    """Build an imaketx RAT-write command for linear, mipmapped textures.

    imaketx preserves the input pixel type.  Disabling its automatic sRGB
    linearisation keeps HDR/EXR values unchanged, while ``linearmips`` computes
    the generated mip levels directly in that same linear space.
    """

    return [
        executable,
        os.fspath(source),
        os.fspath(output),
        "--format",
        "RAT",
        "--linearize",
        "0",
        "--linearmips",
        "on",
        "--no-sanitize",
    ]


def _imaketx_executable() -> str | None:
    return houdini_executable("imaketx")


def _iconvert_executable() -> str | None:
    return houdini_executable("iconvert")


def _rat_backends(executable: str | None = None) -> list[tuple[str, str]]:
    if executable:
        name = "iconvert" if Path(executable).name.lower() == "iconvert" else "imaketx"
        return [(name, executable)]
    result = []
    imaketx = _imaketx_executable()
    iconvert = _iconvert_executable()
    if imaketx:
        result.append(("imaketx", imaketx))
    if iconvert:
        result.append(("iconvert", iconvert))
    return result


def write_rat(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    executable: str | None = None,
    timeout: float = 600.0,
    cancel_event: threading.Event | None = None,
) -> None:
    """Atomically write a RAT, preferring mipmapped imaketx over iconvert."""

    source_path = Path(os.path.abspath(os.path.expanduser(os.fspath(source))))
    target = Path(output).expanduser()
    backends = _rat_backends(executable)
    if not backends:
        raise RatConversionError("Could not find $HFS/bin/imaketx or iconvert")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp.rat", dir=str(target.parent)
    )
    os.close(descriptor)
    errors = []
    try:
        for backend, tool in backends:
            if cancel_event is not None and cancel_event.is_set():
                raise RatConversionCancelled("RAT conversion cancelled")
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            command = (
                imaketx_rat_command(tool, source_path, temporary_name)
                if backend == "imaketx"
                else iconvert_rat_command(tool, source_path, temporary_name)
            )
            ok, detail = run_subprocess(command, timeout, cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                raise RatConversionCancelled("RAT conversion cancelled")
            temporary = Path(temporary_name)
            if ok and temporary.is_file() and temporary.stat().st_size > 0:
                os.replace(str(temporary), str(target))
                return
            errors.append("{}: {}".format(backend, detail))
        raise RatConversionError(
            "RAT write failed for {}: {}".format(source_path, "; ".join(errors))
        )
    except RatConversionCancelled:
        raise
    except OSError as error:
        raise RatConversionError(str(error)) from error
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass


def convert_to_rat(
    source: str | os.PathLike[str],
    mode: str = "alongside",
    subfolder_name: str = "rat",
    overwrite: bool = False,
    executable: str | None = None,
    timeout: float = 600.0,
    cancel_event: threading.Event | None = None,
    target: str | os.PathLike[str] | None = None,
) -> RatConversionResult:
    """Convert one texture to RAT, or return a result describing why it was skipped."""

    if cancel_event is not None and cancel_event.is_set():
        raise RatConversionCancelled("RAT conversion cancelled")
    source_path = Path(os.path.abspath(os.path.expanduser(os.fspath(source))))
    if not source_path.is_file():
        raise RatConversionError("Source image does not exist: {}".format(source_path))
    if source_path.name.lower().endswith(".rat"):
        return RatConversionResult(
            str(source_path), str(source_path), "skipped", "already_rat"
        )

    if target is not None:
        selected_target = Path(target).expanduser()
    else:
        selected_target = allocate_rat_targets(
            rat_collision_sources(source_path), mode, subfolder_name
        )[str(source_path)]
    candidates = [selected_target]
    legacy_target = build_legacy_rat_target(source_path, mode, subfolder_name)
    if legacy_target != selected_target:
        candidates.append(legacy_target)
    try:
        source_mtime = source_path.stat().st_mtime_ns
        up_to_date_target = next(
            (
                candidate
                for candidate in candidates
                if candidate.is_file() and candidate.stat().st_mtime_ns >= source_mtime
            ),
            None,
        )
    except OSError as error:
        raise RatConversionError(str(error)) from error
    if up_to_date_target is not None and not overwrite:
        return RatConversionResult(
            str(source_path), str(up_to_date_target), "skipped", "target_newer"
        )

    write_rat(
        source_path,
        selected_target,
        executable=executable,
        timeout=timeout,
        cancel_event=cancel_event,
    )
    return RatConversionResult(str(source_path), str(selected_target), "converted")


def convert_to_rat_parallel(
    paths: Iterable[str | os.PathLike[str]],
    mode: str = "alongside",
    subfolder_name: str = "rat",
    overwrite: bool = False,
    workers: int = 1,
    executable: str | None = None,
    cancel_event: threading.Event | None = None,
    on_result: Callable[[str, str], None] | None = None,
    on_skipped: Callable[[str, str, str], None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, bool]:
    """Convert textures concurrently with bounded submission and prompt cancellation."""

    sources = []
    seen = set()
    for path in paths:
        source = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if source not in seen:
            seen.add(source)
            sources.append(source)
    targets = allocate_rat_targets(
        rat_collision_source_union(sources), mode, subfolder_name
    )

    def worker(source: str, event: threading.Event) -> RatConversionResult:
        return convert_to_rat(
            source,
            mode=mode,
            subfolder_name=subfolder_name,
            overwrite=overwrite,
            executable=executable,
            cancel_event=event,
            target=targets[source],
        )

    def result(source: str, conversion: RatConversionResult) -> None:
        if conversion.skipped:
            if on_skipped is not None:
                on_skipped(source, conversion.target, conversion.reason)
        elif on_result is not None:
            on_result(source, conversion.target)

    return run_parallel(
        sources,
        worker,
        workers=workers,
        cancel_event=cancel_event,
        on_result=result,
        on_error=on_error,
        on_progress=on_progress,
        thread_name_prefix="hdrilib-rat",
    )
