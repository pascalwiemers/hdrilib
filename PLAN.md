# hdrilib — HDRI / Light-Texture Browser for Houdini (OD-Tools style)

## Goal
A Python Panel for Houdini (22.0+) that browses folders of HDRIs / light textures with
thumbnails. Double-clicking a thumbnail assigns the file to the texture parm of the
currently selected Solaris light (domelight etc.). Must be trivially transferable to
Linux machines (installed as a standard Houdini package, no hardcoded paths).

## Test data
`/Users/pscale/Desktop/hdri` — subfolders `gsg/` and `polyhaven/` containing mixed
`.exr`, `.exr.rat`, `.rat`, `.hdr` files. Good for exercising the format filter.

## Repository layout (Houdini package)
```
hdrilib/
  README.md                      # install instructions macOS + Linux
  hdrilib.json                   # Houdini package file — installed by copying/symlinking
                                 #   into $HOUDINI_USER_PREF_DIR/packages/
  install.py                     # tiny cross-platform installer (writes the package json
                                 #   with the repo path resolved, or symlinks it)
  python/hdrilib/
    __init__.py
    config.py                    # persistent settings
    thumbs.py                    # thumbnail generation + cache
    assign.py                    # apply texture to selected light
    panel.py                     # Qt UI, createInterface() entry point
  python_panels/hdrilib.pypanel  # python panel definition, imports hdrilib.panel
```

## Key design decisions
- **Qt**: import via `from hutil.Qt import QtWidgets, QtCore, QtGui` so it works across
  Houdini's PySide versions (H22 = PySide6, older = PySide2).
- **Config**: JSON at `~/.houdini_hdrilib/config.json` (simple, cross-platform, survives
  Houdini version upgrades). Stores: list of root folders, enabled extension filters,
  thumbnail size, last-used state.
- **Thumbnails**: generated with `$HFS/bin/hoiiotool` (OpenImageIO, ships with Houdini,
  reads .rat/.exr/.hdr) run as background subprocesses from the panel — no TOPs network.
  Roughly: `hoiiotool IN --resize 256x0 --colorconvert linear sRGB -o OUT.png` (Codex to
  verify exact flags against H22's hoiiotool; add a small exposure/tonemap so HDRIs don't
  clip to white). Locate it via `hou.getenv("HFS") + "/bin"` (append `.exe` handling not
  needed; mac/linux only). Fallback: `iconvert`.
  - Cache dir: `~/.houdini_hdrilib/thumbs/`, filename = sha1(abspath + mtime + size).png,
    so cache invalidates when the source changes and never collides.
  - "Generate thumbnails" button scans the current folder tree for files missing thumbs
    and runs a worker (QThread or QProcess pool, few at a time) with a progress bar and
    cancel. Grid updates live as thumbs finish.
- **File discovery**: recursive scan of configured root folders; left side = folder tree
  (roots + subfolders), right side = thumbnail grid (QListView in IconMode, lazy icon
  loading) of the selected folder (option: include subfolders). Text search box filters
  by filename.
- **Format filter**: a set of known extensions (.rat .exr .hdr .tex .tx .png .jpg .jpeg
  .tif .tiff, plus double extensions like .exr.rat handled by suffix matching) shown as
  toggle chips/checkboxes in the toolbar. Only enabled extensions are listed. Persisted
  in config. E.g. enable only `.rat` to hide the source .exr/.hdr files.
- **Assign on double-click** (`assign.py`):
  1. Look at `hou.selectedNodes()` (also check the network editor's current selection).
  2. For LOP nodes: match node type against a table —
     `domelight::*` / `karmadomelight` → parm `xn__inputstexturefile_r3ah`
     (USD `inputs:texture:file`), `light::*` (rect light) → same texture parm if present.
     Prefer a robust approach: iterate the node's parms for one whose name ends with the
     USD-encoded `inputs:texture:file`, fall back to common parm names.
  3. For OBJ context: `envlight` → `env_map`.
  4. Set the parm to the file path; flash a status message in the panel
     ("Assigned foo.rat → /stage/domelight1"). If no light is selected, show a clear
     message instead of failing silently.
  5. Nice-to-have (only if trivial): if nothing is selected and the user is in a LOP
     network, offer right-click menu "Create dome light with this texture".
- **No hard dependencies** beyond Houdini itself. Pure python + hoiiotool subprocess.

## Package file (hdrilib.json)
Uses `$HDRILIB` env var set from the package file's own location or written by
install.py, prepends `python/` to PYTHONPATH and repo root to HOUDINI_PATH so
`python_panels/` is picked up.

## Execution phases
1. **Scaffold + git + GitHub repo** (orchestrator) — done when this file lands.
2. **Implement** — Codex (gpt-5.6-sol, high): all modules above, plus a headless smoke
   test script `tests/smoke.py` runnable with `hython` that: loads config module,
   generates a thumbnail for one file in the test dir, verifies the png exists.
3. **Review** — orchestrator reviews the diff.
4. **Live test** — Codex with computer use drives Houdini: install package, open panel,
   point at /Users/pscale/Desktop/hdri, generate thumbs, select a domelight in Solaris,
   double-click, verify the texture parm; exercise the .rat-only filter. Iterate on bugs.
5. **Polish + docs + push** — README with Linux install steps, commit, push to GitHub.
