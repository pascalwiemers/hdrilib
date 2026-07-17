"""Group resolution variants of one HDRI without guessing beyond exact rules.

Rules, in confidence order; anything unmatched stays ungrouped:

1. Same folder, same stem after stripping one trailing ``_<n>k`` token
   (covers both our low-res suffix output and vendor names like
   ``church_4k.hdr``).
2. Our rung-subfolder layout: a parent directory named ``<n>k`` is removed
   from the path before comparison, so ``lib/1k/foo.exr`` groups with
   ``lib/foo.exr``.

Both ``foo.rat`` and legacy ``foo.exr.rat`` next to a scanned ``foo.exr`` are
treated as format companions of that variant, never as their own entries.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping

_TOKEN_RE = re.compile(r"[_\-. ]((\d{1,3})k)$", re.IGNORECASE)
_RES_DIR_RE = re.compile(r"^(\d{1,3})k$", re.IGNORECASE)
_RAT_SOURCE_RE = re.compile(r"\.(exr|hdr|png|jpg|jpeg|tif|tiff)\.rat$", re.IGNORECASE)

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


def _identity(path: str) -> tuple[tuple[str, str], str | None]:
    """Return the group key and resolution token for one scanned file."""

    directory, name = os.path.split(path)
    stem, extension = os.path.splitext(name)
    base, token = split_token(stem)
    parent_dir, leaf = os.path.split(directory)
    if token is None and _RES_DIR_RE.match(leaf or ""):
        token = leaf.lower()
        directory = parent_dir
    key = (
        os.path.normcase(directory),
        os.path.normcase(base) + os.path.normcase(extension),
    )
    return key, token


def _variant_sort(variant: Variant) -> tuple[int, int, str]:
    # Native master first, then descending resolution.
    width = token_width(variant.token)
    if width is None:
        return (0, 0, variant.path)
    return (1, -width, variant.path)


def build_groups(paths: Iterable[str]) -> list[Group]:
    """Group scanned paths; single-member groups are returned too."""

    ordered = list(dict.fromkeys(paths))
    available = set(ordered)
    companions: dict[str, list[str]] = {}
    members = []
    for path in ordered:
        match = _RAT_SOURCE_RE.search(path)
        if match and path[: -len(".rat")] in available:
            companions.setdefault(path[: -len(".rat")], []).append(path)
            continue
        if path.lower().endswith(".rat"):
            rat_stem = path[: -len(".rat")]
            source = next(
                (
                    candidate
                    for candidate in ordered
                    if candidate != path
                    and os.path.splitext(candidate)[0] == rat_stem
                    and os.path.splitext(candidate)[1].lower()
                    in (".exr", ".hdr", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
                ),
                None,
            )
            if source is not None:
                companions.setdefault(source, []).append(path)
                continue
        members.append(path)

    grouped: dict[tuple[str, str], list[Variant]] = {}
    order: list[tuple[str, str]] = []
    for path in members:
        key, token = _identity(path)
        variant = Variant(path, token)
        variant.companions = companions.get(path, [])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(variant)

    groups = []
    for key in order:
        variants = sorted(grouped[key], key=_variant_sort)
        stem, _extension = os.path.splitext(os.path.basename(variants[0].path))
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
    return min(
        group.variants,
        key=lambda variant: (abs(width_of(variant) - target), width_of(variant), variant.path),
    )
