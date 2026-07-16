"""Assign a texture to a selected Houdini light without UI dependencies."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

try:
    import hou  # type: ignore
except ImportError:  # Safe for documentation, linting, and plain-Python imports.
    hou = None


LOP_TEXTURE_PARMS = (
    "xn__inputstexturefile_r3ah",
    "inputs_texture_file",
    "texture_file",
    "texture",
)
OBJ_TEXTURE_PARMS = ("env_map", "envmap")


@dataclass(frozen=True)
class AssignmentResult:
    success: bool
    message: str
    node_path: str = ""
    parm_name: str = ""


def _node_type_name(node: Any) -> str:
    try:
        return node.type().name().lower()
    except (AttributeError, RuntimeError):
        return ""


def _node_category_name(node: Any) -> str:
    try:
        return node.type().category().name().lower()
    except (AttributeError, RuntimeError):
        return ""


def _node_path(node: Any) -> str:
    try:
        return node.path()
    except (AttributeError, RuntimeError):
        return "<unknown>"


def _selected_nodes() -> list[Any]:
    if hou is None:
        return []
    result = []
    try:
        result.extend(hou.selectedNodes())
    except (AttributeError, RuntimeError):
        pass

    # selectedNodes() is normally sufficient. This also covers pane-local selections
    # in custom desktop configurations where the global selection has not caught up.
    try:
        for pane in hou.ui.paneTabs():
            if pane.type() != hou.paneTabType.NetworkEditor:
                continue
            network = pane.pwd()
            for node in network.selectedChildren() if network else ():
                if node not in result:
                    result.append(node)
    except (AttributeError, RuntimeError, hou.Error):
        pass
    return result


def _is_light(node: Any) -> bool:
    name = _node_type_name(node)
    return "light" in name or name == "envlight"


def _find_named_parm(node: Any, names: Iterable[str]) -> Any | None:
    for name in names:
        try:
            parm = node.parm(name)
        except (AttributeError, RuntimeError):
            parm = None
        if parm is not None:
            return parm
    return None


def _find_lop_texture_parm(node: Any) -> Any | None:
    parm = _find_named_parm(node, LOP_TEXTURE_PARMS)
    if parm is not None:
        return parm
    try:
        parms = node.parms()
    except (AttributeError, RuntimeError):
        return None
    for candidate in parms:
        try:
            name = candidate.name().lower()
            label = candidate.parmTemplate().label().lower()
        except (AttributeError, RuntimeError):
            continue
        compact = "".join(character for character in name if character.isalnum())
        if compact.endswith("inputstexturefile") or "inputstexturefile" in compact:
            return candidate
        if label in ("texture file", "environment map"):
            return candidate
    return None


def find_texture_parm(node: Any) -> Any | None:
    """Return the supported texture parameter for a light-like node."""

    if not _is_light(node):
        return None
    category = _node_category_name(node)
    name = _node_type_name(node)
    if category.startswith("obj") or name == "envlight":
        return _find_named_parm(node, OBJ_TEXTURE_PARMS)
    if category.startswith("lop") or "domelight" in name or "karmadomelight" in name:
        return _find_lop_texture_parm(node)
    return None


def assign_texture(path: str, nodes: Iterable[Any] | None = None) -> AssignmentResult:
    """Assign *path* to the first selected, supported light."""

    texture = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(texture):
        return AssignmentResult(False, "Texture file does not exist: {}".format(texture))
    candidates = list(nodes) if nodes is not None else _selected_nodes()
    if not candidates:
        return AssignmentResult(False, "Select a Solaris dome/rect light or OBJ environment light first.")

    light_seen = False
    for node in candidates:
        if not _is_light(node):
            continue
        light_seen = True
        parm = find_texture_parm(node)
        if parm is None:
            continue
        try:
            parm.set(texture)
            node_path = _node_path(node)
            return AssignmentResult(
                True,
                "Assigned {} → {}".format(os.path.basename(texture), node_path),
                node_path,
                parm.name(),
            )
        except Exception as error:
            return AssignmentResult(
                False,
                "Could not assign {}: {}".format(_node_path(node), error),
                _node_path(node),
            )

    if light_seen:
        message = "The selected light has no supported texture parameter."
    else:
        message = "Select a Solaris dome/rect light or OBJ environment light first."
    return AssignmentResult(False, message)
