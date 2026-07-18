"""Instant, subprocess-free analysis for the guided import flow."""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import config, files, resolution, thumbs, variants


_RUNG_DIRECTORY = re.compile(r"^[0-9]{1,3}k$", re.IGNORECASE)
_NATIVE_EXTENSIONS = tuple(
    extension for extension in config.DEFAULT_EXTENSIONS if extension != ".rat"
)


def resolution_bucket(dimensions: tuple[int, int] | None) -> str:
    """Return a compact width bucket without performing any probing."""

    if dimensions is None:
        return "unknown"
    width = int(dimensions[0])
    for boundary, label in (
        (16384, "16K"),
        (8192, "8K"),
        (4096, "4K"),
        (2048, "2K"),
        (1024, "1K"),
    ):
        if width >= boundary:
            return label
    return "<1K"


def _percentage(covered: int, total: int) -> int:
    return int(round(100.0 * covered / total)) if total else 0


def _group_originals(group: variants.Group) -> list[str]:
    """Choose source-quality members while retaining genuine format collisions."""

    native = [
        variant.path
        for variant in group.variants
        if variant.token is None and Path(variant.path).suffix.lower() != ".rat"
    ]
    if native:
        return native
    tokenless = [variant.path for variant in group.variants if variant.token is None]
    if tokenless:
        return tokenless
    largest = max(
        (variants.token_width(variant.token) or 0 for variant in group.variants),
        default=0,
    )
    candidates = [
        variant.path
        for variant in group.variants
        if (variants.token_width(variant.token) or 0) == largest
    ]
    non_rat = [path for path in candidates if Path(path).suffix.lower() != ".rat"]
    return non_rat or candidates


@dataclass(frozen=True)
class SourceAnalysis:
    root: str
    paths: tuple[str, ...]
    original_paths: tuple[str, ...]
    groups: tuple[variants.Group, ...]
    dimensions: Mapping[str, tuple[int, int] | None]
    format_counts: Mapping[str, int]
    resolution_counts: Mapping[str, int]
    total_bytes: int
    rat_coverage: int
    lowres_coverage: int
    thumbnail_coverage: int
    has_rat: bool
    has_suffix_variants: bool
    has_rung_folders: bool
    has_legacy_rat_names: bool
    category_subfolders: tuple[str, ...]
    categories_predominant: bool
    notes: tuple[str, ...]

    @property
    def image_count(self) -> int:
        """Number of logical HDRIs after exact-match variant grouping."""

        return len(self.groups)

    @property
    def file_count(self) -> int:
        return len(self.paths)

    @property
    def original_bytes(self) -> int:
        total = 0
        for path in self.original_paths:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total


def analyze_paths(
    root: str | os.PathLike[str],
    paths: Iterable[str | os.PathLike[str]],
    *,
    dimensions: Mapping[str, tuple[int, int] | None] | None = None,
    thumbnail_cached: Callable[[str], bool] | None = None,
    rat_subfolder_name: str = "rat",
) -> SourceAnalysis:
    """Analyze an explicit path set using only supplied metadata and pure logic."""

    root_path = os.path.abspath(os.path.expanduser(os.fspath(root)))
    ordered = tuple(
        dict.fromkeys(
            os.path.abspath(os.path.expanduser(os.fspath(path))) for path in paths
        )
    )
    supplied = {
        os.path.abspath(os.path.expanduser(os.fspath(path))): value
        for path, value in (dimensions or {}).items()
    }
    measured = {
        path: supplied.get(path) if path in supplied else resolution.probe_fast(path)
        for path in ordered
    }
    groups = tuple(variants.build_groups(ordered, rat_subfolder_name))
    originals = tuple(
        dict.fromkeys(path for group in groups for path in _group_originals(group))
    )

    category_counts: Counter[str] = Counter()
    excluded_top_level = {rat_subfolder_name.casefold()}
    structure_image_count = 0
    for original in originals:
        try:
            relative = Path(original).relative_to(root_path)
        except ValueError:
            continue
        if len(relative.parts) < 2:
            structure_image_count += 1
            continue
        first = relative.parts[0]
        if first.casefold() in excluded_top_level or _RUNG_DIRECTORY.match(first):
            continue
        category_counts[first] += 1
        structure_image_count += 1
    category_subfolders = tuple(
        sorted(category_counts, key=lambda value: (value.casefold(), value))
    )
    category_image_count = sum(category_counts.values())
    categories_predominant = bool(category_subfolders) and (
        category_image_count * 2 > structure_image_count
    )

    formats = Counter(files.extension_for(path, config.DEFAULT_EXTENSIONS) for path in ordered)
    formats.pop("", None)
    buckets = Counter(resolution_bucket(measured[path]) for path in ordered)
    total_bytes = 0
    for path in ordered:
        try:
            total_bytes += os.path.getsize(path)
        except OSError:
            pass

    rat_groups = 0
    lowres_groups = 0
    thumbnail_groups = 0
    for group in groups:
        all_group_paths = [
            path
            for variant in group.variants
            for path in (variant.path, *variant.companions)
        ]
        if any(path.lower().endswith(".rat") for path in all_group_paths):
            rat_groups += 1
        tokens = {variant.token for variant in group.variants}
        if len(tokens) > 1:
            lowres_groups += 1
        if thumbnail_cached is not None and any(
            thumbnail_cached(path) for path in all_group_paths
        ):
            thumbnail_groups += 1

    suffix_variants = False
    rung_folders = False
    legacy_rat = False
    directory_formats: dict[str, set[str]] = defaultdict(set)
    singleton_tokens = 0
    duplicate_native = False
    for path in ordered:
        extension = files.extension_for(path, config.DEFAULT_EXTENSIONS)
        if extension and extension != ".rat":
            directory_formats[os.path.dirname(path)].add(extension)
        if path.lower().endswith(tuple(ext + ".rat" for ext in _NATIVE_EXTENSIONS)):
            legacy_rat = True
        parent_name = os.path.basename(os.path.dirname(path))
        if _RUNG_DIRECTORY.match(parent_name):
            rung_folders = True
        stem = Path(path).stem
        if path.lower().endswith(".rat") and Path(stem).suffix.lower() in _NATIVE_EXTENSIONS:
            stem = Path(stem).stem
        _base, token = variants.split_token(stem)
        if token is not None:
            suffix_variants = True
    for group in groups:
        if len(group.variants) == 1 and group.variants[0].token is not None:
            singleton_tokens += 1
        if sum(variant.token is None for variant in group.variants) > 1:
            duplicate_native = True

    notes = []
    if not ordered:
        notes.append("No supported HDRI images were found in this folder.")
    if any(len(values) > 1 for values in directory_formats.values()):
        notes.append("Some folders mix source formats; the import can organize them consistently.")
    if suffix_variants and rung_folders:
        notes.append("Resolution variants use both filename suffixes and rung folders.")
    elif suffix_variants:
        notes.append("Resolution variants are marked with filename suffixes such as _4k.")
    elif rung_folders:
        notes.append("Resolution variants are already arranged in 4k/2k-style folders.")
    if singleton_tokens:
        notes.append("Some named resolution variants have no exact-match larger original.")
    if duplicate_native:
        notes.append("A few HDRIs have more than one source format with the same name.")
    if legacy_rat:
        notes.append("Legacy .exr.rat or .hdr.rat conversion names are present.")
    if ordered and not notes:
        notes.append("The folder is consistently named and needs little organization.")

    return SourceAnalysis(
        root=root_path,
        paths=ordered,
        original_paths=originals,
        groups=groups,
        dimensions=measured,
        format_counts=dict(formats),
        resolution_counts=dict(buckets),
        total_bytes=total_bytes,
        rat_coverage=_percentage(rat_groups, len(groups)),
        lowres_coverage=_percentage(lowres_groups, len(groups)),
        thumbnail_coverage=_percentage(thumbnail_groups, len(groups)),
        has_rat=rat_groups > 0,
        has_suffix_variants=suffix_variants,
        has_rung_folders=rung_folders,
        has_legacy_rat_names=legacy_rat,
        category_subfolders=category_subfolders,
        categories_predominant=categories_predominant,
        notes=tuple(notes),
    )


def analyze_folder(
    root: str | os.PathLike[str],
    *,
    thumbnail_size: int = 256,
    thumbnail_tonemap: str | None = None,
    rat_subfolder_name: str = "rat",
) -> SourceAnalysis:
    """Scan and analyze a folder without starting converters or other subprocesses."""

    root_path = os.path.abspath(os.path.expanduser(os.fspath(root)))
    paths = files.scan_files(
        root_path, extensions=config.DEFAULT_EXTENSIONS, recursive=True
    )
    return analyze_paths(
        root_path,
        paths,
        thumbnail_cached=lambda path: bool(
            thumbs.cached_thumbnail(
                path, size=thumbnail_size, tonemap=thumbnail_tonemap
            )
        ),
        rat_subfolder_name=rat_subfolder_name,
    )
