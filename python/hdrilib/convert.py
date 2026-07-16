"""Batch conversion of supported textures to Houdini RAT files."""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

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
    """Build a RAT target by appending ``.rat`` to the source's full filename."""

    source_path = Path(source).expanduser()
    if mode == "alongside":
        directory = source_path.parent
    elif mode == "subfolder":
        directory = source_path.parent / _subfolder_name(subfolder_name)
    else:
        raise ValueError("RAT output mode must be 'alongside' or 'subfolder'")
    return directory / (source_path.name + ".rat")


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


def _iconvert_executable() -> str | None:
    return houdini_executable("iconvert")


def convert_to_rat(
    source: str | os.PathLike[str],
    mode: str = "alongside",
    subfolder_name: str = "rat",
    overwrite: bool = False,
    executable: str | None = None,
    timeout: float = 600.0,
    cancel_event: threading.Event | None = None,
) -> RatConversionResult:
    """Convert one texture to RAT, or return a result describing why it was skipped."""

    if cancel_event is not None and cancel_event.is_set():
        raise RatConversionCancelled("RAT conversion cancelled")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise RatConversionError("Source image does not exist: {}".format(source_path))
    if source_path.name.lower().endswith(".rat"):
        return RatConversionResult(
            str(source_path), str(source_path), "skipped", "already_rat"
        )

    target = build_rat_target(source_path, mode, subfolder_name)
    try:
        up_to_date = target.is_file() and target.stat().st_mtime_ns >= source_path.stat().st_mtime_ns
    except OSError as error:
        raise RatConversionError(str(error)) from error
    if up_to_date and not overwrite:
        return RatConversionResult(
            str(source_path), str(target), "skipped", "target_newer"
        )

    tool = executable or _iconvert_executable()
    if not tool:
        raise RatConversionError("Could not find $HFS/bin/iconvert")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp.rat", dir=str(target.parent)
    )
    os.close(descriptor)
    try:
        os.unlink(temporary_name)
        ok, detail = run_subprocess(
            iconvert_rat_command(tool, source_path, temporary_name),
            timeout,
            cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise RatConversionCancelled("RAT conversion cancelled")
        temporary = Path(temporary_name)
        if not ok or not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RatConversionError(
                "iconvert RAT write failed for {}: {}".format(source_path, detail)
            )
        os.replace(str(temporary), str(target))
    except RatConversionCancelled:
        raise
    except OSError as error:
        raise RatConversionError(str(error)) from error
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
    return RatConversionResult(str(source_path), str(target), "converted")


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

    def worker(source: str, event: threading.Event) -> RatConversionResult:
        return convert_to_rat(
            source,
            mode=mode,
            subfolder_name=subfolder_name,
            overwrite=overwrite,
            executable=executable,
            cancel_event=event,
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
