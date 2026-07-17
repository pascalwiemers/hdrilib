#!/usr/bin/env python3
"""Headless integration smoke test; run with Houdini's hython."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = REPO_ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from hdrilib import config, convert, files, prepare, resize, resolution, thumbs, variants  # noqa: E402
from hdrilib.houdini import executable as houdini_executable  # noqa: E402
import hdrilib.panel as panel  # noqa: E402


def png_dimensions(path: str) -> tuple[int, int]:
    with open(path, "rb") as stream:
        header = stream.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG: {}".format(path)
    assert header[12:16] == b"IHDR", "PNG has no IHDR: {}".format(path)
    return struct.unpack(">II", header[16:24])


def pick_texture(paths, extension):
    candidates = [path for path in paths if path.lower().endswith(extension)]
    if extension == ".rat":
        candidates = [
            path for path in candidates if not path.lower().endswith(".exr.rat")
        ] or candidates
    assert candidates, "no {} test texture found".format(extension)
    return candidates[0]


def environmental_tool_failure(error):
    message = str(error).lower()
    return any(
        text in message
        for text in (
            "license",
            "could not connect to server",
            "incompatible processor",
            "neon",
            "could not find $hfs/bin",
        )
    )


def fixture_inventory(source: Path) -> dict[str, int]:
    counts = {".exr": 0, ".hdr": 0, ".rat": 0}
    if source.is_dir():
        for path in source.rglob("*"):
            suffix = path.suffix.lower()
            if suffix in counts and path.is_file():
                counts[suffix] += 1
    return counts


def fixture_complete(source: Path) -> bool:
    counts = fixture_inventory(source)
    return counts[".exr"] >= 1 and counts[".hdr"] >= 3 and counts[".rat"] >= 1


def resolve_source(explicit: str | None) -> Path | None:
    """Pick the first candidate folder holding a complete fixture set.

    The parallel-thumbnail stage needs at least three HDRs; the RAT bridge and
    EXR probes need one of each. Symlinks into a real library are fine.
    """

    if explicit:
        candidates = [explicit]
    else:
        candidates = [
            os.environ.get("HDRILIB_TEST_DIR"),
            str(config.config_dir() / "smoke-fixtures"),
            "~/Desktop/hdri",
            "/Users/pscale/Desktop/hdri",
        ]
    tried = []
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if fixture_complete(path):
            return path.resolve()
        counts = fixture_inventory(path)
        tried.append(
            "  {} ({})".format(
                path,
                "missing"
                if not path.is_dir()
                else "exr={exr} hdr={hdr} rat={rat}".format(
                    exr=counts[".exr"], hdr=counts[".hdr"], rat=counts[".rat"]
                ),
            )
        )
    print("SMOKE SKIP: no usable fixture folder found. Checked:")
    print("\n".join(tried))
    print(
        "Provide a folder with >=1 .exr, >=3 .hdr and >=1 .rat via --source or"
        " HDRILIB_TEST_DIR, or symlink real textures into {}.".format(
            config.config_dir() / "smoke-fixtures"
        )
    )
    return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=None,
        help="Folder containing real RAT, EXR and HDR textures"
        " (default: $HDRILIB_TEST_DIR, then known fixture folders)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    source = resolve_source(args.source)
    if source is None:
        return 2
    config.config_dir().mkdir(parents=True, exist_ok=True)
    assert panel._format_duration(9) == "9s"
    assert panel._format_duration(130) == "2m 10s"
    assert panel._format_duration(7500) == "2h 5m"

    with tempfile.TemporaryDirectory(prefix="smoke-", dir=str(config.config_dir())) as work:
        work = Path(work)
        config_file = work / "config.json"

        real_exr = next(source.rglob("*.exr"), None)
        real_hdr = next(source.rglob("*.hdr"), None)
        assert real_exr is not None and real_hdr is not None, "need real EXR and HDR fixtures"
        with real_exr.open("rb") as stream:
            exr_dimensions = resolution._exr_dimensions(stream.read(64 * 1024))
        with real_hdr.open("rb") as stream:
            hdr_dimensions = resolution._hdr_dimensions(stream.read(64 * 1024))
        assert exr_dimensions and min(exr_dimensions) > 0
        assert hdr_dimensions and min(hdr_dimensions) > 0

        cached_rat = work / "cached.rat"
        cached_rat.write_bytes(b"not a real RAT")
        resolution.store(cached_rat, 321, 123)
        assert resolution.probe_fast(cached_rat) == (321, 123)
        # Drop process memory to prove the value survives through the JSON cache.
        resolution._MEMORY_CACHE.clear()
        resolution._CACHE_LOADED = False
        assert resolution.probe_fast(cached_rat) == (321, 123)
        cached_stat = cached_rat.stat()
        os.utime(
            cached_rat,
            ns=(cached_stat.st_atime_ns, cached_stat.st_mtime_ns + 1_000_000_000),
        )
        assert resolution.probe_fast(cached_rat) is None

        uncached_rat = work / "uncached.rat"
        uncached_rat.write_bytes(b"unknown")
        started = time.perf_counter()
        assert resolution.probe_fast(uncached_rat) is None
        elapsed = time.perf_counter() - started
        assert elapsed < 0.05, "uncached RAT fast probe took {:.1f}ms".format(elapsed * 1000)
        print(
            "RESOLUTION FAST ok: EXR {}x{}, HDR {}x{}, persistent cache, RAT {:.1f}ms".format(
                *exr_dimensions, *hdr_dimensions, elapsed * 1000
            )
        )

        # Load a literal schema-v1 file to exercise migration rather than merely
        # passing legacy-shaped data through save_config().
        with config_file.open("w", encoding="utf-8") as stream:
            json.dump(
                {
                    "version": 1,
                    "roots": [str(source), str(source)],
                    "enabled_extensions": ["RAT", "HDR"],
                    "thumbnail_size": 384,
                    "include_subfolders": True,
                    "last_folder": str(source),
                    "search_text": "church",
                },
                stream,
            )
        migrated = config.load_config(config_file)
        assert migrated["version"] == config.SCHEMA_VERSION
        assert migrated["roots"] == [
            {
                "path": str(source),
                "label": "",
                "color": "",
                "extensions": [".rat", ".hdr"],
                "include_in_all": True,
            }
        ]
        assert migrated["location_ui_mode"] == "sidebar"
        assert "enabled_extensions" not in migrated
        assert "quick_filter_extensions" not in migrated
        assert migrated["thumbnail_workers"] == config.DEFAULT_THUMBNAIL_WORKERS
        assert migrated["rat_output_mode"] == "alongside"
        assert migrated["rat_subfolder_name"] == "rat"
        assert migrated["rat_overwrite_existing"] is False
        assert migrated["lowres_output_mode"] == "alongside"
        assert migrated["lowres_also_rat"] is False
        assert migrated["lowres_overwrite_existing"] is False
        assert migrated["prepare_lowres_format"] == "both"
        assert migrated["prepare_generate_thumbnails"] is True

        # Version 2 stored richer root objects but still kept both format sets
        # globally. The master enabled set, not its quick-filter subset, seeds roots.
        with config_file.open("w", encoding="utf-8") as stream:
            json.dump(
                {
                    "version": 2,
                    "roots": [
                        {
                            "path": str(source),
                            "label": "Legacy studio",
                            "color": "#123456",
                        }
                    ],
                    "enabled_extensions": ["EXR", "RAT"],
                    "quick_filter_extensions": ["RAT"],
                },
                stream,
            )
        migrated_v2 = config.load_config(config_file)
        assert migrated_v2["version"] == config.SCHEMA_VERSION
        assert migrated_v2["roots"] == [
            {
                "path": str(source),
                "label": "Legacy studio",
                "color": "#123456",
                "extensions": [".exr", ".rat"],
                "include_in_all": True,
            }
        ]

        strict = config.normalise_config(
            {
                "version": 4,
                "roots": [
                    {
                        "path": str(source),
                        "label": "  Studio  ",
                        "color": "#A1B2C3",
                        "extensions": ["EXR", ".rat", ".not-real", 42],
                        "unknown_root_key": True,
                    },
                    str(source),
                    {"path": 12, "label": "bad", "color": "red"},
                    "",
                ],
                "location_ui_mode": "floating",
                "enabled_extensions": ["EXR", ".rat", ".not-real", 42],
                "quick_filter_extensions": ["rat", "hdr", "not-real"],
                "thumbnail_size": 10,
                "thumbnail_workers": 999,
                "rat_output_mode": "elsewhere",
                "rat_subfolder_name": "../bad",
                "rat_overwrite_existing": "yes",
                "lowres_output_mode": "elsewhere",
                "lowres_also_rat": "yes",
                "lowres_overwrite_existing": "yes",
                "prepare_generate_thumbnails": "yes",
                "prepare_lowres_format": "invalid",
                "include_subfolders": "yes",
                "unknown_key": "discard me",
            }
        )
        assert strict["version"] == config.SCHEMA_VERSION
        assert strict["roots"] == [
            {
                "path": str(source),
                "label": "Studio",
                "color": "#a1b2c3",
                "extensions": [".exr", ".rat"],
                "include_in_all": True,
            }
        ]
        assert strict["location_ui_mode"] == "sidebar"
        assert strict["thumbnail_size"] == 64
        assert strict["thumbnail_workers"] == 64
        assert strict["rat_output_mode"] == "alongside"
        assert strict["rat_subfolder_name"] == "rat"
        assert strict["rat_overwrite_existing"] is False
        assert strict["lowres_output_mode"] == "alongside"
        assert strict["lowres_also_rat"] is False
        assert strict["lowres_overwrite_existing"] is False
        assert strict["prepare_generate_thumbnails"] is True
        assert strict["prepare_lowres_format"] == "both"
        assert strict["include_subfolders"] is False
        assert "unknown_key" not in strict
        assert "enabled_extensions" not in strict
        assert "quick_filter_extensions" not in strict
        assert (
            config.normalise_config({"thumbnail_workers": True})["thumbnail_workers"]
            == config.DEFAULT_THUMBNAIL_WORKERS
        )

        rat_root = work / "rat-root"
        all_root = work / "all-root"
        rat_root.mkdir()
        all_root.mkdir()
        for root in (rat_root, all_root):
            (root / "sky.rat").touch()
            (root / "sky.hdr").touch()

        saved = config.save_config(
            {
                "roots": [
                    {
                        "path": str(rat_root),
                        "label": "RAT only",
                        "color": "#336699",
                        "extensions": ["RAT"],
                    },
                    {
                        "path": str(all_root),
                        "label": "All formats",
                        "color": "",
                    },
                ],
                "location_ui_mode": "dropdown",
                "thumbnail_size": 256,
                "thumbnail_workers": 3,
                "rat_output_mode": "subfolder",
                "rat_subfolder_name": "converted-rat",
                "rat_overwrite_existing": True,
                "lowres_output_mode": "subfolder",
                "lowres_also_rat": True,
                "lowres_overwrite_existing": True,
                "prepare_auto_add_subfolders": False,
                "prepare_generate_thumbnails": False,
                "prepare_lowres_format": "rat",
                "include_subfolders": True,
                "last_folder": str(source),
                "search_text": "church",
            },
            config_file,
        )
        loaded = config.load_config(config_file)
        assert loaded == saved, "config save/load round trip changed data"
        assert loaded["roots"][0]["label"] == "RAT only"
        assert loaded["roots"][0]["color"] == "#336699"
        assert loaded["roots"][0]["extensions"] == [".rat"]
        assert loaded["roots"][1]["extensions"] == list(config.DEFAULT_EXTENSIONS)
        assert loaded["rat_output_mode"] == "subfolder"
        assert loaded["rat_subfolder_name"] == "converted-rat"
        assert loaded["rat_overwrite_existing"] is True
        assert loaded["lowres_output_mode"] == "subfolder"
        assert loaded["lowres_also_rat"] is True
        assert loaded["lowres_overwrite_existing"] is True
        assert loaded["prepare_auto_add_subfolders"] is False
        assert loaded["prepare_generate_thumbnails"] is False
        assert loaded["prepare_lowres_format"] == "rat"

        scans = [
            files.scan_files(
                root["path"], extensions=root["extensions"], recursive=True
            )
            for root in loaded["roots"]
        ]
        assert [Path(path).suffix for path in scans[0]] == [".rat"]
        assert {Path(path).suffix for path in scans[1]} == {".rat", ".hdr"}
        print("CONFIG ok: v1/v2 migration, strict v8 normalization, round trip")

        groups = variants.build_groups(
            [
                "/lib/church_8k.hdr",
                "/lib/church_4k.hdr",
                "/lib/studio.exr",
                "/lib/studio_1k.exr",
                "/lib/studio.exr.rat",
                "/lib/studio.rat",
                "/lib/studio_8k.rat",
                "/lib/studio_8k.exr.rat",
                "/lib/4k/studio.hdr",
                "/lib/4k/studio.rat",
                "/lib/4k/studio.hdr.rat",
                "/lib/1k/forest.exr",
                "/lib/forest.exr",
                "/lib/warehouse.rat",
                "/lib/warehouse_4k.rat",
                "/lib/2k/warehouse.rat",
                "/lib/sunset_01.exr",
                "/lib/sunset_02.exr",
            ]
        )
        assert [group.name for group in groups].count("studio") == 1
        assert [group.name for group in groups].count("warehouse") == 1
        by_name = {group.name: group for group in groups}
        assert [v.label for v in by_name["church"].variants] == ["8k", "4k"]
        assert [v.label for v in by_name["studio"].variants] == [
            "native",
            "8k",
            "4k",
            "1k",
        ]
        assert by_name["studio"].variants[0].companions == [
            "/lib/studio.exr.rat",
            "/lib/studio.rat",
        ]
        assert by_name["studio"].variants[1].path == "/lib/studio_8k.rat"
        assert by_name["studio"].variants[1].companions == [
            "/lib/studio_8k.exr.rat"
        ]
        assert by_name["studio"].variants[2].path == "/lib/4k/studio.hdr"
        assert by_name["studio"].variants[2].companions == [
            "/lib/4k/studio.rat",
            "/lib/4k/studio.hdr.rat",
        ]
        studio_files = {
            path
            for variant in by_name["studio"].variants
            for path in [variant.path, *variant.companions]
        }
        assert studio_files == {
            "/lib/studio.exr",
            "/lib/studio_1k.exr",
            "/lib/studio.exr.rat",
            "/lib/studio.rat",
            "/lib/studio_8k.rat",
            "/lib/studio_8k.exr.rat",
            "/lib/4k/studio.hdr",
            "/lib/4k/studio.rat",
            "/lib/4k/studio.hdr.rat",
        }
        assert [v.label for v in by_name["forest"].variants] == ["native", "1k"]
        assert [v.label for v in by_name["warehouse"].variants] == [
            "native",
            "4k",
            "2k",
        ]
        assert variants.pick_variant(by_name["warehouse"], "highest").path == (
            "/lib/warehouse.rat"
        )
        assert len(by_name["sunset_01"].variants) == 1
        assert variants.pick_variant(by_name["church"], "highest").label == "8k"
        assert variants.pick_variant(by_name["church"], "1024").label == "4k"
        assert variants.pick_variant(by_name["studio"], "lowest").label == "1k"
        assert (
            variants.pick_variant(
                by_name["studio"],
                "highest",
                {"/lib/studio.exr": 12000},
            ).path
            == "/lib/studio.exr"
        )
        assert variants.pick_variant(by_name["studio"], "4096").path == (
            "/lib/4k/studio.hdr"
        )
        assert variants.pick_variant(by_name["studio"], "2048").label == "1k"

        rat_subfolder_groups = variants.build_groups(
            [
                "/lib/foo.exr",
                "/lib/rat/foo.rat",
                "/lib/4k/foo.rat",
                "/lib/1k/foo.rat",
                "/lib2/rat/bar.rat",
            ]
        )
        rat_subfolder_by_name = {
            group.name: group for group in rat_subfolder_groups
        }
        foo_group = rat_subfolder_by_name["foo"]
        assert [variant.label for variant in foo_group.variants] == [
            "native",
            "4k",
            "1k",
        ]
        assert foo_group.variants[0].path == "/lib/foo.exr"
        assert foo_group.variants[0].companions == ["/lib/rat/foo.rat"]
        assert variants.pick_variant(foo_group, "highest").path == "/lib/foo.exr"
        assert rat_subfolder_by_name["bar"].variants[0].path == "/lib2/rat/bar.rat"
        assert len(rat_subfolder_by_name["bar"].variants) == 1

        casefolded_rat_group = variants.build_groups(
            ["/case/baz.exr", "/case/RAT/baz.rat"]
        )[0]
        assert casefolded_rat_group.variants[0].companions == [
            "/case/RAT/baz.rat"
        ]
        print("VARIANTS ok: originals, suffix/subfolder rungs, RAT forms, picks")
        print("PER-ROOT SCAN ok: RAT-only root differs from all-formats root")

        target_source = work / "targets" / "sunset.exr"
        expected_alongside = target_source.parent / "sunset.rat"
        expected_subfolder = target_source.parent / "rat-cache" / "sunset.rat"
        assert convert.build_rat_target(target_source, "alongside", "ignored") == expected_alongside
        assert (
            convert.build_rat_target(target_source, "subfolder", "rat-cache")
            == expected_subfolder
        )
        try:
            convert.build_rat_target(target_source, "subfolder", "../escape")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe RAT subfolder was accepted")

        collision_exr = work / "collision" / "sky.exr"
        collision_hdr = work / "collision" / "sky.hdr"
        collision_exr.parent.mkdir()
        collision_exr.write_bytes(b"exr")
        collision_hdr.write_bytes(b"hdr")
        allocated = convert.allocate_rat_targets(
            [collision_exr, collision_hdr], "alongside", "rat"
        )
        assert allocated[str(collision_exr)] == collision_exr.with_suffix(".rat")
        assert allocated[str(collision_hdr)] == collision_hdr.with_name("sky.hdr.rat")
        assert convert.rat_collision_sources(collision_hdr) == [
            str(collision_exr),
            str(collision_hdr),
        ]
        collision_fallback = collision_hdr.with_name("sky.hdr.rat")
        collision_fallback.write_bytes(b"collision-safe target")
        collision_now = time.time()
        os.utime(collision_exr, (collision_now - 10.0, collision_now - 10.0))
        os.utime(collision_hdr, (collision_now - 10.0, collision_now - 10.0))
        os.utime(collision_fallback, (collision_now, collision_now))
        collision_skips = []
        convert_to_rat_result = convert.convert_to_rat_parallel(
            [collision_hdr],
            executable=str(work / "must-not-run-iconvert"),
            on_skipped=lambda source_path, target_path, reason: collision_skips.append(
                (source_path, target_path, reason)
            ),
        )
        assert convert_to_rat_result[:2] == (1, 1)
        assert collision_skips == [
            (str(collision_hdr), str(collision_fallback), "target_newer")
        ]

        skip_source = work / "skip.hdr"
        skip_source.write_bytes(b"source")
        skip_target = convert.build_rat_target(skip_source, "alongside", "rat")
        skip_target.write_bytes(b"newer target")
        now = time.time()
        os.utime(skip_source, (now - 10.0, now - 10.0))
        os.utime(skip_target, (now, now))
        skipped = convert.convert_to_rat(
            skip_source,
            overwrite=False,
            executable=str(work / "must-not-run-iconvert"),
        )
        assert skipped.skipped and skipped.reason == "target_newer"
        legacy_source = work / "legacy.hdr"
        legacy_source.write_bytes(b"source")
        legacy_target = convert.build_legacy_rat_target(
            legacy_source, "alongside", "rat"
        )
        legacy_target.write_bytes(b"legacy target")
        os.utime(legacy_source, (now - 10.0, now - 10.0))
        os.utime(legacy_target, (now, now))
        legacy_skipped = convert.convert_to_rat(
            legacy_source,
            overwrite=False,
            executable=str(work / "must-not-run-iconvert"),
        )
        assert legacy_skipped.skipped and legacy_skipped.target == str(legacy_target)
        already_rat = work / "already.rat"
        already_rat.write_bytes(b"rat")
        skipped_rat = convert.convert_to_rat(
            already_rat, executable=str(work / "must-not-run-iconvert")
        )
        assert skipped_rat.skipped and skipped_rat.reason == "already_rat"
        print("RAT TARGET/SKIP ok: alongside, subfolder, up-to-date, already-RAT")

        imaketx_command = convert.imaketx_rat_command(
            "/hfs/bin/imaketx", "input.exr", "output.rat"
        )
        assert imaketx_command[:3] == ["/hfs/bin/imaketx", "input.exr", "output.rat"]
        assert imaketx_command[imaketx_command.index("--format") + 1] == "RAT"
        assert imaketx_command[imaketx_command.index("--linearize") + 1] == "0"
        assert "--linearmips" in imaketx_command

        assert resize.rungs_below(16384) == (8192, 4096, 2048, 1024)
        assert resize.rungs_below(16385) == resize.STANDARD_RUNGS
        assert resize.rungs_below_largest([1200, 9000, 4000]) == (8192, 4096, 2048, 1024)
        naming_source = work / "variants" / "dome_16K.exr"
        assert resize.build_resize_target(naming_source, 4096, "alongside") == (
            naming_source.parent / "dome_4k.exr"
        )
        assert resize.build_resize_target(naming_source, 4096, "subfolder") == (
            naming_source.parent / "4k" / "dome.exr"
        )
        rat_naming_source = naming_source.with_suffix(".rat")
        assert resize.build_resize_target(rat_naming_source, 2048, "alongside") == (
            rat_naming_source.parent / "dome_2k.rat"
        )
        converted_rat_source = naming_source.with_name("dome_16K.exr.rat")
        assert resize.build_resize_target(converted_rat_source, 2048, "alongside") == (
            converted_rat_source.parent / "dome_2k.rat"
        )
        assert resize.build_resize_target(converted_rat_source, 2048, "subfolder") == (
            converted_rat_source.parent / "2k" / "dome.rat"
        )
        assert resize.build_resize_rat_target(naming_source, 2048, "alongside") == (
            naming_source.parent / "dome_2k.rat"
        )
        resize_allocated = resize.allocate_resize_rat_targets(
            [collision_exr, collision_hdr], 4096, "alongside"
        )
        assert resize_allocated[str(collision_exr)].name == "sky_4k.rat"
        assert resize_allocated[str(collision_hdr)].name == "sky_4k.hdr.rat"
        legacy_resize_source = work / "legacy-resize.exr"
        legacy_resize_source.write_bytes(b"source")
        legacy_resize_target = resize.build_legacy_resize_rat_target(
            legacy_resize_source, 4096, "alongside"
        )
        legacy_resize_target.write_bytes(b"legacy low-res RAT")
        os.utime(legacy_resize_source, (now - 10.0, now - 10.0))
        os.utime(legacy_resize_target, (now, now))
        resolution.store(legacy_resize_source, 9000, 4500)
        legacy_resize_skipped = resize.resize_to_rung(
            legacy_resize_source,
            4096,
            output_format="rat",
        )
        assert legacy_resize_skipped.skipped
        assert legacy_resize_skipped.target == str(legacy_resize_target)
        eligible, too_small = resize.partition_by_width(
            {"16k": 16384, "4k": 4096, "small": 900}, 4096
        )
        assert eligible == ["16k"]
        assert too_small == ["4k", "small"]
        print("LOW-RES LOGIC ok: rungs, suffix/subfolder naming, multi-file skips")

        prepare_root = work / "prepare-root"
        nested = prepare_root / "nested"
        nested.mkdir(parents=True)
        large = nested / "large.exr"
        small = prepare_root / "small.hdr"
        large.touch()
        small.touch()
        plan = prepare.build_pipeline_plan(
            prepare_root,
            [large, small, large],
            convert_to_rat=True,
            rungs=(4096, 2048),
            widths={str(large): 9000, str(small): 1500},
            lowres_format="both",
        )
        assert plan.sources == (str(large), str(small))
        assert plan.rungs == (4096, 2048)
        assert plan.resize_stages[0].sources == (str(large),)
        assert plan.resize_stages[0].targets == (
            str(prepare_root / "4k" / "nested" / "large.exr"),
            str(prepare_root / "4k" / "nested" / "large.rat"),
        )
        assert plan.resize_stages[1].sources == (str(large),)
        assert plan.total == 4  # two original conversions + two combined resizes

        native_plan = prepare.build_pipeline_plan(
            prepare_root,
            [large],
            rungs=(4096,),
            widths={str(large): 9000},
            lowres_format="native",
        )
        rat_only_plan = prepare.build_pipeline_plan(
            prepare_root,
            [large],
            rungs=(4096,),
            widths={str(large): 9000},
            lowres_format="rat",
        )
        both_plan = prepare.build_pipeline_plan(
            prepare_root,
            [large],
            rungs=(4096,),
            widths={str(large): 9000},
            lowres_format="both",
        )
        native_target = str(prepare_root / "4k" / "nested" / "large.exr")
        rat_target = str(Path(native_target).with_suffix(".rat"))
        assert native_plan.resize_stages[0].targets == (native_target,)
        assert rat_only_plan.resize_stages[0].targets == (rat_target,)
        assert native_target not in rat_only_plan.resize_stages[0].targets
        assert both_plan.resize_stages[0].targets == (native_target, rat_target)
        Path(rat_target).parent.mkdir(parents=True, exist_ok=True)
        Path(rat_target).touch()
        rat_thumbnail_paths = prepare.final_thumbnail_paths(rat_only_plan)
        assert rat_thumbnail_paths == [str(large), rat_target]
        assert native_target not in rat_thumbnail_paths
        assert prepare.sensible_rungs(
            [large, small], {str(large): (9000, 4500), str(small): None}
        ) == prepare.PREPARE_RUNGS

        # The final thumbnail stage includes originals and every existing output.
        # RAT inputs also prove that a native low-res RAT and its conversion target
        # collapse to one path.
        thumbnail_rat = prepare_root / "source.rat"
        thumbnail_rat.touch()
        thumbnail_plan = prepare.build_pipeline_plan(
            prepare_root,
            [thumbnail_rat],
            convert_to_rat=True,
            rungs=(4096,),
            widths={str(thumbnail_rat): 9000},
            lowres_format="both",
        )
        thumbnail_lowres = Path(thumbnail_plan.resize_stages[0].targets[0])
        thumbnail_lowres.parent.mkdir(parents=True, exist_ok=True)
        thumbnail_lowres.touch()
        thumbnail_paths = prepare.final_thumbnail_paths(thumbnail_plan)
        assert thumbnail_paths == [str(thumbnail_rat), str(thumbnail_lowres)]

        original_rat = convert.build_rat_target(large, "alongside", "rat")
        original_rat.touch()
        existing_lowres = Path(plan.resize_stages[0].targets[0])
        existing_lowres.parent.mkdir(parents=True, exist_ok=True)
        existing_lowres.touch()
        generated_rat = convert.build_rat_target(existing_lowres, "alongside", "rat")
        generated_rat.touch()
        final_paths = prepare.final_thumbnail_paths(plan)
        assert str(large) in final_paths and str(small) in final_paths
        assert str(existing_lowres) in final_paths
        assert str(original_rat) in final_paths and str(generated_rat) in final_paths
        assert len(final_paths) == len(set(final_paths))
        duplicate_plan = prepare.PipelinePlan(
            str(prepare_root),
            (str(large),),
            False,
            (
                prepare.ResizeStage(4096, (str(large),), (str(large),)),
            ),
            False,
        )
        assert prepare.final_thumbnail_paths(duplicate_plan) == [str(large)]

        parent_root = {
            "path": str(prepare_root),
            "label": "Studio",
            "color": "#123456",
            "extensions": [".exr", ".hdr"],
        }
        generated = prepare.generated_root_entries(
            [parent_root, {"path": str(prepare_root / "4k")}],
            parent_root,
            [
                (prepare_root / "4k", "4k", False),
                (prepare_root / "2k", "2k", False),
                (prepare_root / "rat", "rat", True),
                (prepare_root / "2k", "duplicate", "rat"),
            ],
        )
        assert generated == [
            {
                "path": str(prepare_root / "2k"),
                "label": "Studio 2k",
                "color": "#123456",
                "extensions": [".exr", ".hdr"],
            },
            {
                "path": str(prepare_root / "rat"),
                "label": "Studio rat",
                "color": "#123456",
                "extensions": [".rat"],
            },
        ]
        format_roots = prepare.generated_root_entries(
            [],
            parent_root,
            [
                (prepare_root / "native", "native", "native"),
                (prepare_root / "rat-only", "rat", "rat"),
                (prepare_root / "both", "both", "both"),
            ],
        )
        assert [root["extensions"] for root in format_roots] == [
            [".exr", ".hdr"],
            [".rat"],
            [".exr", ".hdr", ".rat"],
        ]
        generated_4k = prepare_root / "4k"
        generated_4k.mkdir(exist_ok=True)
        (generated_4k / "old.exr").touch()
        scanned = prepare.scan_root(parent_root)
        assert str(large) in scanned and str(small) in scanned
        assert str(generated_4k / "old.exr") not in scanned

        classification_root = work / "classification-root"
        classification_root.mkdir()
        classification_exr = classification_root / "source.exr"
        classification_exr.touch()
        classification = prepare.classify_root_scan(
            {"path": str(classification_root), "extensions": [".exr"]}
        )
        assert classification.state == "has-matching"
        classification = prepare.classify_root_scan(
            {"path": str(classification_root), "extensions": [".rat"]}
        )
        assert classification.state == "hidden-by-filter"
        assert classification.hidden_count == 1
        classification_exr.unlink()
        classification_rat = classification_root / "source.rat"
        classification_rat.touch()
        classification = prepare.classify_root_scan(
            {"path": str(classification_root), "extensions": [".rat"]}
        )
        assert classification.state == "only-rat"
        classification_rat.unlink()
        (classification_root / "notes.txt").touch()
        classification = prepare.classify_root_scan(
            {"path": str(classification_root), "extensions": [".rat"]}
        )
        assert classification.state == "empty"

        original_parallel_rat = convert.convert_to_rat_parallel
        original_parallel_resize = resize.resize_to_rung_parallel
        pipeline_calls = []
        pipeline_progress = []

        def fake_rat(paths, **kwargs):
            values = list(paths)
            pipeline_calls.append(("rat", tuple(values)))
            for index, value in enumerate(values, 1):
                kwargs["on_result"](value, value + ".rat")
                kwargs["on_progress"](index, len(values))
            return len(values), len(values), False

        def fake_resize(paths, width, **kwargs):
            values = list(paths)
            pipeline_calls.append(
                ("resize", width, tuple(values), kwargs.get("output_format"))
            )
            for index, value in enumerate(values, 1):
                target = resize.build_resize_target(
                    value,
                    width,
                    "subfolder",
                    source_root=kwargs["source_root"],
                    output_root=kwargs["output_root"],
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
                kwargs["on_result"](
                    value,
                    resize.ResizeResult(value, (str(target),), "resized"),
                )
                kwargs["on_progress"](index, len(values))
            return len(values), len(values), False

        convert.convert_to_rat_parallel = fake_rat
        resize.resize_to_rung_parallel = fake_resize
        try:
            summary, cancelled = prepare.run_pipeline(
                plan,
                workers=2,
                on_progress=lambda current, total: pipeline_progress.append(
                    (current, total)
                ),
            )
        finally:
            convert.convert_to_rat_parallel = original_parallel_rat
            resize.resize_to_rung_parallel = original_parallel_resize
        assert not cancelled
        assert [call[0] for call in pipeline_calls] == [
            "rat",
            "resize",
            "resize",
        ]
        assert [call[3] for call in pipeline_calls if call[0] == "resize"] == [
            "both",
            "both",
        ]
        assert summary.converted == 2 and summary.resized == 2
        assert summary.completed == plan.total == 4
        assert pipeline_progress[-1] == (4, 4)
        print(
            "PREPARE LOGIC ok: plan/queue, root-level targets, "
            "auto-add inheritance/dedupe"
        )

        rat_files = files.scan_files(source, extensions=[".rat"], recursive=True)
        hdr_files = files.scan_files(source, extensions=[".hdr"], recursive=True)
        assert rat_files and hdr_files, "extension-filtered scan did not find RAT and HDR inputs"
        assert all(path.lower().endswith(".rat") for path in rat_files)
        assert all(path.lower().endswith(".hdr") for path in hdr_files)
        assert not set(rat_files).intersection(hdr_files)
        print("SCAN ok: {} RAT, {} HDR".format(len(rat_files), len(hdr_files)))

        # Exercise an actual 1K resize. On an incompatible CI host Houdini's ARM
        # binaries can stop at their NEON guard before reading the source.
        resize_source = None
        try:
            for candidate in hdr_files:
                if resize.get_resolution(candidate)[0] > 1024:
                    resize_source = candidate
                    break
            assert resize_source, "need one HDR wider than 1K for the resize smoke test"
            copied_resize_source = work / "real-resize.hdr"
            shutil.copy2(resize_source, copied_resize_source)
            resized = resize.resize_to_rung(
                copied_resize_source, 1024, mode="alongside", overwrite=True
            )
            assert not resized.skipped
            resized_path = Path(resized.target)
            assert resized_path.name == "real-resize_1k.hdr"
            assert resized_path.is_file() and resized_path.stat().st_size > 0
            assert resize.get_resolution(resized_path)[0] == 1024
            print("LOW-RES REAL ok: HDR -> {}".format(resized_path))
        except Exception as error:
            if environmental_tool_failure(error):
                print("LOW-RES REAL environmental skip: {}".format(error))
            else:
                raise

        # Force the primary backend so this specifically verifies imaketx, rather
        # than allowing the normal iconvert compatibility fallback.
        imaketx = houdini_executable("imaketx")
        if not imaketx:
            print("IMAKETX RAT REAL environmental skip: could not find $HFS/bin/imaketx")
        else:
            imaketx_source = work / "imaketx-input.hdr"
            shutil.copy2(hdr_files[0], imaketx_source)
            try:
                imaketx_result = convert.convert_to_rat(
                    imaketx_source, overwrite=True, executable=imaketx
                )
                assert not imaketx_result.skipped
                assert Path(imaketx_result.target).is_file()
                assert Path(imaketx_result.target).stat().st_size > 0
                print("IMAKETX RAT REAL ok: {}".format(imaketx_result.target))
            except Exception as error:
                if environmental_tool_failure(error):
                    print("IMAKETX RAT REAL environmental skip: {}".format(error))
                else:
                    raise

        # Deterministically verify that cancellation does not drain the executor's
        # entire input queue. Real converters also receive the same event and are
        # terminated by thumbs._run().
        original_generate = thumbs.generate_thumbnail
        cancel_event = threading.Event()
        started_fake = []
        cancel_result = []

        def cancellable_fake(source_path, **kwargs):
            started_fake.append(source_path)
            event = kwargs["cancel_event"]
            while not event.wait(0.01):
                pass
            raise thumbs.ThumbnailCancelled("cancelled by smoke test")

        thumbs.generate_thumbnail = cancellable_fake
        try:
            coordinator = threading.Thread(
                target=lambda: cancel_result.append(
                    thumbs.generate_thumbnails_parallel(
                        ["pending-{}".format(index) for index in range(20)],
                        workers=2,
                        cancel_event=cancel_event,
                    )
                )
            )
            coordinator.start()
            time.sleep(0.05)
            cancel_event.set()
            coordinator.join(2.0)
        finally:
            thumbs.generate_thumbnail = original_generate
        assert not coordinator.is_alive(), "parallel cancellation did not finish promptly"
        assert cancel_result == [(0, 20, True)]
        assert len(started_fake) <= 2, "cancelled queue continued starting work"
        print("CANCEL ok: pending queue stopped with only {} active jobs".format(len(started_fake)))

        cache_dir = work / "thumbs"
        results = []
        failures = []
        parallel_sources = hdr_files[:4]
        assert len(parallel_sources) >= 3, "need at least three HDR inputs for parallel smoke test"
        callback_threads = set()
        parallel_outputs = {}
        parallel_errors = []
        progress = []
        caller_thread = threading.get_ident()

        def parallel_result(source_path, output):
            callback_threads.add(threading.get_ident())
            parallel_outputs[source_path] = output

        def parallel_error(source_path, error):
            callback_threads.add(threading.get_ident())
            parallel_errors.append((source_path, error))

        completed, total, cancelled = thumbs.generate_thumbnails_parallel(
            parallel_sources,
            size=256,
            workers=3,
            cache_dir=cache_dir,
            force=True,
            on_result=parallel_result,
            on_error=parallel_error,
            on_progress=lambda current, count: progress.append((current, count)),
        )
        assert not cancelled
        assert (completed, total) == (len(parallel_sources), len(parallel_sources))
        assert callback_threads == {caller_thread}, "callbacks escaped the coordinating thread"
        assert progress[-1] == (len(parallel_sources), len(parallel_sources))
        environmental_thumbnail_errors = [
            (source_path, error)
            for source_path, error in parallel_errors
            if environmental_tool_failure(error)
        ]
        if parallel_errors and len(environmental_thumbnail_errors) == len(parallel_errors):
            print(
                "PARALLEL THUMBS environmental skip: {}".format(
                    "; ".join(str(error) for _source_path, error in parallel_errors)
                )
            )
        else:
            assert not parallel_errors, "parallel HDR failures: {}".format(parallel_errors)
            for source_path in parallel_sources:
                output = parallel_outputs[source_path]
                width, height = png_dimensions(output)
                assert width == 256 and 0 < height <= 256, (
                    "unexpected dimensions {}x{}".format(width, height)
                )
                results.append(("HDR", source_path, output, width, height))
            print(
                "PARALLEL THUMBS ok: {} HDR files with 3 workers".format(
                    len(parallel_sources)
                )
            )

        conversion_input = work / "rat-convert-input"
        conversion_input.mkdir()
        conversion_sources = []
        for index, source_path in enumerate(hdr_files[:2]):
            copied = conversion_input / ("input-{}{}".format(index, Path(source_path).suffix))
            shutil.copy2(source_path, copied)
            conversion_sources.append(str(copied))
        assert len(conversion_sources) >= 2, "need at least two HDR/EXR inputs for RAT conversion"
        conversion_outputs = {}
        conversion_skips = []
        conversion_errors = []
        convert_completed, convert_total, convert_cancelled = convert.convert_to_rat_parallel(
            conversion_sources,
            mode="subfolder",
            subfolder_name="rat",
            overwrite=True,
            workers=2,
            on_result=lambda source_path, output: conversion_outputs.setdefault(
                source_path, output
            ),
            on_skipped=lambda source_path, output, reason: conversion_skips.append(
                (source_path, output, reason)
            ),
            on_error=lambda source_path, error: conversion_errors.append(
                (source_path, error)
            ),
        )
        assert not convert_cancelled
        assert (convert_completed, convert_total) == (2, 2)
        environmental_errors = [
            (source_path, error)
            for source_path, error in conversion_errors
            if environmental_tool_failure(error)
        ]
        if conversion_errors and len(environmental_errors) == len(conversion_errors):
            print(
                "PARALLEL RAT environmental skip: {}".format(
                    "; ".join(str(error) for _source_path, error in conversion_errors)
                )
            )
        else:
            assert not conversion_errors, "parallel RAT failures: {}".format(
                conversion_errors
            )
            assert not conversion_skips, "forced RAT conversions were skipped"
            assert set(conversion_outputs) == set(conversion_sources)
            for source_path, output in conversion_outputs.items():
                assert Path(output).name == Path(source_path).stem + ".rat"
                assert Path(output).parent.name == "rat"
                assert Path(output).is_file() and Path(output).stat().st_size > 0
            print("PARALLEL RAT ok: 2 HDR/EXR files with 2 workers")

        rat_source = pick_texture(rat_files, ".rat")
        try:
            output = thumbs.generate_thumbnail(
                rat_source,
                size=256,
                cache_dir=cache_dir,
                force=True,
            )
            width, height = png_dimensions(output)
            assert width == 256 and 0 < height <= 256, "unexpected dimensions {}x{}".format(
                width, height
            )
            results.append(("RAT", rat_source, output, width, height))
            print("THUMB RAT ok: {} -> {} ({}x{})".format(rat_source, output, width, height))
        except Exception as error:
            if environmental_tool_failure(error):
                print("THUMB RAT environmental skip: {}".format(error))
            else:
                failures.append(("RAT", error))
                print("THUMB RAT failed: {}".format(error))

        assert callable(panel.createInterface), "panel createInterface entry point is missing"
        print("PANEL import ok: createInterface() entry point present")
        assert not failures, "thumbnail failures: {}".format(
            "; ".join("{}: {}".format(extension, error) for extension, error in failures)
        )
        print(
            "SMOKE PASS: config migration, filtered scanning, low-res logic/resize, "
            "parallel thumbnails, imaketx RAT write, RAT bridge"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
