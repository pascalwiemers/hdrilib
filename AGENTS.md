# Agent / contributor notes

Working notes for anyone (human or agent) changing this repository. User-facing
documentation lives in `README.md` and `INSTALL.md`.

## Repository map

```
python/hdrilib/
  panel.py        # Qt UI (hutil.Qt shim: PySide6 on H22, PySide2 earlier); createInterface()
  config.py       # versioned JSON settings, strict normalization (see below)
  files.py        # texture scanning; prunes hidden/NAS junk dirs (@eaDir, .alg_meta, ._*)
  thumbs.py       # thumbnail cache + hoiiotool/iconvert subprocess recipes
  variants.py     # resolution-variant grouping (pure functions, exact rules only)
  resize.py       # low-res rung naming and targets (_4k suffix / 4k/ subfolder)
  resolution.py   # pure-Python header probes + persistent resolution cache
  convert.py      # RAT conversion (imaketx, iconvert fallback)
  prepare.py      # multi-stage pipeline (convert + downscale + thumbnails)
  assign.py       # apply texture to selected light (LOP texture parm / OBJ env_map)
  houdini.py      # $HFS tool discovery, cancellable subprocess, OCIO env pinning
  jobs.py         # parallel executor with cancellation + main-thread callbacks
python_panels/hdrilib.pypanel
tests/smoke.py    # the entire test suite; run it after every change
install.py        # writes the Houdini package descriptor
```

## Running the tests

```sh
HFS=/opt/hfs22.0 python3 tests/smoke.py          # plain python3 works for most sections
"$HFS/bin/hython" tests/smoke.py                  # full fidelity
```

- Fixtures resolve from `--source`, `$HDRILIB_TEST_DIR`, then
  `~/.houdini_hdrilib/smoke-fixtures/` (symlinks into a real library are fine).
  A usable folder needs >= 1 `.exr`, >= 3 `.hdr`, >= 1 `.rat`; otherwise the suite
  exits 2 with instructions instead of failing.
- Expected output ends with `SMOKE PASS: ...`.
- License-server failures ("Could not connect to server") only affect
  `iconvert`/`imaketx` sections and are reported as environmental.

UI changes can be exercised without a Houdini session:

```sh
QT_QPA_PLATFORM=offscreen "$HFS/bin/hython" -c "
import sys; sys.path.insert(0, 'python')
from hutil.Qt import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(['t'])
from hdrilib.panel import createInterface
w = createInterface(); w.resize(1200, 800); w.show()
w.grab().save('/tmp/panel.png')"
```

Set `HDRILIB_CONFIG_DIR` to a scratch path in such scripts so real user settings
stay untouched. Widget methods (`_populate_grid`, `_add_root`, …) can be driven
directly after seeding `widget._settings`.

## Conventions that bite if missed

- **Config schema is strict.** `config.normalise_config` drops every key it does
  not recognize. A new setting needs: a `DEFAULT_CONFIG` entry, a validation
  clause in `normalise_config` (and `_normalise_root` for per-root keys), and an
  update to the exact-dict assertions in `tests/smoke.py`.
- **Thumbnail cache keys** hash path, mtime, size, thumb size, tone map, and
  `THUMBNAIL_RECIPE`. Any change to conversion output must bump the recipe string
  so stale PNGs regenerate.
- **OCIO**: the ACES recipe uses Houdini's shipped config's color-space names. A
  site `$OCIO` (e.g. an ACES 1.2 studio config) renames everything, so converter
  subprocesses pin `OCIO` to `$HFS/packages/ocio/houdini-config-*.ocio`
  (`houdini.houdini_ocio_environment`). The neutral tone map needs no OCIO.
- **RAT reading costs a license.** `iconvert`/`imaketx` check out a Houdini
  license; thumbnails prefer a RAT's neighboring original
  (`thumbs.rat_sibling_source`) and only bridge through `iconvert` for lone RATs.
- **Scanning must stay junk-free.** All directory walks go through
  `files._wanted_directory` / `_wanted_file` (hidden dirs, `@eaDir`, `#recycle`,
  AppleDouble `._*` files). Do not add a raw `rglob`/`scandir` walk without them.
- **Qt compatibility**: import Qt via `hutil.Qt`, resolve enums with the module's
  `_enum` helper, and never rely on `QIcon` upscaling (it silently caps at the
  pixmap's native size — icons are pre-scaled in `panel._display_icon`).
- **Variant grouping is exact-match only** (`variants.py`): trailing `_<n>k`
  token in the same folder, or a `<n>k/` parent directory. Anything fuzzier was
  deliberately rejected — leave unmatched files ungrouped rather than guessing.

## Machine-independence rules

- Never embed an installation path; tools resolve through `hou.getenv("HFS")` or
  the inherited `$HFS`.
- All user state stays under `~/.houdini_hdrilib/` (override:
  `HDRILIB_CONFIG_DIR`).
- Supported platforms: Linux and macOS.
