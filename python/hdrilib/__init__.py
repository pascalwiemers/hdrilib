"""HDRI Library for Houdini.

The non-UI modules deliberately remain importable in plain Python so file discovery,
configuration, and thumbnail command construction can be tested independently.
"""

from .config import DEFAULT_EXTENSIONS, load_config, save_config
from .files import extension_for, matches_extension, scan_files

__all__ = [
    "DEFAULT_EXTENSIONS",
    "extension_for",
    "load_config",
    "matches_extension",
    "save_config",
    "scan_files",
]

__version__ = "0.2.0"
