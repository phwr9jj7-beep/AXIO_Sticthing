"""
mcp_server.py — the AXIO Stitching MCP (Model Context Protocol) server.

Exposes the pipeline's OWN operations as typed MCP tools over stdio, so an external agent
can inspect, size, run and QC a Zeiss tile-scan stitch while driving exactly the same engine
the desktop app drives. It contains **no LLM code**: it is a tool PROVIDER, not an agent.
Every tool is a thin wrapper over :mod:`axio_stitching.engine`, :mod:`axio_stitching.estimate`,
:mod:`axio_stitching.qc` and :mod:`axio_stitching.jobs`, shared with the ``axio`` CLI so the
two surfaces never diverge.

Run it as::

    python -m axio_stitching.mcp_server      # source / venv install
    AXIO_Stitching_Studio.exe --mcp-serve    # frozen desktop build

SDK compatibility
-----------------
The MCP Python SDK renamed its high-level server class from
``mcp.server.fastmcp.FastMCP`` (1.x) to ``mcp.server.MCPServer`` (2.0), and both are widely
installed. This module binds whichever is present; :func:`axio_stitching.doctor.detect_mcp_flavour`
reports which one a given environment will use.

Long-running work
-----------------
A real scene takes minutes to hours, which no MCP client will wait for synchronously, so the
stitch tools are **asynchronous by default**: ``axio_start_stitch`` returns a job id and
``axio_job_status`` polls it. ``axio_stitch_sync`` exists only for datasets small enough to
finish inside a single tool call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .models import AlignmentMode, CorrectionMethod, StitchAlgorithm, StitchConfig, ZMode

# ---------------------------------------------------------------------------
# SDK binding — FastMCP (1.x) or MCPServer (2.0+)
# ---------------------------------------------------------------------------

_SERVER_CLASS: Any
_IMAGE_CLASS: Any
_SDK_API: str

try:  # SDK >= 2.0
    from mcp.server import MCPServer as _SERVER_CLASS  # type: ignore[no-redef]
    from mcp.server.mcpserver import Image as _IMAGE_CLASS  # type: ignore[no-redef]

    _SDK_API = "MCPServer"
except ImportError:  # pragma: no cover - depends on the installed SDK
    try:
        from mcp.server.fastmcp import FastMCP as _SERVER_CLASS  # type: ignore[no-redef]
        from mcp.server.fastmcp import Image as _IMAGE_CLASS  # type: ignore[no-redef]

        _SDK_API = "FastMCP"
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "No usable MCP server API found. Install the SDK with:\n"
            f'    {sys.executable} -m pip install "mcp[cli]>=1.0"'
        ) from exc


INSTRUCTIONS = """\
AXIO Stitching Studio - high-throughput stitching for Zeiss Axio tile scans.

WORKFLOW (do not skip steps 1-3; each one prevents a failure mode that costs a whole run):
  1. axio_doctor            - confirm the environment. Optional packages gate whole
                              algorithms: basicpy for correction="basicpy", OpenCV for
                              algorithm="sift". They fail at run time, not config time.
  2. axio_inspect_dataset   - read the XML. It tells you whether the dataset is multi-page
                              (channels inside each tile; use ref_channel) or split-channel
                              (one file per channel; use ref_tag + target_tags), how many
                              scenes there are, and whether there is a Z dimension.
  3. axio_estimate_stitch   - size the job. These are gigapixel canvases. Act on the
                              verdict: will_not_fit means narrow the job (one scene,
                              z_mode="mip_output_only", fewer target_tags), not "try anyway".
  4. axio_validate_stitch   - prerequisites and missing tiles.
  5. axio_start_stitch      - run in the BACKGROUND; poll with axio_job_status. A real scene
                              takes minutes to hours, so a synchronous call will time out.
  6. axio_read_preview / axio_qc_report - LOOK at the result before reporting success. A
                              diverged registration still exits zero.
  7. axio_launch_gui        - hand the mosaic to the user in the desktop app.

CHOOSING PARAMETERS
  correction: basicpy (best, slow, needs basicpy) | median (good default for large sets)
              | spatial (uneven background) | none (already flat, or iterating fast)
  algorithm:  phase (default; fast, needs texture) | sift (low contrast or stage drift;
              needs OpenCV) | coordinate (no registration; trust the stage - fastest)
  alignment_mode: reference (align on ref_channel) | average | max_projection (fuse
              channels first when no single channel has enough structure)
  z_mode:     none (2D) | mip_output_only (align 2D, write a projection - cheapest way to
              see a Z dataset) | mip_align_3d (align on the MIP, output the volume)
              | ref_slice_3d (align on ref_z_slice, output the volume)

Split-channel datasets: registration is computed ONCE on ref_tag and re-applied to every
target_tag, so channels stay in register and splitting a run across tags is free.
"""

mcp = _SERVER_CLASS("axio-stitching", version=__version__, instructions=INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _error(message: str, **extra: Any) -> str:
    return _json({"ok": False, "error": message, **extra})


def _split_tags(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def _build_config(
    xml_path: str,
    out_dir: str,
    correction: str = "basicpy",
    algorithm: str = "phase",
    scene: int | None = None,
    ref_channel: int = 0,
    ref_tag: str = "",
    target_tags: str = "",
    alignment_mode: str = "reference",
    z_mode: str = "none",
    ref_z_slice: int = 0,
) -> StitchConfig:
    """Validate and normalise the shared parameter set. Raises ValueError with a usable message."""
    return StitchConfig(
        xml_path=Path(xml_path).expanduser().resolve(),
        out_dir=Path(out_dir).expanduser().resolve(),
        correction=correction,
        algorithm=algorithm,
        scene=scene,
        ref_channel=ref_channel,
        ref_tag=ref_tag,
        target_tags=_split_tags(target_tags),
        alignment_mode=alignment_mode,
        z_mode=z_mode,
        ref_z_slice=ref_z_slice,
    )


# ---------------------------------------------------------------------------
# Environment & vocabulary
# ---------------------------------------------------------------------------

@mcp.tool()
def axio_doctor(out_dir: str = "") -> str:
    """
    Diagnose the environment and report exactly what is missing and how to fix it.

    Run this FIRST. It checks the interpreter, every required package, the optional packages
    that gate whole algorithms (basicpy -> correction="basicpy", OpenCV -> algorithm="sift"),
    the MCP SDK flavour, CPU count, physical memory, and the free space and writability of
    the output volume. Read-only.

    Args:
        out_dir: Optional output directory to check for free space and writability, instead
            of the current working directory.

    Returns:
        JSON with keys: ok, summary, checks[{name, status: ok|warn|fail, detail, fix}], info.
    """
    from .doctor import run_doctor

    return _json(run_doctor(out_dir or None).to_dict())


@mcp.tool()
def axio_list_algorithms() -> str:
    """
    The exact vocabulary of legal parameter values, with guidance on choosing between them.

    Use ONLY values that appear here — the pipeline rejects anything else. Read-only, no
    filesystem access.

    Returns:
        JSON with keys: version, corrections, algorithms, alignment_modes, z_modes, guidance.
    """
    return _json(
        {
            "version": __version__,
            "corrections": [m.value for m in CorrectionMethod],
            "algorithms": [m.value for m in StitchAlgorithm],
            "alignment_modes": [m.value for m in AlignmentMode],
            "z_modes": [m.value for m in ZMode],
            "guidance": {
                "correction": {
                    "basicpy": "BaSiCPy flatfield. Best quality; use when there is a visible "
                               "illumination gradient or vignetting. Slow; needs the basicpy package.",
                    "median": "Median flatfield approximation. Much cheaper and usually good "
                              "enough. The right default for large datasets.",
                    "spatial": "Rolling-ball background subtraction. For uneven BACKGROUND, "
                               "not uneven illumination.",
                    "none": "Skip correction. Use when tiles are already flat-fielded, or while "
                            "iterating on registration.",
                },
                "algorithm": {
                    "phase": "Phase correlation. Default: fast and robust when the stage is "
                             "repeatable and tiles carry texture.",
                    "sift": "Feature matching with a Tikhonov-anchored least-squares solve. For "
                            "low contrast, sparse fluorescence, or real stage drift. Needs OpenCV.",
                    "coordinate": "No registration - place tiles at their stage coordinates. "
                                  "Fastest; correct when the stage is accurate or the sample is featureless.",
                },
                "alignment_mode": {
                    "reference": "Align on ref_channel alone. Default.",
                    "average": "Average the channels first, then align. Use when no single "
                               "channel has enough structure.",
                    "max_projection": "Max-project across channels, then align. Strongest option "
                                      "for sparse multi-marker data.",
                },
                "z_mode": {
                    "none": "2D only - stitch the first slice.",
                    "mip_output_only": "Align in 2D, write a maximum-intensity projection. The "
                                       "cheapest way to see a Z dataset.",
                    "mip_align_3d": "Align on the MIP, apply that transform to every slice. Full 3D volume out.",
                    "ref_slice_3d": "Align on slice ref_z_slice, apply to every slice. Use when "
                                    "one slice is in focus and the MIP is not.",
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# Dataset inspection
# ---------------------------------------------------------------------------

@mcp.tool()
def axio_inspect_dataset(xml_path: str) -> str:
    """
    Parse a Zeiss _info.xml or _meta.xml and return the dataset's structure.

    Read this BEFORE proposing any stitch: it tells you the scene count, the tiles per scene
    with their stage coordinates and sizes, the tile pixel dimensions, the channel and
    Z-slice counts, and the pixel scale in micrometres when the metadata carries it — the
    facts that decide every parameter. Read-only.

    Args:
        xml_path: Absolute path to the Zeiss _info.xml or _meta.xml file. The raw tile TIFFs
            must sit in the same directory.

    Returns:
        JSON with keys: xml_path, xml_type ('info'|'meta'), scenes[{scene_id, tiles[...],
        total_tiles}], total_scenes, total_tiles, pixel_scale_um, tile_geometry.
    """
    try:
        config = StitchConfig(xml_path=Path(xml_path).expanduser().resolve(), out_dir=Path.cwd())
    except Exception as exc:
        return _error(str(exc), xml_path=xml_path)

    from .engine import StitchingEngine

    try:
        metadata = StitchingEngine(config).inspect_metadata()
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}", xml_path=xml_path)

    # Attach real tile geometry — the metadata describes the stage footprint, and only the
    # file itself knows how many channels and Z-slices each tile actually holds.
    try:
        from .canvas import detect_tile_axes

        raw_dir = config.xml_path.parent
        sample = None
        for scene in metadata.get("scenes", []):
            for tile in scene.get("tiles", [])[:4]:
                candidate = raw_dir / tile["filename"]
                if candidate.exists():
                    sample = candidate
                    break
            if sample:
                break
        if sample is not None:
            info = detect_tile_axes(sample)
            metadata["tile_geometry"] = {
                "sample_file": sample.name,
                "axes": info["axes"],
                "tile_height": int(info["H"]),
                "tile_width": int(info["W"]),
                "channels_per_file": int(info["num_channels"]),
                "z_per_file": int(info["num_z"]),
                "layout": "multi-page" if int(info["num_channels"]) > 1 else "single-channel-or-split",
            }
        else:
            metadata["tile_geometry"] = {
                "error": "no tile file from the metadata was found beside the XML; the raw "
                         "TIFFs must sit in the same directory as the XML"
            }
    except Exception as exc:  # noqa: BLE001 - geometry is a bonus, not the payload
        metadata["tile_geometry"] = {"error": f"{type(exc).__name__}: {exc}"}

    return _json(metadata)


@mcp.tool()
def axio_estimate_stitch(
    xml_path: str,
    out_dir: str,
    correction: str = "basicpy",
    algorithm: str = "phase",
    scene: int | None = None,
    ref_channel: int = 0,
    ref_tag: str = "",
    target_tags: str = "",
    alignment_mode: str = "reference",
    z_mode: str = "none",
    ref_z_slice: int = 0,
) -> str:
    """
    Size a stitching job BEFORE running it: canvas dimensions, output size, peak RAM,
    intermediate footprint and a rough wall-clock estimate.

    Call this before every stitch. AXIO canvases are routinely gigapixel, and a run that dies
    on memory forty minutes in costs the user the whole run. ACT ON THE VERDICT:
      * will_not_fit - do not start; narrow the job using the returned `advice` (one scene at
        a time, z_mode="mip_output_only", fewer target_tags per run). The outputs are identical.
      * tight - say so and name the headroom before spending an hour.
      * ok - proceed.
    Read-only; it touches metadata and one sample tile, never the full dataset.

    Args:
        xml_path: Absolute path to the Zeiss _info.xml or _meta.xml.
        out_dir: Intended output directory (its volume's free space is part of the verdict).
        correction: 'basicpy' | 'median' | 'spatial' | 'none'.
        algorithm: 'phase' | 'sift' | 'coordinate'.
        scene: Single scene index (0-based). Omit to estimate every scene.
        ref_channel: Reference channel index for multi-page tiles.
        ref_tag: Reference channel filename tag for split-channel datasets (e.g. '_c1_').
        target_tags: Comma-separated target channel tags (e.g. '_c2_,_c3_').
        alignment_mode: 'reference' | 'average' | 'max_projection'.
        z_mode: 'none' | 'mip_align_3d' | 'ref_slice_3d' | 'mip_output_only'.
        ref_z_slice: Reference Z-slice index for 'ref_slice_3d'.

    Returns:
        JSON with keys: verdict ('ok'|'tight'|'will_not_fit'), reasons, advice, scenes[...],
        totals (peak_ram, output_size, disk_needed, estimated_time), machine, warnings.
        Time estimates are order-of-magnitude and labelled as such.
    """
    try:
        config = _build_config(
            xml_path, out_dir, correction, algorithm, scene, ref_channel,
            ref_tag, target_tags, alignment_mode, z_mode, ref_z_slice,
        )
    except Exception as exc:
        return _error(str(exc))

    from .estimate import estimate_stitch

    try:
        return _json(estimate_stitch(config).to_dict())
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")


@mcp.tool()
def axio_validate_stitch(
    xml_path: str,
    out_dir: str,
    correction: str = "basicpy",
    algorithm: str = "phase",
    scene: int | None = None,
    ref_tag: str = "",
) -> str:
    """
    Check a stitching configuration's prerequisites without running anything.

    Verifies that the XML parses and yields scenes, that the tile files it names actually
    exist beside it, that out_dir is writable, and that the packages the chosen correction
    and algorithm need are importable. Read-only apart from creating out_dir.

    Fix every `error`. Read every `warning` — missing tile files are the common one, and a
    partially-copied dataset stitches to a canvas full of holes rather than failing outright.

    Args:
        xml_path: Absolute path to the Zeiss XML.
        out_dir: Intended output directory.
        correction: Intended correction method.
        algorithm: Intended registration algorithm.
        scene: Intended scene index, checked against the scenes the XML actually contains.
        ref_tag: Intended split-channel reference tag.

    Returns:
        JSON with keys: valid (bool), errors (list), warnings (list).
    """
    try:
        config = _build_config(
            xml_path, out_dir, correction, algorithm, scene, ref_tag=ref_tag
        )
    except Exception as exc:
        return _json({"valid": False, "errors": [str(exc)], "warnings": []})

    from .engine import StitchingEngine

    try:
        return _json(StitchingEngine(config).validate_config())
    except Exception as exc:
        return _json({"valid": False, "errors": [f"{type(exc).__name__}: {exc}"], "warnings": []})


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

@mcp.tool()
def axio_start_stitch(
    xml_path: str,
    out_dir: str,
    correction: str = "basicpy",
    algorithm: str = "phase",
    scene: int | None = None,
    ref_channel: int = 0,
    ref_tag: str = "",
    target_tags: str = "",
    alignment_mode: str = "reference",
    z_mode: str = "none",
    ref_z_slice: int = 0,
) -> str:
    """
    Start the stitching pipeline in the BACKGROUND and return a job id immediately.

    This is the tool to use for real datasets. A scene takes minutes to hours, so a
    synchronous call would time out and orphan a job that is still consuming the machine.
    Poll with axio_job_status, collect with axio_job_result, stop with axio_cancel_job.

    Run axio_estimate_stitch first; if its verdict was will_not_fit, narrow the job instead
    of starting this.

    Args:
        xml_path: Absolute path to the Zeiss _info.xml or _meta.xml.
        out_dir: Directory to write the stitched TIFFs and previews into.
        correction: 'basicpy' | 'median' | 'spatial' | 'none'.
        algorithm: 'phase' | 'sift' | 'coordinate'.
        scene: Single scene index (0-based). Omit to process every scene sequentially.
        ref_channel: Reference channel index for multi-page tiles.
        ref_tag: Reference channel filename tag for split-channel datasets (e.g. '_c1_').
        target_tags: Comma-separated target channel tags (e.g. '_c2_,_c3_'). Registration is
            computed once on ref_tag and re-applied to each of these.
        alignment_mode: 'reference' | 'average' | 'max_projection'.
        z_mode: 'none' | 'mip_align_3d' | 'ref_slice_3d' | 'mip_output_only'.
        ref_z_slice: Reference Z-slice index for 'ref_slice_3d'.

    Returns:
        JSON with keys: job_id, state, config — poll axio_job_status with the job_id.
    """
    try:
        config = _build_config(
            xml_path, out_dir, correction, algorithm, scene, ref_channel,
            ref_tag, target_tags, alignment_mode, z_mode, ref_z_slice,
        )
    except Exception as exc:
        return _error(str(exc))

    from .jobs import MANAGER

    job = MANAGER.start(config)
    payload = job.to_dict(log_lines=0)
    payload["next"] = f"poll axio_job_status with job_id={job.id}"
    return _json(payload)


@mcp.tool()
def axio_job_status(job_id: str, log_lines: int = 30) -> str:
    """
    Poll a background stitching job.

    Poll at a human cadence (tens of seconds) and tell the user which stage the run is in
    rather than going silent. A job whose owning process has gone is reported as `orphaned`,
    never as still running.

    Args:
        job_id: The id returned by axio_start_stitch.
        log_lines: How many trailing log lines to include (0 for none).

    Returns:
        JSON with keys: job_id, state ('running'|'succeeded'|'failed'|'cancelled'|'orphaned'),
        percent, stage, message, elapsed_seconds, log_tail, result, error.
    """
    from .jobs import MANAGER

    return _json(MANAGER.describe(job_id, log_lines=log_lines))


@mcp.tool()
def axio_job_result(job_id: str) -> str:
    """
    The final result of a finished stitching job.

    Args:
        job_id: The id returned by axio_start_stitch.

    Returns:
        JSON with keys: job_id, state, result{success, output_paths, preview_paths,
        duration_seconds, scenes_processed, tiles_processed, error_message}. If the job is
        still running, `state` says so and `result` is null — keep polling axio_job_status.
    """
    from .jobs import MANAGER

    record = MANAGER.describe(job_id, log_lines=0)
    return _json(
        {
            "job_id": job_id,
            "state": record.get("state"),
            "result": record.get("result"),
            "error": record.get("error"),
            "elapsed_seconds": record.get("elapsed_seconds"),
        }
    )


@mcp.tool()
def axio_list_jobs(limit: int = 20) -> str:
    """
    Recent stitching jobs, newest first — including ones started by an earlier server process.

    Use this to pick up where a previous session left off instead of re-running work.

    Args:
        limit: Maximum number of jobs to return.

    Returns:
        JSON with keys: jobs[{job_id, state, percent, stage, config, elapsed_seconds}].
    """
    from .jobs import MANAGER

    live = {j.id: j.to_dict(log_lines=0) for j in MANAGER.list()}
    for record in MANAGER.list_journalled(limit=limit):
        live.setdefault(record.get("job_id", ""), record)
    jobs = sorted(live.values(), key=lambda r: r.get("started_at") or "", reverse=True)[:limit]
    return _json({"jobs": jobs})


@mcp.tool()
def axio_cancel_job(job_id: str) -> str:
    """
    Ask a running stitching job to stop.

    Cancellation is cooperative: it takes effect at the next pipeline stage boundary, so the
    job stops cleanly rather than being killed mid-write. Output already written stays in
    place. Cancelling a finished job is not an error — it reports that there was nothing to stop.

    Args:
        job_id: The id returned by axio_start_stitch.

    Returns:
        JSON with keys: job_id, cancelled (bool), reason, state.
    """
    from .jobs import MANAGER

    return _json(MANAGER.cancel(job_id))


@mcp.tool()
def axio_stitch_sync(
    xml_path: str,
    out_dir: str,
    correction: str = "median",
    algorithm: str = "phase",
    scene: int | None = None,
    ref_channel: int = 0,
    ref_tag: str = "",
    target_tags: str = "",
    alignment_mode: str = "reference",
    z_mode: str = "none",
    ref_z_slice: int = 0,
) -> str:
    """
    Run the pipeline synchronously and return the finished result.

    Only for datasets small enough to complete inside one tool call — a handful of tiles, one
    channel, no Z-stack — and for tests. Check axio_estimate_stitch first: if it reports more
    than about a minute, use axio_start_stitch instead, or this call will simply time out
    while the work continues in the background where you cannot see it.

    Args (identical to axio_start_stitch):
        xml_path, out_dir, correction, algorithm, scene, ref_channel, ref_tag, target_tags,
        alignment_mode, z_mode, ref_z_slice.

    Returns:
        JSON with keys: success, output_paths, preview_paths, duration_seconds,
        scenes_processed, tiles_processed, error_message.
    """
    try:
        config = _build_config(
            xml_path, out_dir, correction, algorithm, scene, ref_channel,
            ref_tag, target_tags, alignment_mode, z_mode, ref_z_slice,
        )
    except Exception as exc:
        return _json({"success": False, "error_message": str(exc)})

    from .engine import StitchingEngine

    try:
        return _json(StitchingEngine(config).run().to_dict())
    except Exception as exc:
        return _json({"success": False, "error_message": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# Looking at the result
# ---------------------------------------------------------------------------

@mcp.tool()
def axio_read_preview(path: str):
    """
    Return a stitched mosaic's preview thumbnail as an IMAGE you can actually look at.

    Do this before reporting success: a diverged registration produces duplicated or torn
    tissue while still exiting zero, and only looking at the picture catches it. Accepts
    either the preview PNG itself or the stitched .tif (the matching *_preview.png beside it
    is used). Read-only.

    Args:
        path: Path to a stitched .tif or its *_preview.png.

    Returns:
        The preview image, or a JSON error naming what was tried when no preview exists.
    """
    target = Path(path).expanduser()
    if target.suffix.lower() in {".tif", ".tiff"}:
        preview = target.with_name(target.stem + "_preview.png")
    else:
        preview = target

    if not preview.exists():
        return _error(
            f"no preview image at {preview}",
            hint="Previews are written beside each stitched TIFF as <name>_preview.png. "
                 "Use axio_list_outputs to see what a run actually produced.",
        )
    try:
        return _IMAGE_CLASS(path=str(preview))
    except Exception as exc:
        return _error(f"could not read {preview}: {type(exc).__name__}: {exc}")


@mcp.tool()
def axio_qc_report(path: str, frame: int | None = None) -> str:
    """
    Measure a stitched mosaic: how much of it is empty, how much is clipped, where the signal
    sits, and whether hard seams run across it.

    Memory-bounded — a gigapixel TIFF is streamed strip by strip, never materialised. Use it
    together with axio_read_preview to judge a result rather than trusting the exit status.

    Interpreting the numbers:
      * empty_fraction - share of never-written pixels. High means missing tiles or a
        registration that flung a tile off-canvas.
      * saturated_fraction - clipped at the sensor maximum. That is an acquisition problem,
        not a stitching one.
      * seam_prominence_x / _y - strongest gradient ridge over the typical gradient. ~1 is
        clean; >= 3 means visible seams; >= 6 means registration did not converge.
      * findings - the same numbers stated as actionable sentences.

    Args:
        path: Path to a stitched .tif.
        frame: Page index for a multi-channel / Z-stack file. Default: the middle page.

    Returns:
        JSON with keys: ok, width, height, dtype, axes, method ('full'|'streamed'),
        metrics{...}, findings[...].
    """
    from .qc import qc_report

    return _json(qc_report(path, frame=frame).to_dict())


@mcp.tool()
def axio_list_outputs(directory: str) -> str:
    """
    List the stitched outputs a previous run left in a directory, with their shapes, sizes and
    preview paths.

    Use this to resume rather than re-run an hour of work. Read-only.

    Args:
        directory: Output directory to scan for stitched_*.tif files.

    Returns:
        JSON with keys: directory, outputs[{path, name, size_bytes, axes, shape, dtype,
        preview_path}].
    """
    from .qc import list_outputs

    return _json({"directory": str(Path(directory).expanduser()), "outputs": list_outputs(directory)})


# ---------------------------------------------------------------------------
# Handing work back to the user
# ---------------------------------------------------------------------------

@mcp.tool()
def axio_launch_gui(out_dir: str = "", xml_path: str = "") -> str:
    """
    Open AXIO Stitching Studio so the user can view a mosaic at full resolution and re-run
    with adjusted parameters.

    Do this once you have an output worth reviewing — a file path in a chat log is not a
    delivered result. If the app cannot be located the tool says so and lists what it tried;
    relay that rather than guessing at a path.

    Args:
        out_dir: Output directory to pre-fill in the app.
        xml_path: Dataset XML to pre-fill in the app.

    Returns:
        JSON with keys: launched (bool), app_path, pid, reason, tried.
    """
    from .agent_runner import find_app_path

    app = os.environ.get("AXIO_STITCHING_APP") or find_app_path()
    if not app or not Path(app).exists():
        return _json(
            {
                "launched": False,
                "app_path": app,
                "reason": "AXIO Stitching Studio could not be located.",
                "fix": "Set the AXIO_STITCHING_APP environment variable to the executable, or "
                       "re-run `axio agent install` from an installation that knows where it is.",
            }
        )

    app_path = Path(app)
    if app_path.suffix.lower() == ".py":
        command = [sys.executable, str(app_path)]
    else:
        command = [str(app_path)]

    env = dict(os.environ)
    if out_dir:
        env["AXIO_STITCHING_OUT_DIR"] = str(Path(out_dir).expanduser())
    if xml_path:
        env["AXIO_STITCHING_XML"] = str(Path(xml_path).expanduser())

    # Detach: the GUI is for the human and must outlive this server process, which the agent
    # host will kill as soon as the conversation ends.
    creation_flags = 0
    if sys.platform.startswith("win"):
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    try:
        process = subprocess.Popen(  # noqa: S603 - launching our own known executable
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=not sys.platform.startswith("win"),
            creationflags=creation_flags,
        )
    except OSError as exc:
        return _json({"launched": False, "app_path": str(app_path), "reason": f"{type(exc).__name__}: {exc}"})

    return _json(
        {
            "launched": True,
            "app_path": str(app_path),
            "pid": process.pid,
            "note": "The app opened in its own window. Tell the user to switch to it.",
        }
    )


@mcp.tool()
def axio_agent_status() -> str:
    """
    Report how AXIO is wired into the AI agent platforms on this machine — which are
    installed, which have the skill and MCP server registered, and whether anything has
    drifted since it was installed.

    Read-only. Use it when the user asks why a tool or the skill is missing somewhere.

    Returns:
        JSON with keys: targets[{target, label, detected, state, units, keys}].
    """
    from .agent_runner import make_ctx, status_all

    try:
        return _json({"targets": [r.to_dict() for r in status_all(make_ctx())]})
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Serve over stdio until the client disconnects.

    **stdout is the JSON-RPC transport.** Nothing else may write to it: a single stray line
    corrupts the frame stream and the client reports "Failed to parse JSONRPC message from
    server". The pipeline therefore sends every diagnostic to stderr (see
    ``axio_stitching.canvas._log`` and ``engine._default_progress``), tqdm already writes to
    stderr, and SDK 2.0 additionally points fd 1 at stderr for the duration of the session so
    a third-party library's ``print`` cannot reach the wire either. ``tests/test_mcp_server.py``
    pins the invariant.
    """
    # Zeiss datasets routinely carry non-ASCII path components; a cp932/cp1252 default
    # codepage turns those into UnicodeDecodeErrors deep inside the pipeline.
    os.environ.setdefault("PYTHONUTF8", "1")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
