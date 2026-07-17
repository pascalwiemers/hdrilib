# UX Plan B — Guided Import & Prepare

Goal: the panel restructures around three spaces — **Browse · Import · Settings** —
and the central experience becomes a guided, reusable **import flow**: point the
panel at a (possibly messy) folder of HDRIs, get a brief analysis, choose how to
organize/convert it, run one job, and end with a clean library entry that browses
as stacked resolutions with thumbnails. Adding another HDRI location later reuses
the exact same flow.

## The Import flow (new tab, replaces the scattered prepare actions)

### Step 1 — Point at a folder
Folder picker + drag-friendly empty state. Also reachable from Browse's empty
state ("Add your first HDRI folder…") and from Settings ("Import folder…").

### Step 2 — Analysis (instant, no subprocesses)
Header-probe + scan based summary card of the source, e.g.:
- image count by format (.exr/.hdr/.rat/…), by resolution bucket (16K/8K/4K/…)
- existing structure detection: rat/ conversions present? rung subfolders or
  _NNk suffix variants present? legacy .exr.rat spellings?
- coverage: % with .rat, % with low-res rungs, % thumbnails cached
- approximate disk size, and a "messiness" readout (mixed formats in one folder,
  variants scattered, inconsistent naming) written in plain language.
Uses resolution.probe_fast + the variants grouping only — must render instantly;
unknown resolutions shown as "unknown" without blocking.

### Step 3 — Choose the plan
One page of options, each with a plain-language consequence line:
- **Where**:
  - *Organize in place* — generated files go into rat/ + 4k/… subfolders next to
    the sources (current behavior).
  - *Copy into a library location* — copy originals into a chosen destination
    folder (clean per-image structure), then generate there. The source folder is
    treated strictly read-only in this mode. Destination default remembered.
- **What to generate**: mipmapped .rat of originals (default on), low-res rungs
  (8K/4K/2K/1K checkboxes, sensible ones pre-checked from the analysis), low-res
  output format (native / .rat / both — reuse existing prepare_lowres_format),
  thumbnails (default on).
- **Summary line** before running, e.g. "Copy 62 images (14 GB) to
  /lib/hdri/studio, convert 62 → .rat, generate 4K + 1K (.rat), 186 thumbnails."

### Step 4 — Run
One cancellable job with the existing progress + ETA bar: copy stage (when
chosen) → rat stage → rung stages → thumbnail stage. Reuses prepare.run_pipeline
with a new copy stage; skip logic makes re-runs incremental. Failure summary at
the end if any file failed, with counts.

### Step 5 — Land
The resulting folder is added/updated as a root entry configured so it browses
well immediately: resolution grouping on, per-root formats set to match what was
produced (e.g. .rat-only when fully converted), label defaulted from the folder
name, optional color. A finished import switches to Browse with that folder
selected, thumbnails visible.

Re-running Import on an already-imported root shows the same analysis (now
mostly green) and only proposes what's missing — Import doubles as the library
health/maintenance view (this replaces the old Settings right-click prepare as
the primary path; the context-menu actions remain as shortcuts).

## Browse slims down
Search, formats, grouping/assign-resolution, view mode, refresh. Right-click on
items focuses on use: assign (specific rung submenu), open file location, copy
path, plus a "Re-import / prepare this folder…" jump into Import. Conversion
verbs leave the Browse toolbar; empty state links to Import.

## Settings shrinks
Folders (list, labels, colors, per-root formats, ordering, include-in-all) and
app preferences (thumbnail size/workers/tonemap, location UI mode, display size,
worker count). Conversion/prepare options move to the Import step 3 page and are
persisted as the flow's defaults. Settings keeps an "Import folder…" button.

## Non-goals / constraints
- No deletion or modification of source files ever; copy mode is strictly
  read-only toward the source. No "clean up source" features in this iteration.
- All existing pipeline modules (convert/resize/prepare/thumbs/variants/
  resolution) stay the engine; this is a UX-layer restructure plus a copy stage
  and an analysis module.
- Config schema changes follow the existing normalise/migration pattern.
- Everything must remain instant in the UI path (probe_fast only), cancellable
  in the job path, and cross-platform (macOS/Linux).
