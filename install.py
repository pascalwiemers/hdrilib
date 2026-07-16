#!/usr/bin/env python3
"""Install HDRI Library as a Houdini package on macOS or Linux."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path


PACKAGE_NAME = "hdrilib.json"


def default_packages_dir(version: str) -> Path:
    override = os.environ.get("HOUDINI_USER_PREF_DIR")
    if override:
        return Path(os.path.expandvars(override.replace("__HVER__", version))).expanduser() / "packages"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Preferences" / "houdini" / version / "packages"
    return Path.home() / ("houdini" + version) / "packages"


def package_data(repo_root: Path) -> dict:
    root = str(repo_root.resolve())
    return {
        "enable": True,
        "load_package_once": True,
        "env": [
            {"HDRILIB": root},
            {"PYTHONPATH": {"value": "$HDRILIB/python", "method": "prepend"}},
        ],
        "hpath": "$HDRILIB",
    }


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    os.replace(str(temporary), str(path))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="22.0", help="Houdini major.minor version (default: 22.0)")
    parser.add_argument(
        "--packages-dir",
        type=Path,
        help="Override $HOUDINI_USER_PREF_DIR/packages",
    )
    parser.add_argument(
        "--mode",
        choices=("write", "symlink"),
        default="write",
        help="Write the package directly, or symlink a generated descriptor (default: write)",
    )
    parser.add_argument("--uninstall", action="store_true", help="Remove the installed package file")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if platform.system() not in ("Darwin", "Linux"):
        print("Unsupported platform: {} (macOS and Linux are supported)".format(platform.system()), file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent
    packages_dir = args.packages_dir.expanduser().resolve() if args.packages_dir else default_packages_dir(args.version)
    destination = packages_dir / PACKAGE_NAME

    if args.uninstall:
        try:
            destination.unlink()
            print("Removed {}".format(destination))
        except FileNotFoundError:
            print("Already absent: {}".format(destination))
        return 0

    packages_dir.mkdir(parents=True, exist_ok=True)
    data = package_data(repo_root)
    if args.mode == "write":
        write_json(destination, data)
    else:
        descriptor = Path.home() / ".houdini_hdrilib" / "package" / PACKAGE_NAME
        write_json(descriptor, data)
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                print("Cannot replace directory: {}".format(destination), file=sys.stderr)
                return 2
            destination.unlink()
        try:
            destination.symlink_to(descriptor)
        except OSError as error:
            # Some managed Linux homes disallow symlinks; a copied descriptor is equivalent.
            shutil.copy2(str(descriptor), str(destination))
            print("Symlink unavailable ({}); copied descriptor instead.".format(error))

    print("Installed HDRI Library package: {}".format(destination))
    print("Repository: {}".format(repo_root))
    print("Restart Houdini, then open New Pane Tab Type > HDRI Library.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
