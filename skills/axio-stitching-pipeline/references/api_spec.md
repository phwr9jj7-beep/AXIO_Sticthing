# AXIO Stitching — MCP tool reference (17 tools)

The authoritative schemas are what the server itself advertises over `tools/list`; this file
is the human-readable map. Every tool returns a JSON string (except `axio_read_preview`,
which returns an image). Parameter semantics and legal values are detailed in
[parameters.md](parameters.md).

## Environment & vocabulary

| Tool | Arguments | Returns |
|---|---|---|
| `axio_doctor` | `out_dir?` | `{ok, summary, checks[{name, status: ok\|warn\|fail, detail, fix}], info}` — run FIRST; optional packages gate whole algorithms. |
| `axio_list_algorithms` | — | `{corrections, algorithms, alignment_modes, z_modes, guidance}` — the exact legal vocabulary. |

## Dataset identification

| Tool | Arguments | Returns |
|---|---|---|
| `axio_detect_source` | `source` | `{source_type: zeiss\|fiji\|ome\|explicit\|grid\|unknown, is_directory, explanation}` — cheap classification when unsure what was handed over. |
| `axio_inspect_dataset` | `source, overlap?, grid_cols?, pixel_size_um?` | `{source_type, confidence, raw_dir, scenes[...], total_scenes, total_tiles, pixel_scale_um, tile_geometry, notes, warnings}`. `tile_geometry` recognizes channels and Z in BOTH representations: `layout` (`multi-page` \| `split-channel` \| `single-channel`), `split_channel_tags` (actual `_cN_` tags), `z_per_file` and `z_slices_from_filenames`, and `recommendations` — the facts restated as the exact parameters to pass. |

## Sizing & validation (run BEFORE stitching)

| Tool | Arguments | Returns |
|---|---|---|
| `axio_estimate_stitch` | `source, out_dir` + stitch params | `{verdict: ok\|tight\|will_not_fit, reasons, advice, scenes[...], totals{peak_ram, output_size, disk_needed, estimated_time}, machine, warnings}` — act on the verdict; `advice` names job-narrowing steps that do not change the output. |
| `axio_validate_stitch` | `source, out_dir, correction?, algorithm?, scene?, ref_tag?, ...` | `{valid, errors, warnings}` — missing tile files are the classic warning. |

## Stitching (background-first)

Shared parameters for the run tools: `source, out_dir, correction, algorithm, scene,
ref_channel, ref_tag, target_tags, alignment_mode, z_mode, ref_z_slice, overlap, grid_cols,
pixel_size_um`.

| Tool | Arguments | Returns |
|---|---|---|
| `axio_start_stitch` | shared params | `{job_id, state, config}` — returns immediately; a real scene takes minutes to hours. |
| `axio_job_status` | `job_id, log_lines?` | `{state: running\|succeeded\|failed\|cancelled\|orphaned, percent, stage, message, elapsed_seconds, log_tail, result, error}`. |
| `axio_job_result` | `job_id` | `{state, result{success, output_paths, preview_paths, duration_seconds, scenes_processed, tiles_processed, error_message}}`. |
| `axio_list_jobs` | `limit?` | `{jobs[...]}` — includes journalled jobs from earlier server sessions. |
| `axio_cancel_job` | `job_id` | `{cancelled, reason, state}` — cooperative; takes effect at the next stage boundary, written output stays. |
| `axio_stitch_sync` | shared params | the final `StitchResult` — ONLY for datasets the estimate says are seconds long. |

## Inspecting results

| Tool | Arguments | Returns |
|---|---|---|
| `axio_read_preview` | `path` (a stitched `.tif` or its `*_preview.png`) | the preview as an IMAGE — look at it before reporting success. |
| `axio_qc_report` | `path, frame?` | `{ok, width, height, dtype, axes, method: full\|streamed, metrics{empty_fraction, saturated_fraction, percentiles, seam_prominence_x/y, ...}, findings[...]}` — memory-bounded even at gigapixel scale. |
| `axio_list_outputs` | `directory` | `{outputs[{path, name, size_bytes, axes, shape, dtype, preview_path}]}` — resume instead of re-running. |

## Handoff & meta

| Tool | Arguments | Returns |
|---|---|---|
| `axio_launch_gui` | `out_dir?, source?, correction?, algorithm?, scene?` | `{launched, app_path, pid, ...}` — opens the desktop app PRE-LOADED with the dataset, the parameters used, and the newest stitched preview. Always pass the config you just ran. |
| `axio_agent_status` | — | `{targets[{target, label, detected, state, units, keys}]}` — how AXIO is wired into the agent platforms on this machine. |

## Output files

```
stitched_scene<N>_<algorithm>.tif              # multi-page dataset (YX / CYX / ZYX / ZCYX)
stitched_scene<N>_<tag>_<algorithm>.tif        # split-channel dataset, one file per tag
stitched_scene<N>_..._preview.png              # 8-bit preview beside each TIFF
intermediate/scene<N>/<method>_corrected/      # corrected tiles (unless correction=none)
```

16-bit, deflate-compressed, ImageJ-compatible TIFFs.
