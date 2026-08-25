# AXIO Stitching — parameter and result reference

The authoritative list of legal values is whatever the `axio_list_algorithms` MCP tool (or
`axio version --json`) reports for the installed build. This document explains what each one
*means*.

## Shared parameter set

Every run-shaped tool (`axio_estimate_stitch`, `axio_validate_stitch`, `axio_start_stitch`,
`axio_stitch_sync`) and every equivalent CLI command takes the same parameters.

| Parameter | Type | Default | Legal values | Meaning |
|---|---|---|---|---|
| `xml_path` | string | *required* | — | Absolute path to the Zeiss `_info.xml` or `_meta.xml`. The raw tile TIFFs must sit in the **same directory**. |
| `out_dir` | string | *required* | — | Where stitched TIFFs, previews and correction intermediates are written. Created if absent. |
| `correction` | string | `basicpy` | `basicpy`, `median`, `spatial`, `none` | Shading / flatfield correction applied to every tile before registration. |
| `algorithm` | string | `phase` | `phase`, `sift`, `coordinate` | How tile positions are determined. |
| `scene` | int \| null | `null` | ≥ 0 | Restrict to one scene (0-based). `null` processes every scene in sequence. |
| `ref_channel` | int | `0` | ≥ 0 | For **multi-page** tiles: which channel inside each tile TIFF registration is computed on. |
| `ref_tag` | string | `""` | — | For **split-channel** datasets: the filename substring identifying the reference channel, e.g. `"_c1_"`. Empty means the dataset is multi-page. |
| `target_tags` | string | `""` | — | Comma-separated filename substrings for the other channels, e.g. `"_c2_,_c3_"`. Each is stitched using the reference channel's registration and saved as its own file. |
| `alignment_mode` | string | `reference` | `reference`, `average`, `max_projection` | How multiple channels are fused into the frame registration runs on. |
| `z_mode` | string | `none` | `none`, `mip_align_3d`, `ref_slice_3d`, `mip_output_only` | Z-stack handling. |
| `ref_z_slice` | int | `0` | ≥ 0 | For `ref_slice_3d`: which slice registration is computed on. |

Resolution is always **full** — the pipeline enforces `downsample=1`.

## `correction`

| Value | Cost | Requires | Use when |
|---|---|---|---|
| `basicpy` | High | `basicpy` package | There is a visible illumination gradient or vignetting and quality matters more than time. |
| `median` | Low | — | Default for large datasets. A median-based flatfield approximation; usually good enough. |
| `spatial` | Medium | — | The **background** is uneven (rolling-ball subtraction), as opposed to the illumination. |
| `none` | Zero | — | Tiles are already flat-fielded, or you are iterating on registration and want the fastest loop. |

Correction writes corrected tiles to `<out_dir>/intermediate/scene<N>/<method>_corrected/`.
That directory is roughly the size of the raw dataset — `axio_estimate_stitch` reports it as
`intermediate_size`.

## `algorithm`

| Value | Cost | Requires | Use when |
|---|---|---|---|
| `phase` | Medium | — | Default. Frequency-domain phase correlation with a bounded shift search; robust when the stage is repeatable and tiles carry texture. |
| `sift` | High | `opencv-python` | Feature matching with a Tikhonov-anchored global least-squares solve. For low contrast, sparse fluorescence, or real stage drift. |
| `coordinate` | Zero | — | No registration at all: tiles are placed at their stage coordinates. Fastest, and the correct choice when the stage is accurate or the sample is featureless enough that matching would be noise-driven. |

Both `phase` and `sift` solve a **global** position field rather than chaining pairwise
offsets, so a single bad pair does not propagate along the meander.

## `alignment_mode`

Applies only when a tile carries more than one channel.

| Value | Meaning |
|---|---|
| `reference` | Register on `ref_channel` alone. Default, and correct when one channel has good structure everywhere. |
| `average` | Average the channels, then register on the average. |
| `max_projection` | Max-project across channels, then register. The strongest option when markers are sparse and complementary. |

## `z_mode`

| Value | Registration frame | Output |
|---|---|---|
| `none` | First slice | One 2-D mosaic |
| `mip_output_only` | Maximum-intensity projection | One 2-D mosaic of the projection |
| `mip_align_3d` | Maximum-intensity projection | Full 3-D volume (`ZYX` / `ZCYX`) |
| `ref_slice_3d` | Slice `ref_z_slice` | Full 3-D volume (`ZYX` / `ZCYX`) |

`mip_output_only` is the cheapest way to *see* a Z dataset: it costs the same RAM as a 2-D
stitch and writes one frame instead of `Z` of them.

## Output files

Written into `out_dir`:

```
stitched_scene<N>_<algorithm>.tif              # multi-page dataset
stitched_scene<N>_<tag>_<algorithm>.tif        # split-channel dataset, one per tag
stitched_scene<N>_..._preview.png              # 8-bit downscaled preview beside each TIFF
intermediate/scene<N>/<method>_corrected/      # corrected tiles (unless correction=none)
```

TIFFs are 16-bit, deflate-compressed, ImageJ-compatible, with an `axes` tag of `YX`, `CYX`,
`ZYX` or `ZCYX`.

## Result shapes

### `StitchResult` — `axio_stitch_sync`, `axio_job_result`

```json
{
  "success": true,
  "output_paths": ["D:/out/stitched_scene0_phase.tif"],
  "preview_paths": ["D:/out/stitched_scene0_phase_preview.png"],
  "duration_seconds": 245.3,
  "scenes_processed": 1,
  "tiles_processed": 732,
  "error_message": null
}
```

### Job record — `axio_job_status`

```json
{
  "job_id": "20260825-142233-a1b2c3",
  "state": "running",
  "percent": 42,
  "stage": "alignment",
  "message": "Solving global tile positions...",
  "elapsed_seconds": 128.4,
  "log_tail": ["[ 40%] ...", "[ 42%] ..."],
  "result": null,
  "error": null
}
```

`state` is one of `running`, `succeeded`, `failed`, `cancelled`, or `orphaned` — the last
meaning the process that started the job is gone and its outcome was never recorded.

### Estimate — `axio_estimate_stitch`

```json
{
  "verdict": "tight",
  "reasons": ["peak RAM 21.4 GB is 74% of the 29.0 GB available"],
  "advice": ["stitch one scene at a time (scene=0..3); the largest single scene needs 21.4 GB"],
  "scenes": [{"scene_id": 0, "tiles": 732, "canvas_width": 28900, "canvas_height": 24100, "...": "..."}],
  "totals": {"peak_ram": "21.4 GB", "output_size": "1.3 GB", "disk_needed": "48.2 GB",
             "estimated_time": "1.8 h", "time_confidence": "order-of-magnitude"},
  "machine": {"ram_total": "32.0 GB", "ram_available": "29.0 GB", "disk_free": "310.2 GB"}
}
```

`verdict` is `ok`, `tight`, or `will_not_fit`. Time is explicitly order-of-magnitude — it
scales per-tile constants and should never be quoted as a promise.

### QC — `axio_qc_report`

```json
{
  "ok": true, "width": 28900, "height": 24100, "dtype": "uint16", "method": "streamed",
  "metrics": {
    "mean": 4210.5, "std": 3122.9, "dynamic_range": 61000.0,
    "empty_fraction": 0.081, "saturated_fraction": 0.0004,
    "percentiles": {"p1": 210, "p50": 3300, "p99": 15400, "p99_9": 41000},
    "seam_prominence_x": 1.4, "seam_prominence_y": 7.9,
    "seam_ridges_x": [14201, 3060], "seam_ridges_y": [12040, 1019]
  },
  "findings": ["a hard edge runs across the y axis (seam prominence 7.9x ...)"]
}
```

`method` is `full` for small frames and `streamed` for large ones — the numbers are the same,
the cost is not.

## Errors and what they mean

| Message | Cause | Fix |
|---|---|---|
| `XML file does not exist` | Bad path, or a relative path resolved against the wrong cwd. | Pass an absolute path. |
| `No tiles or scene grid geometry could be extracted` | The XML is not a Zeiss `_info.xml`/`_meta.xml`, or its `<Image>` entries carry no `Bounds`. | Check the file; `axio inspect` prints what it found. |
| `BaSiCPy not installed` | `correction="basicpy"` without the package. | `pip install basicpy`, or use `median`. |
| `OpenCV not installed for SIFT` | `algorithm="sift"` without OpenCV. | `pip install opencv-python`, or use `phase`. |
| `No tiles matched reference tag` | `ref_tag` does not occur in any filename. | Copy a tag from an actual filename in the inspect output. Falls back to using every tile. |
| `N/M tile files missing` (warning) | A partially-copied dataset. | Copy the rest; the stitch would otherwise produce a canvas with holes. |
| `Scene N not found` | `scene` is out of range. | The error names the scenes that do exist. |
