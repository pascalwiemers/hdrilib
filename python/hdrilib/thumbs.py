"""Thumbnail cache and Houdini OpenImageIO subprocess integration."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterable

from .config import thumbs_dir
from .houdini import executable as _executable
from .houdini import houdini_hfs as _houdini_hfs
from .houdini import run_subprocess as _run
from .jobs import JobCancelled, run_parallel


# Recipe changes intentionally invalidate old thumbnails.
THUMBNAIL_RECIPE = "h22-aces-sdr-exposure-minus1-rat-bridge-v2"
EXPOSURE_MULTIPLIER = "0.5"
LINEAR_COLORSPACE = "Linear Rec.709 (sRGB)"
DISPLAY = "sRGB - Display"
VIEW = "ACES 1.0 - SDR Video"


class ThumbnailError(RuntimeError):
    pass


class ThumbnailCancelled(JobCancelled, ThumbnailError):
    pass


def thumbnail_tools(hfs: str | None = None) -> list[str]:
    """Return available converters in preference order."""

    result = []
    for name in ("hoiiotool", "iconvert"):
        executable = _executable(name, hfs=hfs)
        if executable and executable not in result:
            result.append(executable)
    return result


def thumbnail_key(source: str | os.PathLike[str], size: int = 256) -> str:
    path = Path(source).expanduser().resolve()
    stat = path.stat()
    identity = "\0".join(
        (str(path), str(stat.st_mtime_ns), str(stat.st_size), str(int(size)), THUMBNAIL_RECIPE)
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def thumbnail_path(
    source: str | os.PathLike[str],
    size: int = 256,
    cache_dir: str | os.PathLike[str] | None = None,
) -> Path:
    directory = Path(cache_dir).expanduser() if cache_dir else thumbs_dir()
    return directory / (thumbnail_key(source, size=size) + ".png")


def cached_thumbnail(
    source: str | os.PathLike[str],
    size: int = 256,
    cache_dir: str | os.PathLike[str] | None = None,
) -> str | None:
    try:
        result = thumbnail_path(source, size=size, cache_dir=cache_dir)
    except OSError:
        return None
    return str(result) if result.is_file() and result.stat().st_size > 0 else None


def hoiiotool_command(
    executable: str,
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    size: int = 256,
) -> list[str]:
    """Build the empirically verified H22 thumbnail command.

    A one-stop reduction followed by Houdini's ACES SDR display transform keeps
    typical HDR environments readable and rolls highlights off below hard white.
    """

    return [
        executable,
        os.fspath(source),
        "--resize",
        "{}x0".format(int(size)),
        "--mulc",
        EXPOSURE_MULTIPLIER,
        "--ociodisplay:from={}".format(LINEAR_COLORSPACE),
        DISPLAY,
        VIEW,
        "-d",
        "uint8",
        "-o",
        os.fspath(output),
    ]


def iconvert_command(
    executable: str,
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    size: int = 256,
) -> list[str]:
    """Build the RAT-to-linear-EXR bridge command.

    H22's ``hoiiotool`` does not register Houdini's RAT reader, while ``iconvert``
    does. The intermediate remains float/linear and is deleted after hoiiotool.
    ``size`` is accepted for API symmetry and intentionally unused.
    """

    del size
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


def generate_thumbnail(
    source: str | os.PathLike[str],
    size: int = 256,
    cache_dir: str | os.PathLike[str] | None = None,
    force: bool = False,
    tool: str | None = None,
    timeout: float = 180.0,
    cancel_event: threading.Event | None = None,
) -> str:
    """Generate and cache a PNG thumbnail, returning its absolute path."""

    if cancel_event is not None and cancel_event.is_set():
        raise ThumbnailCancelled("Thumbnail generation cancelled")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise ThumbnailError("Source image does not exist: {}".format(source_path))
    size = max(32, min(2048, int(size)))
    target = thumbnail_path(source_path, size=size, cache_dir=cache_dir)
    if not force and target.is_file() and target.stat().st_size > 0:
        return str(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    tools = [tool] if tool else thumbnail_tools()
    if not tools:
        raise ThumbnailError("Could not find $HFS/bin/hoiiotool or iconvert")

    errors = []
    hoiiotool = next((value for value in tools if Path(value).name.lower() == "hoiiotool"), None)
    iconvert = next((value for value in tools if Path(value).name.lower() == "iconvert"), None)
    if hoiiotool and not iconvert:
        sibling = Path(hoiiotool).with_name("iconvert")
        if sibling.is_file() and os.access(sibling, os.X_OK):
            iconvert = str(sibling)

    # H22 hoiiotool is the resizing/tonemapping backend. For RAT, iconvert first
    # bridges Houdini's native format to a temporary float EXR.
    if hoiiotool:
        fd, temporary_name = tempfile.mkstemp(
            prefix=target.stem + ".", suffix=".png", dir=str(target.parent)
        )
        os.close(fd)
        bridge_name = None
        try:
            os.unlink(temporary_name)
            conversion_source = source_path
            if source_path.name.lower().endswith(".rat"):
                if not iconvert:
                    raise ThumbnailError("RAT conversion requires $HFS/bin/iconvert")
                bridge_fd, bridge_name = tempfile.mkstemp(
                    prefix=target.stem + ".", suffix=".exr", dir=str(target.parent)
                )
                os.close(bridge_fd)
                os.unlink(bridge_name)
                ok, detail = _run(
                    iconvert_command(iconvert, source_path, bridge_name), timeout, cancel_event
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise ThumbnailCancelled("Thumbnail generation cancelled")
                if not ok or not Path(bridge_name).is_file():
                    raise ThumbnailError("iconvert RAT bridge failed: {}".format(detail))
                conversion_source = Path(bridge_name)

            command = hoiiotool_command(hoiiotool, conversion_source, temporary_name, size)
            ok, detail = _run(command, timeout, cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                raise ThumbnailCancelled("Thumbnail generation cancelled")
            temporary = Path(temporary_name)
            if ok and temporary.is_file() and temporary.stat().st_size > 0:
                os.replace(str(temporary), str(target))
                return str(target)
            errors.append("{}: {}".format(hoiiotool, detail))
        except ThumbnailCancelled:
            raise
        except (OSError, ThumbnailError) as error:
            errors.append(str(error))
        finally:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            if bridge_name:
                try:
                    os.unlink(bridge_name)
                except OSError:
                    pass

    # A basic compatibility fallback for installations where hoiiotool is absent.
    # iconvert has no resize option, so panel-side icon scaling still bounds display.
    if iconvert and not hoiiotool:
        fd, temporary_name = tempfile.mkstemp(
            prefix=target.stem + ".", suffix=".png", dir=str(target.parent)
        )
        os.close(fd)
        try:
            os.unlink(temporary_name)
            command = [iconvert, "-d", "8", "-g", "auto", str(source_path), temporary_name]
            ok, detail = _run(command, timeout, cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                raise ThumbnailCancelled("Thumbnail generation cancelled")
            temporary = Path(temporary_name)
            if ok and temporary.is_file() and temporary.stat().st_size > 0:
                os.replace(str(temporary), str(target))
                return str(target)
            errors.append("{}: {}".format(iconvert, detail))
        finally:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    raise ThumbnailError(
        "Thumbnail generation failed for {}\n{}".format(source_path, "\n".join(errors))
    )


def generate_thumbnails_parallel(
    paths: Iterable[str | os.PathLike[str]],
    size: int = 256,
    workers: int = 1,
    cache_dir: str | os.PathLike[str] | None = None,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    on_result: Callable[[str, str], None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, bool]:
    """Generate thumbnails concurrently and report completions from this thread.

    Callbacks run on the calling thread, never on executor threads. This makes the
    helper safe for a Qt worker object to translate into queued signals. Cancellation
    prevents queued futures from starting and terminates active converter subprocesses.
    The return value is ``(completed, total, cancelled)``; failures count as completed.
    """

    sources = [os.path.abspath(os.path.expanduser(os.fspath(path))) for path in paths]

    def worker(source, event):
        return generate_thumbnail(
            source,
            size=size,
            cache_dir=cache_dir,
            force=force,
            cancel_event=event,
        )

    return run_parallel(
        sources,
        worker,
        workers=workers,
        cancel_event=cancel_event,
        on_result=on_result,
        on_error=on_error,
        on_progress=on_progress,
        thread_name_prefix="hdrilib-thumb",
    )
