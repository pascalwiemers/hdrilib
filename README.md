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
- Guided **Import** flow that instantly analyzes formats, resolution structure,
  conversion/low-res/thumbnail coverage, and approximate disk size
- One cancellable import job: optionally copy originals to a read-only-source
  destination, convert to mipmapped `.rat`, build 8K/4K/2K/1K rungs, and thumbnail
- Incremental re-import/health checks that skip identical copies and current outputs

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

1. **Import** → choose or drop an HDRI folder, review its analysis, then choose
   in-place organization or a separate library destination and what to generate.
2. Run the plan. The finished folder is configured and selected in **Browse**.
3. Select a light (Solaris dome/rect or OBJ `envlight`) and double-click a thumbnail.

Browse item menus focus on assigning a specific resolution, opening its location,
copying its path, and jumping back to Import for maintenance. **Settings** manages
folder labels, colors, formats, ordering, and application preferences.

## State

Everything lives in `~/.houdini_hdrilib/` (settings, thumbnail cache, resolution
cache) and survives Houdini version upgrades. The repository itself is the install —
no files are copied.

## Development

Contributor and agent documentation — repository map, test commands, conventions —
is in [AGENTS.md](AGENTS.md).
