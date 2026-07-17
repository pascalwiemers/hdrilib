# HDRI Library for Houdini

HDRI Library is a Houdini 22 Python Panel for browsing folders of HDRIs and light
textures. It caches compact previews and assigns a texture to the selected Solaris
dome/rect light or OBJ environment light when you double-click a thumbnail. It can
also batch-convert any enabled source texture format to Houdini `.rat` files.

It has no dependencies beyond Houdini. Thumbnail conversion and low-resolution variants
use Houdini's own `$HFS/bin/hoiiotool`; Houdini's `iconvert` supplies the native RAT
reader when an OpenImageIO operation needs a float EXR bridge. Mipmapped RAT output uses
`$HFS/bin/imaketx`, with `iconvert` retained as a compatibility fallback. All user state
lives in `~/.houdini_hdrilib` so it survives Houdini version upgrades.

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
   smaller of 8 and the machine's CPU count. Thumbnail generation, RAT conversion, and
   low-resolution creation share this worker limit.
5. Return to **Browse**, choose a location, then search by filename or toggle **Include
   subfolders**. Click **Generate thumbnails**; conversions run concurrently and previews
   appear as each finishes. **Cancel** promptly drops pending jobs and terminates active
   converter subprocesses.
6. Convert textures to RAT in either of two ways:

   - Select one or more thumbnails, right-click, and choose **Convert to .rat**.
   - Click **Convert folder to .rat** beside Refresh. This scans the current folder,
     honoring that root's Formats selection and **Include subfolders** state.

   The **RAT Conversion** Settings group writes beside each source by default, or into
   a configurable source subfolder (default `rat`). Targets append `.rat` to the full
   source name (`sky.exr` becomes `sky.exr.rat`) so different source formats do not
   collide. Existing targets at least as new as their sources are skipped unless
   **Overwrite existing** is enabled, and `.rat` inputs are always
   skipped. Conversion shares the thumbnail progress bar and Cancel button; only one
   background batch can run at a time. The grid refreshes when a batch finishes.
   `imaketx` writes a render-ready mip pyramid and automatic sRGB linearization is
   disabled, preserving linear HDR/EXR values and their floating-point depth. If
   `imaketx` is unavailable or fails, `iconvert` supplies the older flat RAT fallback.
7. Create lower-resolution management copies from the standard **16K**, **8K**, **4K**,
   **2K**, and **1K** width rungs:

   - Select one or more thumbnails, right-click, open **Create Low-Res Versions**, and
     choose a rung. The menu only shows rungs below the widest selection. Its counts
     show how many files will resize and how many are already at or below that width.
   - Click **Create low-res…** beside the folder RAT action to process the current
     folder scope, honoring Formats and Include subfolders.

   In **Low-Res Variants** settings, **Alongside source** creates names such as
   `sky_4k.exr`; an existing trailing `_NNk` is replaced instead of stacked.
   **Resolution subfolder** creates `4k/sky.exr`. Outputs keep the source format by
   default. RAT sources are bridged through a temporary float EXR and returned as RAT;
   **Also convert to .rat** keeps the native low-res copy and adds a mipmapped companion
   such as `sky_4k.exr.rat`. Up-to-date outputs are skipped unless overwrite is enabled.
   Resolution probes use pure-Python EXR, HDR, PNG, JPEG, and TIFF header readers on
   UI paths, backed by a persistent cache keyed by path, modification time, and size.
   Houdini subprocess probing is reserved for background jobs and unknown formats.
   Batches use the shared
   worker setting, progress bar, and Cancel button, and refresh the grid on completion.
8. Select a supported light and double-click a texture:

   - Solaris dome/Karma dome/rect lights use the USD `inputs:texture:file` parameter.
   - OBJ `envlight` nodes use `env_map`.

Settings are stored in `~/.houdini_hdrilib/config.json`; cached PNGs are stored in
`~/.houdini_hdrilib/thumbs/`. Cache keys include source path, modification time, file
size, thumbnail size, and conversion recipe, so edited textures regenerate cleanly.
The version-5 config stores each root's path, label, color, and extension set alongside
the location mode, preview size, worker count, RAT output choices, and low-resolution
defaults. Existing version-1 through version-4 configs are migrated on load: each root receives the old
global `enabled_extensions`
set, version-1 path strings become root objects, and obsolete global master/quick format
fields are removed from the normalized config.
The preview recipe applies a one-stop reduction and Houdini's ACES 1.0 SDR Video view
for an sRGB PNG, which retains useful environment detail without hard-clipping typical
HDR highlights.

## Headless smoke test

The smoke test checks strict version-5 normalization and version-1/version-2 migration,
verifies different roots produce different results with RAT-only and all-format sets,
checks low-res rung computation, naming and multi-file skip logic, recursively scans real
`.rat` and `.hdr` inputs, imports the panel entry point, performs a real HDR-to-1K resize,
generates several real HDR previews in parallel, forces one real `imaketx` RAT write,
checks RAT target/skip behavior, converts two temporary inputs to RAT in parallel, and
exercises the RAT-read bridge:

```sh
HFS=/opt/hfs22.0 \
  /opt/hfs22.0/bin/hython tests/smoke.py --source /path/to/hdri
```

On macOS, use the `hython` in your Houdini install's `Resources/bin` directory. The
library never embeds an installation path: it locates the tools through `hou.getenv("HFS")`
or the inherited `$HFS` environment variable. A RAT-only `Could not connect to server`
failure means a Houdini tool could not reach the license server; a NEON/incompatible-CPU
message means the installed Houdini binaries cannot execute on that test host. The smoke
test marks those specific tool-environment failures while still requiring all pure-Python
checks to pass. Resize and RAT-write smoke outputs live only inside the test's temporary
directory, leaving source libraries and existing conversion caches untouched.
