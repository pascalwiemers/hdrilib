"""HDRI Library for Houdini.

The non-UI modules deliberately remain importable in plain Python so file discovery,
configuration, and thumbnail command construction can be tested independently.
"""

from .config import DEFAULT_EXTENSIONS, load_config, save_config
from .convert import build_rat_target, convert_to_rat, convert_to_rat_parallel
from .files import extension_for, matches_extension, scan_files

__all__ = [
    "DEFAULT_EXTENSIONS",
    "build_rat_target",
    "convert_to_rat",
    "convert_to_rat_parallel",
    "extension_for",
    "load_config",
    "matches_extension",
    "save_config",
    "scan_files",
]

__version__ = "0.3.0"
