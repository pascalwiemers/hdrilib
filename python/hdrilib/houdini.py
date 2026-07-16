"""Houdini executable discovery and cancellable subprocess helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path


def houdini_hfs() -> str | None:
    """Read HFS from HOM when available, otherwise from the process environment."""

    try:
        import hou  # type: ignore

        value = hou.getenv("HFS")
        if value:
            return value
    except (ImportError, AttributeError, RuntimeError):
        pass
    return os.environ.get("HFS")


def executable(name: str, hfs: str | None = None) -> str | None:
    hfs = hfs or houdini_hfs()
    if hfs:
        candidate = Path(hfs) / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def run_subprocess(
    command: list[str],
    timeout: float,
    cancel_event: threading.Event | None = None,
) -> tuple[bool, str]:
    """Run one converter, polling for cancellation and enforcing a timeout."""

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    process.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                return False, "cancelled"
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, timeout))
                break
            except subprocess.TimeoutExpired:
                timeout -= 0.1
                if timeout <= 0:
                    process.kill()
                    stdout, stderr = process.communicate()
                    return False, (stderr or stdout or "conversion timed out").strip()
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    detail = (stderr or stdout or "no diagnostic output").strip()
    return process.returncode == 0, detail
