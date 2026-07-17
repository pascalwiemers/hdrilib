# Installing hdrilib on a new machine

Step-by-step instructions for installing the HDRI Library Houdini panel. Written so
an agent (or a human in a hurry) can execute them top to bottom and verify each step.
Supported platforms: **Linux and macOS**, Houdini **20.5+** (developed on 22.0).

## 0. Prerequisites — verify before starting

1. **Houdini is installed and licensed.** Find the install:
   - Linux: usually `/opt/hfsXX.Y` (e.g. `/opt/hfs22.0`)
   - macOS: `/Applications/Houdini/HoudiniXX.Y.ZZZ/Frameworks/Houdini.framework/Versions/Current/Resources`
   The directory containing `bin/hython` is referred to as `$HFS` below. Verify:
   ```sh
   "$HFS/bin/hython" -c "import hou; print(hou.applicationVersionString())"
   ```
   This must print a version, not a license error. RAT conversion and thumbnails
   for `.rat` files need a working license server (`hserver`); pure browsing does not.
2. **Git and Python 3** are available (`git --version`, `python3 --version`).
   `install.py` uses only the standard library.
3. Access to the repo: `https://github.com/pascalwiemers/hdrilib` (public).

## 1. Clone

```sh
git clone https://github.com/pascalwiemers/hdrilib.git ~/code/hdrilib
```

Any location works; the package file records an absolute path, so pick the final
location before installing (re-run `install.py` if the repo moves later).

## 2. Install the Houdini package

```sh
cd ~/code/hdrilib
python3 install.py --version 22.0
```

- `--version` is the Houdini major.minor the user runs (`22.0`, `21.0`, …). It only
  selects the preferences folder the package file is written into.
- Default target: `$HOUDINI_USER_PREF_DIR/packages/hdrilib.json` if
  `HOUDINI_USER_PREF_DIR` is set, else `~/houdiniXX.Y/packages/` on Linux,
  `~/Library/Preferences/houdini/XX.Y/packages/` on macOS.
- `--packages-dir /path/to/packages` overrides the target explicitly (use for studio
  setups with custom `HOUDINI_PACKAGE_DIR` locations).
- `--uninstall` removes the package file again.

The generated package file sets `$HDRILIB` to the repo, prepends `$HDRILIB/python`
to `PYTHONPATH`, and adds the repo to `HOUDINI_PATH` (which exposes
`python_panels/hdrilib.pypanel`). No files are copied — the repo is the install.
`--mode symlink` keeps the installed descriptor as a symlink to a generated
descriptor under `~/.houdini_hdrilib/package/`.

### Manual package install (no install.py)

Create `hdrilib.json` in `$HOUDINI_USER_PREF_DIR/packages/` with the following
content, replacing `/absolute/path/to/hdrilib` with this repository's absolute path
(forward slashes on both macOS and Linux):

```json
{
  "enable": true,
  "load_package_once": true,
  "env": [
    { "HDRILIB": "/absolute/path/to/hdrilib" },
    {
      "PYTHONPATH": {
        "value": "$HDRILIB/python",
        "method": "prepend"
      }
    }
  ],
  "hpath": "$HDRILIB"
}
```

For a centrally managed install, set `HDRILIB_ROOT` before Houdini starts and copy
the repository's `hdrilib.json` into a scanned package directory; if the repository
itself is in `HOUDINI_PACKAGE_DIR`, its checked-in package file resolves relative to
that directory.

## 3. Verify headless (before launching the UI)

Run the smoke suite with the machine's own hython:

```sh
cd ~/code/hdrilib
"$HFS/bin/hython" tests/smoke.py
```

Expected: output ends with `SMOKE PASS: ...`. Notes:
- The suite creates scratch data under `~/.houdini_hdrilib/smoke-*` and needs the
  test images it references only if present; it skips gracefully otherwise.
- `iconvert`/`imaketx` steps failing with "Could not connect to server" means the
  license server is unreachable — fix licensing before relying on RAT features.

Then verify the panel constructs (catches Qt/PySide issues without opening Houdini):

```sh
QT_QPA_PLATFORM=offscreen "$HFS/bin/hython" -c "
import sys; sys.path.insert(0, 'python')
from hutil.Qt import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(['t'])
from hdrilib.panel import createInterface
print('OK', type(createInterface()).__name__)"
```

Expected: `OK HDRILibPanel`.

## 4. First run in Houdini

1. Start (or restart) Houdini.
2. Open a pane tab menu → **New Pane Tab Type → Misc → HDRI Library** (searchable
   as "HDRI Library").
3. Go to the **Settings** tab → add one or more root folders of HDRIs/textures.
   Optional per folder: display label, color, and which file formats are shown.
4. Back in **Browse**: pick a folder, press **Generate thumbnails** (parallel;
   progress + ETA in the bar at the bottom of the panel).
5. Assign: in Solaris, select a dome light (or any light with a texture parm; OBJ
   `envlight` also works), then **double-click** a thumbnail.

Library management lives in the Settings tab's folder right-click menu: convert to
`.rat` (mipmapped, via `imaketx`), generate low-res versions (8K/4K/2K/1K into
resolution-named subfolders), thumbnail generation, and **Prepare for Library…**
(one job that converts, downscales, thumbnails, and auto-adds the generated
subfolders to the folder list).

## 5. State and cache locations (per user)

| Path | Contents |
| --- | --- |
| `~/.houdini_hdrilib/config.json` | all panel settings (versioned schema, migrates automatically) |
| `~/.houdini_hdrilib/thumbs/` | thumbnail cache (safe to delete; regenerates) |
| `~/.houdini_hdrilib/resolutions.json` | image-resolution cache (safe to delete) |

Nothing is stored inside the Houdini prefs except the one `hdrilib.json` package file.

## 6. Troubleshooting

- **Panel missing from the pane-tab menu**: package file not loaded — confirm its
  location matches the Houdini version actually launched, and that the `HDRILIB`
  path inside it points at the repo. Restart Houdini after changes.
- **`TypeError: 'NoneType' object is not callable` mentioning QAction**: should not
  happen (the panel resolves QAction from PySide directly); if a variant appears on
  an exotic build, check `python/hdrilib/panel.py::_resolve_qaction`.
- **Thumbnails fail only for `.rat`**: licensing — `.rat` reading/writing uses
  `iconvert`/`imaketx`, which need `hserver` to reach a license server.
- **Everything grayed out in a folder's right-click menu**: the label explains why
  (e.g. files hidden by that folder's format filter) — use "Edit Formats…" in the
  same menu.
- Report issues with the exact console output of step 3.
