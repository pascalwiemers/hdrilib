# HDRI Library for Houdini

A Houdini Python Panel for browsing HDRI and light-texture libraries. Double-click a
thumbnail to assign it to the selected Solaris dome/rect light or OBJ environment
light. No dependencies beyond Houdini itself.

## Features

- Fast thumbnail grid with adjustable display size, grid/list view, and filename search
- Neutral or ACES tone-mapped previews, cached and generated in parallel
- Multiple library folders with per-folder labels, colors, and format filters
- "All HDRIs" aggregate view across every included folder
- Optional grouping of resolution variants (`_1k`/`_4k` suffixes, `4k/` subfolders)
  with a configurable double-click resolution and per-variant copy-path menu
- Batch conversion to mipmapped `.rat` (via `imaketx`) and low-res generation
  (16K/8K/4K/2K/1K rungs), alongside sources or into resolution subfolders
- "Prepare for Library…" one-shot pipeline: convert, downscale, thumbnail, auto-add

## Install

```sh
git clone https://github.com/pascalwiemers/hdrilib.git
cd hdrilib
python3 install.py --version 22.0
```

Restart Houdini, then open **New Pane Tab Type → Misc → HDRI Library**.

See [INSTALL.md](INSTALL.md) for prerequisites, manual package setup, verification
steps, and troubleshooting.

## Use

1. **Settings** → add one or more HDRI folders (subfolders can become their own
   entries). Pick formats, labels, and colors per folder.
2. **Browse** → choose a location, hit **Generate thumbnails**.
3. Select a light (Solaris dome/rect or OBJ `envlight`) and double-click a thumbnail.

Right-click menus hold the batch tools: convert to `.rat`, low-res versions,
thumbnails for a selection, and copy path. Library-wide jobs live in the Settings
folder list's right-click menu.

## State

Everything lives in `~/.houdini_hdrilib/` (settings, thumbnail cache, resolution
cache) and survives Houdini version upgrades. The repository itself is the install —
no files are copied.

## Development

Contributor and agent documentation — repository map, test commands, conventions —
is in [AGENTS.md](AGENTS.md).
