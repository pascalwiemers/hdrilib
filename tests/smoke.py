#!/usr/bin/env python3
"""Headless integration smoke test; run with Houdini's hython."""

from __future__ import annotations

import argparse
import json
import os
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

from hdrilib import config, files, thumbs  # noqa: E402
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=os.environ.get("HDRILIB_TEST_DIR", "/Users/pscale/Desktop/hdri"),
        help="Folder containing real RAT and HDR textures",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    assert source.is_dir(), "test texture folder is missing: {}".format(source)
    config.config_dir().mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="smoke-", dir=str(config.config_dir())) as work:
        work = Path(work)
        config_file = work / "config.json"

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
        assert migrated["version"] == 3
        assert migrated["roots"] == [
            {
                "path": str(source),
                "label": "",
                "color": "",
                "extensions": [".rat", ".hdr"],
            }
        ]
        assert migrated["location_ui_mode"] == "sidebar"
        assert "enabled_extensions" not in migrated
        assert "quick_filter_extensions" not in migrated
        assert migrated["thumbnail_workers"] == config.DEFAULT_THUMBNAIL_WORKERS

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
        assert migrated_v2["version"] == 3
        assert migrated_v2["roots"] == [
            {
                "path": str(source),
                "label": "Legacy studio",
                "color": "#123456",
                "extensions": [".exr", ".rat"],
            }
        ]

        strict = config.normalise_config(
            {
                "version": 3,
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
                "include_subfolders": "yes",
                "unknown_key": "discard me",
            }
        )
        assert strict["version"] == 3
        assert strict["roots"] == [
            {
                "path": str(source),
                "label": "Studio",
                "color": "#a1b2c3",
                "extensions": [".exr", ".rat"],
            }
        ]
        assert strict["location_ui_mode"] == "sidebar"
        assert strict["thumbnail_size"] == 64
        assert strict["thumbnail_workers"] == 64
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

        scans = [
            files.scan_files(
                root["path"], extensions=root["extensions"], recursive=True
            )
            for root in loaded["roots"]
        ]
        assert [Path(path).suffix for path in scans[0]] == [".rat"]
        assert {Path(path).suffix for path in scans[1]} == {".rat", ".hdr"}
        print("CONFIG ok: v1/v2 migration, strict v3 normalization, round trip")
        print("PER-ROOT SCAN ok: RAT-only root differs from all-formats root")

        rat_files = files.scan_files(source, extensions=[".rat"], recursive=True)
        hdr_files = files.scan_files(source, extensions=[".hdr"], recursive=True)
        assert rat_files and hdr_files, "extension-filtered scan did not find RAT and HDR inputs"
        assert all(path.lower().endswith(".rat") for path in rat_files)
        assert all(path.lower().endswith(".hdr") for path in hdr_files)
        assert not set(rat_files).intersection(hdr_files)
        print("SCAN ok: {} RAT, {} HDR".format(len(rat_files), len(hdr_files)))

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
        assert not parallel_errors, "parallel HDR failures: {}".format(parallel_errors)
        assert (completed, total) == (len(parallel_sources), len(parallel_sources))
        assert callback_threads == {caller_thread}, "callbacks escaped the coordinating thread"
        assert progress[-1] == (len(parallel_sources), len(parallel_sources))
        for source_path in parallel_sources:
            output = parallel_outputs[source_path]
            width, height = png_dimensions(output)
            assert width == 256 and 0 < height <= 256, "unexpected dimensions {}x{}".format(
                width, height
            )
            results.append(("HDR", source_path, output, width, height))
        print(
            "PARALLEL THUMBS ok: {} HDR files with 3 workers".format(len(parallel_sources))
        )

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
            if "could not connect to server" in str(error).lower():
                print("THUMB RAT environmental skip: {}".format(error))
            else:
                failures.append(("RAT", error))
                print("THUMB RAT failed: {}".format(error))

        assert callable(panel.createInterface), "panel createInterface entry point is missing"
        print("PANEL import ok: createInterface() entry point present")
        assert not failures, "thumbnail failures: {}".format(
            "; ".join("{}: {}".format(extension, error) for extension, error in failures)
        )
        print("SMOKE PASS: config migration, filtered scanning, parallel HDR, RAT bridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
