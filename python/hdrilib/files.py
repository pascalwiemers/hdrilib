"""Cross-platform texture discovery with suffix-aware format filters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator

from .config import DEFAULT_EXTENSIONS

# NAS/system metadata folders that hold fake image files (Synology thumbnails,
# recycle bins, Substance .alg_meta, macOS AppleDouble companions).
_SKIP_DIRECTORIES = {"@eadir", "@recycle", "#recycle", "@tmp", "$recycle.bin"}


def _wanted_directory(name: str) -> bool:
    return not name.startswith(".") and name.lower() not in _SKIP_DIRECTORIES


def _wanted_file(name: str) -> bool:
    return not name.startswith(".")


def normalise_extensions(extensions: Iterable[str] | None) -> tuple[str, ...]:
    values = DEFAULT_EXTENSIONS if extensions is None else extensions
    clean = []
    for value in values:
        value = str(value).strip().lower()
        if value and not value.startswith("."):
            value = "." + value
        if value and value not in clean:
            clean.append(value)
    # Longest first makes explicit double extensions deterministic.
    return tuple(sorted(clean, key=lambda item: (-len(item), item)))


def extension_for(path: str | os.PathLike[str], extensions: Iterable[str] | None = None) -> str:
    """Return the enabled suffix matching *path*, or an empty string."""

    name = os.fspath(path).lower()
    for extension in normalise_extensions(extensions):
        if name.endswith(extension):
            return extension
    return ""


def matches_extension(path: str | os.PathLike[str], extensions: Iterable[str] | None = None) -> bool:
    return bool(extension_for(path, extensions))


def iter_files(
    folder: str | os.PathLike[str],
    extensions: Iterable[str] | None = None,
    recursive: bool = False,
) -> Iterator[str]:
    """Yield matching files below one folder, silently skipping unreadable entries."""

    root = Path(folder).expanduser()
    if not root.is_dir():
        return
    if recursive:
        for current, directories, names in os.walk(root, onerror=lambda _error: None):
            directories[:] = [name for name in directories if _wanted_directory(name)]
            for name in names:
                if not _wanted_file(name) or not matches_extension(name, extensions):
                    continue
                path = Path(current) / name
                try:
                    if path.is_file():
                        # Preserve the path as it appears under the selected root.
                        # Resolving file symlinks can move a valid import source
                        # outside that root and breaks relative copy structure.
                        yield os.path.abspath(os.fspath(path))
                except OSError:
                    continue
        return
    try:
        iterator = root.iterdir()
        for path in iterator:
            try:
                if (
                    _wanted_file(path.name)
                    and path.is_file()
                    and matches_extension(path.name, extensions)
                ):
                    yield os.path.abspath(os.fspath(path))
            except OSError:
                continue
    except OSError:
        return


def scan_files(
    folders: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    extensions: Iterable[str] | None = None,
    recursive: bool = True,
) -> list[str]:
    """Return sorted, de-duplicated matches from one or more folders."""

    if isinstance(folders, (str, os.PathLike)):
        folders = [folders]
    found = set()
    for folder in folders:
        found.update(iter_files(folder, extensions=extensions, recursive=recursive))
    return sorted(found, key=lambda value: (value.lower(), value))


def iter_folders(root: str | os.PathLike[str]) -> Iterator[str]:
    """Yield *root* and its readable subdirectories in display order."""

    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        return
    yield os.path.abspath(os.fspath(root_path))
    for current, directories, _files in os.walk(root_path):
        directories[:] = sorted(
            (directory for directory in directories if _wanted_directory(directory)),
            key=str.lower,
        )
        for directory in directories:
            yield os.path.abspath(os.fspath(Path(current) / directory))
