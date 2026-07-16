# HDRI Library for Houdini

HDRI Library is a Houdini 22 Python Panel for browsing folders of HDRIs and light
textures. It caches compact previews and assigns a texture to the selected Solaris
dome/rect light or OBJ environment light when you double-click a thumbnail.

It has no dependencies beyond Houdini. Thumbnail conversion uses Houdini's own
`$HFS/bin/hoiiotool`; Houdini's `iconvert` supplies the native RAT reader before the
OpenImageIO resize/display-transform stage. All user state lives in
`~/.houdini_hdrilib` so it survives Houdini version upgrades.

## Install with `install.py`

Clone or copy this repository to a permanent location. The installer writes a small
package descriptor containing that resolved location; the repository itself can stay
anywhere.

macOS:

```sh
cd /path/to/hdrilib
/usr/bin/python3 install.py --version 22.0
```

Linux:

```sh
cd /path/to/hdrilib
python3 install.py --version 22.0
```

By default this installs `hdrilib.json` in:

- macOS: `~/Library/Preferences/houdini/22.0/packages/`
- Linux: `~/houdini22.0/packages/`

If your Houdini preferences are elsewhere, supply the exact package directory:

```sh
python3 install.py --packages-dir /path/to/houdini22.0/packages
```

`--mode symlink` keeps the installed descriptor as a symlink to a generated descriptor
under `~/.houdini_hdrilib/package/`. To remove either installation:

```sh
python3 install.py --version 22.0 --uninstall
```

Restart Houdini after installation. In any pane, choose **New Pane Tab Type > HDRI
Library**.

## Manual package install

Create `hdrilib.json` in `$HOUDINI_USER_PREF_DIR/packages/` with the following content,
replacing `/absolute/path/to/hdrilib` with this repository's absolute path (use forward
slashes on both macOS and Linux):

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

For a centrally managed install, you may instead set `HDRILIB_ROOT` before Houdini starts
and copy the repository's `hdrilib.json` into a scanned package directory. If the
repository itself is included in `HOUDINI_PACKAGE_DIR`, its checked-in package file also
resolves relative to that directory.

## Use

1. Click **Add Folder…** and choose one or more HDRI roots.
2. Select a folder in the tree. Search by filename, toggle **Include subfolders**, or use
   the **Formats** menu to show only formats such as `.rat`.
3. Click **Generate thumbnails**. Missing previews are generated in the background and
   appear as they finish; **Cancel** stops after the current conversion.
4. Select a supported light and double-click a texture:
   - Solaris dome/Karma dome/rect lights use the USD `inputs:texture:file` parameter.
   - OBJ `envlight` nodes use `env_map`.

Settings are stored in `~/.houdini_hdrilib/config.json`; cached PNGs are stored in
`~/.houdini_hdrilib/thumbs/`. Cache keys include source path, modification time, file
size, thumbnail size, and conversion recipe, so edited textures regenerate cleanly.
The preview recipe applies a one-stop reduction and Houdini's ACES 1.0 SDR Video view
for an sRGB PNG, which retains useful environment detail without hard-clipping typical
HDR highlights.

## Headless smoke test

The smoke test performs a real config round trip, recursively scans with separate `.rat`
and `.hdr` filters, imports the panel entry point without constructing a pane, and asks
Houdini's converter to generate 256-pixel PNGs from one real RAT and one real HDR:

```sh
HFS=/opt/hfs22.0 \
  /opt/hfs22.0/bin/hython tests/smoke.py --source /path/to/hdri
```

On macOS, use the `hython` in your Houdini install's `Resources/bin` directory. The
library never embeds an installation path: it locates the tools through `hou.getenv("HFS")`
or the inherited `$HFS` environment variable.
