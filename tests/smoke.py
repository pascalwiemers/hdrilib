#!/usr/bin/env python3
"""Headless integration smoke test; run with Houdini's hython."""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
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
        candidates = [path for path in candidates if not path.lower().endswith(".exr.rat")] or candidates
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
        saved = config.save_config(
            {
                "roots": [str(source)],
                "enabled_extensions": ["RAT"],
                "thumbnail_size": 256,
                "include_subfolders": True,
                "last_folder": str(source),
                "search_text": "church",
            },
            config_file,
        )
        loaded = config.load_config(config_file)
        assert loaded == saved, "config save/load round trip changed data"
        assert loaded["enabled_extensions"] == [".rat"]
        print("CONFIG ok: {}".format(config_file))

        rat_files = files.scan_files(source, extensions=[".rat"], recursive=True)
        hdr_files = files.scan_files(source, extensions=[".hdr"], recursive=True)
        assert rat_files and hdr_files, "extension-filtered scan did not find RAT and HDR inputs"
        assert all(path.lower().endswith(".rat") for path in rat_files)
        assert all(path.lower().endswith(".hdr") for path in hdr_files)
        assert not set(rat_files).intersection(hdr_files)
        print("SCAN ok: {} RAT, {} HDR".format(len(rat_files), len(hdr_files)))

        cache_dir = work / "thumbs"
        results = []
        failures = []
        for extension, source_path in (
            ("HDR", pick_texture(hdr_files, ".hdr")),
            ("RAT", pick_texture(rat_files, ".rat")),
        ):
            try:
                output = thumbs.generate_thumbnail(
                    source_path,
                    size=256,
                    cache_dir=cache_dir,
                    force=True,
                )
                width, height = png_dimensions(output)
                assert width == 256 and 0 < height <= 256, "unexpected dimensions {}x{}".format(
                    width, height
                )
                results.append((extension, source_path, output, width, height))
                print("THUMB {} ok: {} -> {} ({}x{})".format(extension, source_path, output, width, height))
            except Exception as error:
                failures.append((extension, error))
                print("THUMB {} failed: {}".format(extension, error))

        assert callable(panel.createInterface), "panel createInterface entry point is missing"
        print("PANEL import ok: createInterface() entry point present")
        assert not failures, "thumbnail failures: {}".format(
            "; ".join("{}: {}".format(extension, error) for extension, error in failures)
        )
        print("SMOKE PASS: config, filtered scanning, RAT thumbnail, HDR thumbnail")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
