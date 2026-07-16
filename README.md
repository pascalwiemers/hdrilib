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

1. Open **Settings** and add one or more HDRI root folders. Roots can be removed or
   reordered, given a shorter display label, and assigned a color swatch with the
   standard color picker. Use the **Formats** menu in each root's row to choose the
   texture extensions scanned for that root; new roots enable all supported formats.
2. Choose the location UI: **Sidebar** shows the folder tree, while **Dropdown** puts a
   compact location picker in the Browse toolbar. The panel switches immediately, and
   root colors decorate folders in both controls.
3. The **Formats** menu on **Browse** shows the persistent format set for the root that
   contains the selected location. Its checked states update when you switch roots, and
   changing them immediately updates that root's Settings entry and scan results. This
   works for both root folders and their subfolders in Sidebar and Dropdown modes.
4. Set the preview size and parallel worker count. The default worker count is the
   smaller of 8 and the machine's CPU count.
5. Return to **Browse**, choose a location, then search by filename or toggle **Include
   subfolders**. Click **Generate thumbnails**; conversions run concurrently and previews
   appear as each finishes. **Cancel** promptly drops pending jobs and terminates active
   converter subprocesses.
6. Select a supported light and double-click a texture:

   - Solaris dome/Karma dome/rect lights use the USD `inputs:texture:file` parameter.
   - OBJ `envlight` nodes use `env_map`.

Settings are stored in `~/.houdini_hdrilib/config.json`; cached PNGs are stored in
`~/.houdini_hdrilib/thumbs/`. Cache keys include source path, modification time, file
size, thumbnail size, and conversion recipe, so edited textures regenerate cleanly.
The version-3 config stores each root's path, label, color, and extension set alongside
the location mode, preview size, and worker count. Existing version-1 and version-2
configs are migrated on load: each root receives the old global `enabled_extensions`
set, version-1 path strings become root objects, and obsolete global master/quick format
fields are removed from the normalized config.
The preview recipe applies a one-stop reduction and Houdini's ACES 1.0 SDR Video view
for an sRGB PNG, which retains useful environment detail without hard-clipping typical
HDR highlights.

## Headless smoke test

The smoke test checks strict version-3 normalization and version-1/version-2 migration,
verifies different roots produce different results with RAT-only and all-format sets,
recursively scans real `.rat` and `.hdr` inputs, imports the panel entry point, generates
several real HDR previews in parallel, and exercises the RAT bridge:

```sh
HFS=/opt/hfs22.0 \
  /opt/hfs22.0/bin/hython tests/smoke.py --source /path/to/hdri
```

On macOS, use the `hython` in your Houdini install's `Resources/bin` directory. The
library never embeds an installation path: it locates the tools through `hou.getenv("HFS")`
or the inherited `$HFS` environment variable. A RAT-only `Could not connect to server`
failure means `iconvert` could not reach the Houdini license server; the smoke test marks
that specific bridge failure as environmental while still requiring all other checks to
pass.
