"""Group resolution variants of one HDRI without guessing beyond exact rules.

Rules, in confidence order; anything unmatched stays ungrouped:

1. Same folder, same stem after stripping one trailing ``_<n>k`` token
   (covers both our low-res suffix output and vendor names like
   ``church_4k.hdr``).
2. Our rung-subfolder layout: a parent directory named ``<n>k`` is removed
   from the path before comparison, so ``lib/1k/foo.exr`` groups with
   ``lib/foo.exr``.

The source format is not part of the group key, so RAT-only rungs still join a
native EXR/HDR master.  Both ``foo.rat`` and legacy ``foo.exr.rat`` next to a
scanned ``foo.exr`` are treated as format companions of that variant, never as
their own entries.  A RAT in the configured RAT output subfolder is likewise a
native companion when that subfolder's parent is part of the same scan.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping

from . import config

_TOKEN_RE = re.compile(r"[_\-. ]((\d{1,3})k)$", re.IGNORECASE)
_RES_DIR_RE = re.compile(r"^(\d{1,3})k$", re.IGNORECASE)
_RAT_SOURCE_RE = re.compile(r"\.(exr|hdr|png|jpg|jpeg|tif|tiff)\.rat$", re.IGNORECASE)
_SOURCE_EXTENSIONS = (".exr", ".hdr", ".png", ".jpg", ".jpeg", ".tif", ".tiff")

ASSIGN_CHOICES = ("highest", "lowest", "1024", "2048", "4096", "8192", "16384")
DEFAULT_ASSIGN = "highest"


def split_token(stem: str) -> tuple[str, str | None]:
    """Return ``(base, token)`` where token is a trailing resolution like ``4k``."""

    match = _TOKEN_RE.search(stem)
    if not match:
        return stem, None
    return stem[: match.start()], match.group(1).lower()


def token_width(token: str | None) -> int | None:
    if not token:
        return None
    return int(token[:-1]) * 1024


class Variant:
    __slots__ = ("path", "token", "companions")

    def __init__(self, path: str, token: str | None):
        self.path = path
        self.token = token
        self.companions: list[str] = []

    @property
    def label(self) -> str:
        return self.token or "native"


class Group:
    __slots__ = ("name", "variants")

    def __init__(self, name: str, variants: list[Variant]):
        self.name = name
        self.variants = variants

    @property
    def paths(self) -> list[str]:
        return [variant.path for variant in self.variants]

    def badge(self) -> str:
        return " · ".join(variant.label for variant in self.variants)


def _logical_stem(path: str) -> str:
    """Return a stem with RAT's optional embedded source extension removed."""

    stem, extension = os.path.splitext(os.path.basename(path))
    if extension.lower() == ".rat":
        embedded_stem, embedded_extension = os.path.splitext(stem)
        if embedded_extension.lower() in _SOURCE_EXTENSIONS:
            stem = embedded_stem
    return stem


def _identity(
    path: str,
    rat_subfolder_name: str | None = None,
    rat_parent_directories: set[str] | None = None,
) -> tuple[tuple[str, str], str | None]:
    """Return the group key and resolution token for one scanned file."""

    if rat_subfolder_name is None:
        rat_subfolder_name = str(config.DEFAULT_CONFIG["rat_subfolder_name"])
    directory, name = os.path.split(path)
    stem = _logical_stem(name)
    base, token = split_token(stem)
    parent_dir, leaf = os.path.split(directory)
    if (
        token is None
        and os.path.splitext(name)[1].lower() == ".rat"
        and leaf.casefold() == rat_subfolder_name.casefold()
        and (
            rat_parent_directories is None
            or os.path.normcase(parent_dir) in rat_parent_directories
        )
    ):
        directory = parent_dir
    elif token is None and _RES_DIR_RE.match(leaf or ""):
        token = leaf.lower()
        directory = parent_dir
    key = (
        os.path.normcase(directory),
        os.path.normcase(base),
    )
    return key, token


def _variant_sort(variant: Variant) -> tuple[int, int, str]:
    # Native master first, then descending resolution.
    width = token_width(variant.token)
    if width is None:
        return (0, 0, variant.path)
    return (1, -width, variant.path)


def build_groups(
    paths: Iterable[str], rat_subfolder_name: str | None = None
) -> list[Group]:
    """Group scanned paths; single-member groups are returned too."""

    if rat_subfolder_name is None:
        rat_subfolder_name = str(config.DEFAULT_CONFIG["rat_subfolder_name"])
    ordered = list(dict.fromkeys(paths))
    scanned_directories = {
        os.path.normcase(os.path.dirname(path)) for path in ordered
    }
    identities = {
        path: _identity(path, rat_subfolder_name, scanned_directories)
        for path in ordered
    }
    companions: dict[str, list[str]] = {}
    claimed_companions: set[str] = set()

    # Match companions by logical identity instead of their physical path.  This
    # lets ``rat/foo.rat`` match ``foo.exr`` while retaining exact same-directory
    # and legacy ``foo.exr.rat`` behavior.
    buckets: dict[tuple[tuple[str, str], str | None, str], list[str]] = {}
    for path in ordered:
        key, token = identities[path]
        logical_base, _logical_token = split_token(_logical_stem(path))
        signature = (key, token, os.path.normcase(logical_base))
        buckets.setdefault(signature, []).append(path)

    for bucket in buckets.values():
        sources = [
            path
            for path in bucket
            if os.path.splitext(path)[1].lower() in _SOURCE_EXTENSIONS
        ]
        rats = [path for path in bucket if path.lower().endswith(".rat")]
        if sources:
            for rat in rats:
                # Preserve the legacy spelling's exact source association when
                # more than one source format shares this logical identity.
                embedded_source = rat[: -len(".rat")]
                source = embedded_source if embedded_source in sources else sources[0]
                companions.setdefault(source, []).append(rat)
                claimed_companions.add(rat)
            continue
        if len(rats) < 2:
            continue
        primary = min(
            rats,
            key=lambda path: (
                bool(_RAT_SOURCE_RE.search(path)),
                ordered.index(path),
            ),
        )
        for alias in rats:
            if alias != primary:
                companions.setdefault(primary, []).append(alias)
                claimed_companions.add(alias)

    members = [path for path in ordered if path not in claimed_companions]

    grouped: dict[tuple[str, str], list[Variant]] = {}
    order: list[tuple[str, str]] = []
    for path in members:
        key, token = identities[path]
        variant = Variant(path, token)
        variant.companions = companions.get(path, [])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(variant)

    groups = []
    for key in order:
        variants = sorted(grouped[key], key=_variant_sort)
        stem = _logical_stem(variants[0].path)
        base, _token = split_token(stem)
        name = base.rstrip("_-. ") or stem
        groups.append(Group(name, variants))
    return groups


def pick_variant(
    group: Group,
    preference: str | None = None,
    widths: Mapping[str, int] | None = None,
) -> Variant:
    """Choose the variant a double-click should assign.

    ``widths`` supplies probed pixel widths by path; filename tokens fill the
    gaps and a tokenless master counts as the largest member.
    """

    preference = preference if preference in ASSIGN_CHOICES else DEFAULT_ASSIGN
    widths = widths or {}

    def width_of(variant: Variant) -> int:
        probed = widths.get(variant.path)
        if probed:
            return int(probed)
        return token_width(variant.token) or (1 << 30)

    if preference == "lowest":
        return min(group.variants, key=lambda variant: (width_of(variant), variant.path))
    if preference == "highest":
        return max(group.variants, key=lambda variant: (width_of(variant), variant.path))
    target = int(preference)
    exact = [variant for variant in group.variants if token_width(variant.token) == target]
    if exact:
        return min(exact, key=lambda variant: variant.path)
    return min(
        group.variants,
        key=lambda variant: (abs(width_of(variant) - target), width_of(variant), variant.path),
    )
