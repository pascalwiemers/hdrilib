"""Planning and execution for guided import and library preparation."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from . import config, convert, files, resize, thumbs
from .jobs import JobCancelled, run_parallel


PREPARE_RUNGS = (8192, 4096, 2048, 1024)


@dataclass(frozen=True)
class ResizeStage:
    width: int
    sources: tuple[str, ...]
    targets: tuple[str, ...]


@dataclass(frozen=True)
class PipelinePlan:
    root: str
    sources: tuple[str, ...]
    convert_originals: bool
    resize_stages: tuple[ResizeStage, ...]
    convert_generated: bool
    lowres_format: str = "both"
    copy_sources: tuple[str, ...] = ()
    copy_targets: tuple[str, ...] = ()

    @property
    def rungs(self) -> tuple[int, ...]:
        return tuple(stage.width for stage in self.resize_stages)

    @property
    def generated_folders(self) -> tuple[str, ...]:
        return tuple(str(Path(self.root) / resize.rung_label(width)) for width in self.rungs)

    @property
    def total(self) -> int:
        resize_count = sum(len(stage.sources) for stage in self.resize_stages)
        return (
            len(self.copy_sources)
            + (len(self.sources) if self.convert_originals else 0)
            + resize_count
        )


@dataclass
class PipelineSummary:
    completed: int = 0
    converted: int = 0
    resized: int = 0
    skipped: int = 0
    failed: int = 0
    thumbnails: int = 0
    copied: int = 0


class CopyCancelled(JobCancelled):
    pass


@dataclass(frozen=True)
class CopyResult:
    source: str
    target: str
    status: str
    reason: str = ""

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


@dataclass(frozen=True)
class RootScanClassification:
    """Explain why a filtered Settings-root action has no convertible input."""

    state: str
    hidden_count: int = 0


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _absolute_path(path: str | os.PathLike[str]) -> str:
    return os.path.abspath(os.path.expanduser(os.fspath(path)))


def scan_root(root: Mapping[str, object]) -> list[str]:
    """Recursively scan a root using its own extension filter.

    Top-level standard-rung folders are outputs of this workflow and are excluded
    so repeatedly preparing a root never nests or compounds generated variants.
    """

    root_path = config.resolve_root_path(os.fspath(root["path"]))
    if not root_path:
        return []
    extensions = root.get("extensions", ())
    if not isinstance(extensions, (list, tuple, set)) or not extensions:
        return []
    generated_names = {resize.rung_label(width).lower() for width in PREPARE_RUNGS}
    result = []
    for path in files.scan_files(root_path, extensions=extensions, recursive=True):
        try:
            relative = Path(path).relative_to(root_path)
        except ValueError:
            continue
        if relative.parts and relative.parts[0].lower() in generated_names:
            continue
        result.append(path)
    return result


def classify_root_scan(
    root: Mapping[str, object],
    matching_paths: Iterable[str | os.PathLike[str]] | None = None,
) -> RootScanClassification:
    """Classify filtered input with one all-supported-formats filesystem scan."""

    matching = (
        scan_root(root)
        if matching_paths is None
        else [os.fspath(path) for path in matching_paths]
    )
    unfiltered_root = dict(root)
    unfiltered_root["extensions"] = config.DEFAULT_EXTENSIONS
    unfiltered = scan_root(unfiltered_root)
    if matching:
        if all(
            files.extension_for(path, config.DEFAULT_EXTENSIONS) == ".rat"
            for path in matching
        ):
            return RootScanClassification("only-rat")
        return RootScanClassification("has-matching")
    if unfiltered:
        return RootScanClassification("hidden-by-filter", len(unfiltered))
    return RootScanClassification("empty")


def sensible_rungs(
    paths: Iterable[str | os.PathLike[str]],
    dimensions: Mapping[str, tuple[int, int] | None],
) -> tuple[int, ...]:
    """Return useful 8K..1K rungs; one unknown source makes every rung available."""

    values = list(paths)
    normalized_dimensions = {
        _path_key(path): value for path, value in dimensions.items()
    }
    known_widths = []
    for path in values:
        dimension = normalized_dimensions.get(_path_key(path))
        if dimension is None:
            return PREPARE_RUNGS
        known_widths.append(int(dimension[0]))
    if not known_widths:
        return ()
    largest = max(known_widths)
    return tuple(width for width in PREPARE_RUNGS if width < largest)


def _validate_copy_roots(source_root: str, destination: str) -> None:
    source_key = _path_key(source_root)
    destination_key = _path_key(destination)
    try:
        overlap = os.path.commonpath([source_key, destination_key])
    except ValueError:
        return
    if overlap in (source_key, destination_key):
        raise ValueError(
            "Copy destination must be separate from the read-only source folder"
        )


def build_pipeline_plan(
    root: str | os.PathLike[str],
    paths: Iterable[str | os.PathLike[str]],
    convert_to_rat: bool = False,
    rungs: Iterable[int] = (),
    widths: Mapping[str, int | None] | None = None,
    lowres_format: str = "both",
    copy_destination: str | os.PathLike[str] | None = None,
) -> PipelinePlan:
    """Build a deterministic plan without touching image tools or creating files."""

    if lowres_format not in ("native", "rat", "both"):
        raise ValueError("Low-res output format must be 'native', 'rat', or 'both'")
    source_root = _absolute_path(root)
    root_key = _path_key(source_root)
    normalized_widths = (
        {_path_key(path): value for path, value in widths.items()}
        if widths is not None
        else {}
    )
    unique = []
    seen = set()
    for path in paths:
        source = _absolute_path(path)
        source_key = _path_key(source)
        if source_key in seen:
            continue
        try:
            inside = os.path.commonpath([root_key, source_key]) == root_key
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("Pipeline source must be inside the prepared root")
        seen.add(source_key)
        unique.append(source)

    copy_sources: tuple[str, ...] = ()
    copy_targets: tuple[str, ...] = ()
    working_sources = list(unique)
    working_widths = dict(normalized_widths)
    root_path = source_root
    if copy_destination is not None:
        root_path = _absolute_path(copy_destination)
        _validate_copy_roots(source_root, root_path)
        targets = []
        remapped_widths = {}
        for source in unique:
            relative = Path(source).relative_to(source_root)
            target = str(Path(root_path) / relative)
            targets.append(target)
            remapped_widths[_path_key(target)] = normalized_widths.get(
                _path_key(source)
            )
        copy_sources = tuple(unique)
        copy_targets = tuple(targets)
        working_sources = targets
        working_widths = remapped_widths

    stages = []
    seen_rungs = set()
    for width in rungs:
        width = int(width)
        if width not in PREPARE_RUNGS:
            raise ValueError("Prepare rung must be 8K, 4K, 2K, or 1K")
        if width in seen_rungs:
            continue
        seen_rungs.add(width)
        candidates = []
        targets = []
        eligible = [
            source
            for source in working_sources
            if working_widths.get(_path_key(source)) is None
            or int(working_widths[_path_key(source)]) > width
        ]
        rat_targets = resize.allocate_resize_rat_targets(
            convert.rat_collision_source_union(eligible),
            width,
            "subfolder",
            source_root=root_path,
            output_root=root_path,
        )
        for source in working_sources:
            source_width = working_widths.get(_path_key(source))
            if source_width is not None and int(source_width) <= width:
                continue
            candidates.append(source)
            native_target = resize.build_resize_target(
                source,
                width,
                "subfolder",
                source_root=root_path,
                output_root=root_path,
            )
            rat_target = rat_targets[source]
            if Path(source).suffix.lower() == ".rat" or lowres_format == "native":
                targets.append(str(native_target))
            elif lowres_format == "rat":
                targets.append(str(rat_target))
            else:
                targets.extend((str(native_target), str(rat_target)))
        stages.append(ResizeStage(width, tuple(candidates), tuple(targets)))
    return PipelinePlan(
        root_path,
        tuple(working_sources),
        bool(convert_to_rat),
        tuple(stages),
        False,
        lowres_format,
        copy_sources,
        copy_targets,
    )


def copy_original(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    cancel_event: threading.Event | None = None,
    chunk_size: int = 4 * 1024 * 1024,
) -> CopyResult:
    """Atomically copy one original while treating the source as read-only."""

    event = cancel_event or threading.Event()
    if event.is_set():
        raise CopyCancelled("Import copy cancelled")
    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser()
    if not source_path.is_file():
        raise OSError("Source image does not exist: {}".format(source_path))
    source_stat = source_path.stat()
    try:
        target_stat = target_path.stat()
    except OSError:
        target_stat = None
    if (
        target_stat is not None
        and target_stat.st_size == source_stat.st_size
        and target_stat.st_mtime_ns == source_stat.st_mtime_ns
    ):
        return CopyResult(
            str(source_path), str(target_path), "skipped", "identical"
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target_path.name + ".", suffix=".tmp", dir=str(target_path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as output, source_path.open("rb") as input_stream:
            while True:
                if event.is_set():
                    raise CopyCancelled("Import copy cancelled")
                block = input_stream.read(max(64 * 1024, int(chunk_size)))
                if not block:
                    break
                output.write(block)
        if event.is_set():
            raise CopyCancelled("Import copy cancelled")
        shutil.copystat(source_path, temporary_name, follow_symlinks=True)
        os.replace(temporary_name, target_path)
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
    return CopyResult(str(source_path), str(target_path), "copied")


def copy_originals_parallel(
    sources: Iterable[str | os.PathLike[str]],
    targets: Iterable[str | os.PathLike[str]],
    *,
    workers: int = 1,
    cancel_event: threading.Event | None = None,
    on_result: Callable[[str, str], None] | None = None,
    on_skipped: Callable[[str, str, str], None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, bool]:
    """Copy a plan's one-to-one source/target pairs with bounded cancellation."""

    source_values = [_absolute_path(path) for path in sources]
    target_values = [_absolute_path(path) for path in targets]
    if len(source_values) != len(target_values):
        raise ValueError("Copy sources and targets must have matching lengths")
    target_by_source = dict(zip(source_values, target_values))

    def worker(source: str, event: threading.Event) -> CopyResult:
        return copy_original(source, target_by_source[source], cancel_event=event)

    def result(source: str, copied: CopyResult) -> None:
        if copied.skipped:
            if on_skipped is not None:
                on_skipped(source, copied.target, copied.reason)
        elif on_result is not None:
            on_result(source, copied.target)

    return run_parallel(
        source_values,
        worker,
        workers=workers,
        cancel_event=cancel_event,
        on_result=result,
        on_error=on_error,
        on_progress=on_progress,
        thread_name_prefix="hdrilib-copy",
    )


def finished_import_root_entry(
    plan: PipelinePlan,
    *,
    existing_root: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the single browse-friendly root entry produced by an import."""

    existing = existing_root or {}
    native_extensions = []
    source_paths = plan.copy_sources or plan.sources
    for source in source_paths:
        extension = files.extension_for(source, config.DEFAULT_EXTENSIONS)
        if extension and extension not in native_extensions:
            native_extensions.append(extension)

    every_output_has_rat = plan.convert_originals and all(
        plan.lowres_format in ("rat", "both") for _stage in plan.resize_stages
    )
    if every_output_has_rat:
        extensions = [".rat"]
    else:
        enabled = set(native_extensions)
        if plan.convert_originals or (
            plan.resize_stages and plan.lowres_format in ("rat", "both")
        ):
            enabled.add(".rat")
        extensions = [
            extension for extension in config.DEFAULT_EXTENSIONS if extension in enabled
        ]
    label = str(existing.get("label") or "").strip()
    if not label:
        label = os.path.basename(os.path.normpath(plan.root)) or plan.root
    return {
        "path": _absolute_path(plan.root),
        "label": label,
        "color": str(existing.get("color") or ""),
        "extensions": extensions,
        "include_in_all": existing.get("include_in_all") is not False,
    }


def finished_import_config(
    settings: Mapping[str, object], plan: PipelinePlan
) -> dict[str, object]:
    """Return normalized settings after landing a completed guided import."""

    result = dict(settings)
    roots = [dict(root) for root in settings.get("roots", [])]
    root_key = _path_key(plan.root)

    def resolved_key(root: Mapping[str, object]) -> str:
        if not root.get("path"):
            return ""
        resolved = config.resolve_root_path(os.fspath(root["path"]))
        return _path_key(resolved) if resolved else ""

    index = next(
        (
            position
            for position, root in enumerate(roots)
            if resolved_key(root) == root_key
        ),
        None,
    )
    existing = roots[index] if index is not None else None
    entry = finished_import_root_entry(plan, existing_root=existing)
    if existing is not None and config.has_path_variables(existing.get("path")):
        entry["path"] = existing["path"]
    if index is None:
        roots.append(entry)
    else:
        roots[index] = entry
    result["roots"] = roots
    result["group_resolutions"] = True
    result["include_subfolders"] = True
    result["last_folder"] = _absolute_path(plan.root)
    return config.normalise_config(result)


def generated_root_entries(
    existing_roots: Sequence[Mapping[str, object]],
    parent_root: Mapping[str, object],
    folders: Iterable[tuple[str | os.PathLike[str], str, object]],
) -> list[dict[str, object]]:
    """Return deduplicated roots for generated folders, inheriting parent attrs."""

    seen_raw = {
        os.fspath(root["path"]) for root in existing_roots if root.get("path")
    }
    seen_resolved = {
        _path_key(resolved)
        for root in existing_roots
        if root.get("path")
        for resolved in [config.resolve_root_path(os.fspath(root["path"]))]
        if resolved
    }
    parent_label = str(parent_root.get("label") or "").strip()
    if not parent_label:
        parent_path = os.fspath(parent_root["path"])
        parent_label = os.path.basename(os.path.normpath(parent_path)) or parent_path
    inherited_extensions = list(parent_root.get("extensions") or ())
    parent_raw = os.fspath(parent_root["path"])
    parent_resolved = config.resolve_root_path(parent_raw)
    result = []
    for folder, suffix, output_format in folders:
        resolved_path = _absolute_path(folder)
        path = resolved_path
        if config.has_path_variables(parent_raw) and parent_resolved:
            try:
                relative = os.path.relpath(resolved_path, parent_resolved)
                if relative != os.pardir and not relative.startswith(
                    os.pardir + os.sep
                ):
                    path = os.path.join(parent_raw, relative)
            except ValueError:
                pass
        key = _path_key(resolved_path)
        if path in seen_raw or key in seen_resolved:
            continue
        seen_raw.add(path)
        seen_resolved.add(key)
        if output_format is True or output_format == "rat":
            extensions = [".rat"]
        elif output_format == "both":
            extensions = list(inherited_extensions)
            if ".rat" not in extensions:
                extensions.append(".rat")
        else:
            extensions = list(inherited_extensions)
        result.append(
            {
                "path": path,
                "label": "{} {}".format(parent_label, str(suffix).strip()).strip(),
                "color": str(parent_root.get("color") or ""),
                "extensions": extensions,
            }
        )
    return result


def folder_is_rat_only(path: str | os.PathLike[str]) -> bool:
    found = False
    try:
        iterator = Path(path).rglob("*")
        for item in iterator:
            if item.is_file():
                found = True
                if item.suffix.lower() != ".rat":
                    return False
    except OSError:
        return False
    return found


def final_thumbnail_paths(
    plan: PipelinePlan,
    *,
    rat_mode: str = "alongside",
    rat_subfolder_name: str = "rat",
    resize_also_rat: bool = False,
) -> list[str]:
    """Return existing final pipeline inputs and outputs, once each.

    This is intentionally pure planning/filesystem logic: callers can use it after
    the conversion and resize stages without depending on either image backend.
    """

    candidates = list(plan.sources)
    generated = [target for stage in plan.resize_stages for target in stage.targets]
    candidates.extend(generated)
    if plan.convert_originals:
        rat_targets = convert.allocate_rat_targets(
            convert.rat_collision_source_union(plan.sources),
            rat_mode,
            rat_subfolder_name,
        )
        candidates.extend(
            str(rat_targets[_absolute_path(source)])
            for source in plan.sources
        )
    if resize_also_rat and plan.lowres_format == "native":
        for stage in plan.resize_stages:
            rat_targets = resize.allocate_resize_rat_targets(
                convert.rat_collision_source_union(stage.sources),
                stage.width,
                "subfolder",
                source_root=plan.root,
                output_root=plan.root,
            )
            candidates.extend(
                str(rat_targets[_absolute_path(source)])
                for source in stage.sources
            )

    result = []
    seen = set()
    for candidate in candidates:
        absolute = _absolute_path(candidate)
        key = _path_key(absolute)
        if key not in seen and os.path.isfile(absolute):
            seen.add(key)
            result.append(absolute)
    return result


def run_pipeline(
    plan: PipelinePlan,
    *,
    rat_mode: str = "alongside",
    rat_subfolder_name: str = "rat",
    rat_overwrite: bool = False,
    resize_also_rat: bool = False,
    resize_overwrite: bool = False,
    generate_thumbnails: bool = False,
    thumbnail_size: int = 256,
    thumbnail_tonemap: str | None = None,
    workers: int = 1,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_problem: Callable[[str, Exception], None] | None = None,
) -> tuple[PipelineSummary, bool]:
    """Execute all plan stages sequentially, using existing parallel batch helpers."""

    event = cancel_event or threading.Event()
    summary = PipelineSummary()
    total = plan.total

    def progressed(_current: int, _stage_total: int) -> None:
        summary.completed += 1
        if on_progress is not None:
            on_progress(summary.completed, total)

    def problem(source: str, error: Exception) -> None:
        summary.failed += 1
        if on_problem is not None:
            on_problem(source, error)

    def converted(_source: str, _target: str) -> None:
        summary.converted += 1

    def skipped(_source: str, _target: str, _reason: str) -> None:
        summary.skipped += 1

    def resized(_source: str, result: resize.ResizeResult) -> None:
        summary.resized += 1

    def resize_skipped(_source: str, target: str, reason: str) -> None:
        summary.skipped += 1

    def copied(_source: str, _target: str) -> None:
        summary.copied += 1

    if plan.copy_sources and not event.is_set():
        copy_originals_parallel(
            plan.copy_sources,
            plan.copy_targets,
            workers=workers,
            cancel_event=event,
            on_result=copied,
            on_skipped=skipped,
            on_error=problem,
            on_progress=progressed,
        )

    if plan.convert_originals and not event.is_set():
        convert.convert_to_rat_parallel(
            plan.sources,
            mode=rat_mode,
            subfolder_name=rat_subfolder_name,
            overwrite=rat_overwrite,
            workers=workers,
            cancel_event=event,
            on_result=converted,
            on_skipped=skipped,
            on_error=problem,
            on_progress=progressed,
        )

    for stage in plan.resize_stages:
        if event.is_set():
            break
        resize.resize_to_rung_parallel(
            stage.sources,
            stage.width,
            mode="subfolder",
            output_format=(
                "both"
                if resize_also_rat and plan.lowres_format == "native"
                else plan.lowres_format
            ),
            overwrite=resize_overwrite,
            workers=workers,
            cancel_event=event,
            source_root=plan.root,
            output_root=plan.root,
            on_result=resized,
            on_skipped=resize_skipped,
            on_error=problem,
            on_progress=progressed,
        )

    if generate_thumbnails and not event.is_set():
        thumbnail_paths = final_thumbnail_paths(
            plan,
            rat_mode=rat_mode,
            rat_subfolder_name=rat_subfolder_name,
            resize_also_rat=resize_also_rat,
        )
        thumbnail_paths = [
            path
            for path in thumbnail_paths
            if not thumbs.cached_thumbnail(
                path, size=thumbnail_size, tonemap=thumbnail_tonemap
            )
        ]
        total += len(thumbnail_paths)

        def thumbnail_ready(_source: str, _target: str) -> None:
            summary.thumbnails += 1

        if on_progress is not None:
            on_progress(summary.completed, total)
        thumbs.generate_thumbnails_parallel(
            thumbnail_paths,
            size=thumbnail_size,
            tonemap=thumbnail_tonemap,
            workers=workers,
            cancel_event=event,
            on_result=thumbnail_ready,
            on_error=problem,
            on_progress=progressed,
        )
    return summary, event.is_set()
