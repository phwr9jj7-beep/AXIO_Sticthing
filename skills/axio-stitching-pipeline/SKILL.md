---
name: axio-stitching-pipeline
description: >-
  Stitch microscopy tile-scan datasets with AXIO Stitching Studio via its MCP tools or the
  `axio` CLI - Zeiss AND vendor-neutral: Zeiss _info.xml / _meta.xml, Fiji/ImageJ
  TileConfiguration.txt, OME-TIFF stage positions, an explicit positions list, or a bare
  folder of TIFFs with grid-encoded filenames. Estimate canvas size and peak memory before
  committing, apply BaSiCPy / median / spatial shading correction, register tiles by phase
  correlation, SIFT or stage coordinates, and assemble multi-channel, split-channel and 3D
  Z-stack mosaics as ImageJ-compatible TIFFs. Use it for ANY request to stitch, mosaic,
  assemble or flatfield-correct microscope tiles, to read tile-scan metadata, or to QC an
  already-stitched mosaic - never hand-roll a stitching script for that. Also covers wiring
  the pipeline into Claude Code, the ChatGPT desktop app / Codex, and Google Antigravity.
---

# AXIO tile-scan stitching (source checkout)

> **This is the DEV-REPO copy of the skill.** It drives the pipeline through the `axio` CLI
> from a source checkout. The copy installed into an agent by `axio agent install` is
> rendered from this one and drives the **MCP tools** instead, because an installed agent has
> no checkout to `cd` into. Keep the two in sync in spirit; the rendered text lives in
> `axio_stitching/agent_integration.py :: render_installed_skill`.

You drive **AXIO Stitching Studio**'s own pipeline — never a reimplementation. The tool ships
**no LLM code**; the intelligence (reading the dataset, choosing correction and registration,
judging the result) is yours.

**Three failure modes to avoid, all seen in the field:**

1. **Do not write your own stitching script.** No `stitch.py`, no ad-hoc
   `phase_cross_correlation` loop. A hand-rolled script reproduces none of the pipeline's
   feathered blending, channel handling, or Z-stack semantics, and its output is not what the
   desktop app shows the user.
2. **Do not start a stitch you have not sized.** A 5,000-tile scene with three channels and
   forty Z-slices is terabytes of intermediate. `axio estimate` first.
3. **Do not block on a long stitch.** Through MCP, use `axio_start_stitch` + `axio_job_status`.

## 0. Setup, and whenever a run fails on the environment

```bash
conda env create -f environment.yml && conda activate axio_stitching
pip install -e ".[all]"
axio doctor
```

`axio doctor` checks the interpreter, every required package, the optional packages that gate
whole algorithms (**basicpy** for `--correction basicpy`, **OpenCV** for `--algorithm sift`),
the MCP SDK, CPU and RAM, and the free space and writability of the output volume. Resolve
every `✗` before running a pipeline; each check prints its own fix.

Without an editable install, every command below also works as
`python -m axio_stitching.cli <subcommand>` from the repo root.

## 1. Read the dataset (Zeiss OR non-Zeiss)

The `--source` (or MCP `source`) may be any of these — it is auto-detected:

| Source | Example | Positions from |
|---|---|---|
| Zeiss XML | `scan_info.xml` / `scan_meta.xml` | stage coords / meander grid |
| Fiji config | `TileConfiguration.txt` / `.registered.txt` | pixel positions in the file |
| OME-TIFF | a folder of `*.ome.tif` with `Plane PositionX/Y` | embedded stage metadata |
| positions JSON | `{"tiles":[{"filename","x","y"}]}` | you/the user supply them |
| tile folder | filenames like `x00_y01`, `r0c1`, `Position012` | inferred grid + `--overlap` |

```bash
axio inspect --source "D:/data/scan_info.xml"        # Zeiss
axio inspect --source "D:/data/fiji_tiles"           # a folder with TileConfiguration.txt
axio inspect --source "D:/data/ome_tiles" --json     # OME-TIFFs with stage positions
```

(`--xml` is a legacy alias for `--source`.) The report tells you the three facts that decide
every parameter — how many **scenes**, whether tiles are **multi-page** (channels inside each
TIFF → `--ref-channel`) or **split-channel** (one file per channel → `--ref-tag`/`--target-tags`),
and whether there is a **Z** dimension — plus the detected `source_type` and `confidence`.

**Non-Zeiss rules:** a **filename-grid folder** is only an approximate layout
(`confidence: low`) — stitch it with `phase`/`sift`, **not** `coordinate`, and pass `--overlap`
(and `--grid-cols` if filenames carry a linear index). **OME / µm positions** need `--pixel-size-um`
when the source omits `PhysicalSizeX`. The tile TIFFs must sit in the source directory.

## 2. Size the job before committing to it

```bash
axio estimate --source "D:/data/scan_info.xml" --out-dir "D:/out" \
              --correction basicpy --algorithm phase --z-mode mip_align_3d
```

Returns the canvas dimensions, the output size, the estimated **peak RAM**, the intermediate
footprint of the correction pass, a rough wall-clock figure, and a verdict:

| Verdict | What to do |
|---|---|
| `ok` | Proceed. |
| `tight` | Say so and name the headroom before spending an hour. |
| `will_not_fit` | **Do not start.** Narrow the job using the printed advice. |

Narrowing a job never changes the output, only how much is in flight at once: one `--scene`
at a time, `--z-mode mip_output_only` instead of a full volume, or fewer `--target-tags` per
run.

## 3. Validate, then run

```bash
axio validate --source "D:/data/scan_info.xml" --out-dir "D:/out" --correction basicpy --algorithm phase
axio stitch   --source "D:/data/scan_info.xml" --out-dir "D:/out" \
              --correction basicpy --algorithm phase --scene 0
```

`validate` checks that the XML parses and yields scenes, that the tile files it names exist
beside it, that `--out-dir` is writable, and that the packages your chosen correction and
algorithm need are importable. Fix every **error**; read every **warning** — missing tile
files are the common one, and a partially-copied dataset stitches to a canvas full of holes
rather than failing outright.

`stitch` shows a live progress panel; add `--json` for a machine-readable result or `--quiet`
for a silent run. Through MCP, use `axio_start_stitch` and poll `axio_job_status` instead —
a scene takes minutes to hours and a synchronous tool call will simply time out.

## 4. Look at the result before reporting success

A stitch that "succeeded" can still be wrong: a diverged registration produces duplicated or
torn tissue while still exiting zero.

```bash
axio qc "D:/out/stitched_scene0_phase.tif"
axio outputs "D:/out"
```

`qc` streams the mosaic strip by strip (bounded memory, even at gigapixel scale) and reports:

- `empty_fraction` — never-written pixels. High means missing tiles, or a registration that
  flung a tile off-canvas.
- `saturated_fraction` — clipped at the sensor maximum. An acquisition problem, not a
  stitching one.
- `seam_prominence_x` / `_y` — the strongest gradient ridge over the typical gradient. ~1 is
  clean; ≥ 3 means visible seams; ≥ 6 means registration did not converge.
- `findings` — those numbers restated as actionable sentences.

Through MCP, `axio_read_preview` returns the preview thumbnail as an **image you can actually
look at**. Do that too; some failures are obvious to an eye and invisible to a metric.

If registration looks wrong, change **one** thing and re-run: `phase` → `sift` for
low-contrast fluorescence or visible stage drift; `sift` → `coordinate` when the stage is
trustworthy and feature matching finds nothing; `--alignment-mode max_projection` when no
single channel carries enough structure.

## 5. Wire the pipeline into an AI agent

```bash
axio agent status                       # what is detected, installed, or drifted
axio agent install --dry-run            # show every file and config key that would change
axio agent install                      # every agent platform detected on this machine
axio agent install --target claude-code # or codex | antigravity | claude-desktop | gemini-cli
axio agent uninstall                    # removes only what AXIO wrote, hash-verified
```

This installs the rendered skill **and** registers the MCP server. Shared config files
(Codex's `config.toml`, Antigravity's `mcp_config.json`, Claude Desktop's
`claude_desktop_config.json`) are edited surgically: one owned key, backed up before the
first change, written atomically, and removed on uninstall only while it still hashes to what
we wrote. A file the user has since edited is reported as drift and left alone.

See `docs/AGENT_INTEGRATION.md` for the per-platform paths.

## Choosing parameters

**Correction** (`--correction`)

| Value | When |
|---|---|
| `basicpy` | Best quality; visible illumination gradient or vignetting. Slow, needs the `basicpy` package. |
| `median` | Good approximation at a fraction of the cost. The right default for large datasets. |
| `spatial` | Rolling-ball background subtraction — uneven *background*, not uneven *illumination*. |
| `none` | Already flat-fielded, or you are iterating on registration and want speed. |

**Registration** (`--algorithm`)

| Value | When |
|---|---|
| `phase` | Default. Fast and robust when the stage is repeatable and tiles have texture. |
| `sift` | Low contrast, sparse fluorescence, or real stage drift. Needs OpenCV. Slower. |
| `coordinate` | No registration — trust the stage coordinates. Fastest; correct when the stage is accurate or the sample is featureless. |

**Channels**

- Multi-page tiles: `--ref-channel N` picks the channel with the most structure — usually a
  nuclear or autofluorescence channel, rarely a sparse marker.
- Split-channel tiles: `--ref-tag "_c1_" --target-tags "_c2_,_c3_"`. Registration is computed
  **once** on the reference and applied to every target, so the channels stay in register and
  splitting a run across tags costs nothing.
- `--alignment-mode`: `reference` (align on `--ref-channel` alone), `average` or
  `max_projection` (fuse channels first) when no single channel has enough structure.

**Z-stacks** (`--z-mode`)

| Value | Result |
|---|---|
| `none` | 2D only — stitch the first slice. |
| `mip_output_only` | Align in 2D, output a maximum-intensity projection. Cheapest way to see a Z dataset. |
| `mip_align_3d` | Align on the MIP, apply that transform to every slice — full 3D volume out. |
| `ref_slice_3d` | Align on slice `--ref-z-slice`, apply to every slice. Use when one slice is in focus and the MIP is not. |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `BaSiCPy not installed` | `pip install basicpy`, or `--correction median`. |
| `OpenCV not installed for SIFT` | `pip install opencv-python`, or `--algorithm phase`. |
| `No tiles matched reference tag` | `--ref-tag` does not occur in the filenames. Copy a tag from an actual filename in `axio inspect` output. |
| `N tile files missing` (validate warning) | The raw tile TIFFs must sit beside the XML. |
| Memory error mid-run | `axio estimate` said `tight` or `will_not_fit`. Split by scene, or `--z-mode mip_output_only`. |
| Torn or duplicated tissue | Registration diverged. Try `sift`; if that fails too, `coordinate` gives a geometrically honest (if seam-visible) mosaic. |
| Output is mostly empty | Tiles missing, or the wrong `--scene`. Re-check `axio inspect`. |
| `No module named 'mcp.server.fastmcp'` | An MCP SDK 2.0 install. The server supports both APIs; `axio doctor` reports which one is bound. |

## Scope

Microscopy **tile scans** — Zeiss and vendor-neutral (Fiji TileConfiguration, OME-TIFF
stage positions, an explicit positions list, or a grid-encoded tile folder) — and the
mosaics this pipeline produces. Pixel data is read as plain TIFF, so a proprietary
acquisition container that is not already TIFF (`.czi`, `.nd2`, `.lif`, …) must first be
exported to OME-TIFF (Bio-Formats / `bfconvert`) or accompanied by a TileConfiguration or
positions list. **Not** for registering non-tiled images, or for downstream segmentation and
analysis of an already-stitched image.

## Reference

- `references/parameters.md` — every parameter, its legal values, and the JSON result shapes.
- `references/api_spec.md` — full MCP tool input/output schemas.
- `SPEC.md` (repo root) — the normative pipeline specification.
