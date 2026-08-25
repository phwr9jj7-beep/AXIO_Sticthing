"""
engine.py
---------
The StitchingEngine — clean programmatic API for the full AXIO stitching pipeline.

This replaces the main() function from gui_runner.py (lines 620-939) and is the
single entry point for all three interfaces: CLI, MCP server, and GUI.

Design notes:
  - All algorithm constants (LAMBDA_ANCHOR, MAX_PHASE_SHIFT) are preserved in their
    respective modules (stitchers.py).
  - Progress is emitted via ProgressCallback; the GUI subprocess interface
    is provided by the thin shim in scripts/gui_runner.py (prints [STATUS]/[PROGRESS]).
  - Full-resolution stitching is always enforced (downsample=1), matching the
    hardcoded constant in the original main().
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import numpy as np

from .canvas import detect_tile_axes, stitch_canvas, save_tiff, save_preview_thumbnail
from .corrections import run_correction
from .models import (
    StitchConfig,
    StitchResult,
    InspectResult,
    ValidationResult,
    ProgressCallback,
    ProgressEvent,
    PipelineStage,
)
from .parsers import parse_zeiss_xml, parse_zeiss_xml_to_models
from .stitchers import compute_alignment


class StitchingEngine:
    """
    Programmatic API for the AXIO Stitching pipeline.

    Usage:
        config = StitchConfig(xml_path=..., out_dir=...)
        engine = StitchingEngine(config, progress_callback=my_callback)
        result = engine.run()
    """

    def __init__(
        self,
        config: StitchConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self._progress = progress_callback or _default_progress

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self) -> StitchResult:
        """Execute the full stitching pipeline synchronously. Returns a StitchResult."""
        start = time.time()
        cfg = self.config

        self._emit(1, "Starting stitching runner...", PipelineStage.INIT)
        self._emit(1, f"XML Metadata Path: {cfg.xml_path}", PipelineStage.INIT)
        self._emit(1, f"Output Directory  : {cfg.out_dir}", PipelineStage.INIT)
        self._emit(1, f"Correction        : {cfg.correction.value}", PipelineStage.INIT)
        self._emit(1, f"Algorithm         : {cfg.algorithm.value}", PipelineStage.INIT)
        self._emit(1, f"Downsample        : 1x (Full Resolution Enforced)", PipelineStage.INIT)
        self._emit(1, f"Reference Channel : {cfg.ref_channel}", PipelineStage.INIT)
        self._emit(1, f"Reference Tag     : {cfg.ref_tag}", PipelineStage.INIT)
        self._emit(1, f"Target Tags       : {cfg.target_tags}", PipelineStage.INIT)
        self._emit(1, f"Alignment Mode    : {cfg.alignment_mode.value}", PipelineStage.INIT)
        self._emit(1, f"Z-Stack Mode      : {cfg.z_mode.value}", PipelineStage.INIT)
        self._emit(1, f"Reference Z Slice : {cfg.ref_z_slice}", PipelineStage.INIT)

        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        raw_dir = cfg.xml_path.parent

        try:
            # ----- Step 1: Parse XML -------------------------------------------
            self._emit(2, "Parsing Zeiss XML metadata...", PipelineStage.PARSING)
            scenes_raw, xml_type, _ = parse_zeiss_xml(cfg.xml_path)

            if not scenes_raw:
                return StitchResult(
                    success=False,
                    error_message="No tiles or scene grid geometry could be extracted.",
                )

            self._emit(5, f"Extracted {len(scenes_raw)} scenes to stitch.", PipelineStage.PARSING)

            target_scenes = (
                [cfg.scene] if cfg.scene is not None else sorted(scenes_raw.keys())
            )

            output_paths: list[Path] = []
            preview_paths: list[Path] = []
            total_tiles_processed = 0
            scenes_processed = 0

            # ----- Per-scene loop ----------------------------------------------
            for scene_idx in target_scenes:
                if scene_idx not in scenes_raw:
                    self._emit(0, f"Scene {scene_idx} does not exist. Skipping.", PipelineStage.PARSING)
                    continue

                scene_tiles = scenes_raw[scene_idx]
                self._emit(5, f"--- Processing Scene {scene_idx} ({len(scene_tiles)} tiles) ---", PipelineStage.PARSING)

                # Filter ref tiles for split-channel
                if cfg.ref_tag:
                    ref_tiles = [t for t in scene_tiles if cfg.ref_tag in t["filename"]]
                    if not ref_tiles:
                        self._emit(0, f"Warning: No tiles matched ref tag '{cfg.ref_tag}'. Using all.", PipelineStage.PARSING)
                        ref_tiles = scene_tiles
                else:
                    ref_tiles = scene_tiles

                # ----- Step 2: Flatfield correction ----------------------------
                self._emit(5, "Starting shading correction...", PipelineStage.CORRECTION)
                if cfg.correction.value != "none":
                    correction_dir = cfg.out_dir / "intermediate" / f"scene{scene_idx}" / f"{cfg.correction.value}_corrected"
                    run_correction(
                        raw_dir, scene_tiles, correction_dir,
                        method=cfg.correction.value,
                        ref_channel_idx=cfg.ref_channel,
                        ref_tag=cfg.ref_tag,
                        target_tags=cfg.target_tags,
                        progress_callback=self._progress,
                    )
                else:
                    correction_dir = raw_dir

                # ----- Step 3: Alignment ---------------------------------------
                positions = compute_alignment(
                    ref_tiles, correction_dir,
                    algorithm=cfg.algorithm.value,
                    ref_channel_idx=cfg.ref_channel,
                    alignment_mode=cfg.alignment_mode.value,
                    z_mode=cfg.z_mode.value,
                    ref_z_slice=cfg.ref_z_slice,
                    progress_callback=self._progress,
                )

                # ----- Step 4: Canvas assembly ---------------------------------
                self._emit(80, "Blending tiles and building output canvas...", PipelineStage.CANVAS)

                sample_info = detect_tile_axes(correction_dir / ref_tiles[0]["filename"])
                num_channels = sample_info["num_channels"]
                tile_h = sample_info["H"]
                tile_w = sample_info["W"]

                z_pattern = re.compile(r"_z(\d+)_")
                xml_z_indices = sorted(set(
                    int(m.group(1))
                    for t in scene_tiles
                    for m in [z_pattern.search(t["filename"])] if m
                ))
                if not xml_z_indices:
                    first_fn = scene_tiles[0]["filename"]
                    prefix = first_fn.split("_ORG.tif")[0]
                    try:
                        for f in os.listdir(raw_dir):
                            if f.startswith(prefix.split("_c")[0]):
                                m = z_pattern.search(f)
                                if m:
                                    xml_z_indices.append(int(m.group(1)))
                        xml_z_indices = sorted(set(xml_z_indices))
                    except Exception:
                        pass

                num_z_slices = len(xml_z_indices) if xml_z_indices else sample_info["num_z"]

                if cfg.z_mode.value == "none":
                    z_slices_to_stitch = [0]
                    num_z = 1
                    stitching_z_mode = "slice"
                elif cfg.z_mode.value == "mip_output_only":
                    z_slices_to_stitch = [0]
                    num_z = 1
                    stitching_z_mode = "mip"
                else:
                    num_z = num_z_slices
                    z_slices_to_stitch = list(range(num_z))
                    stitching_z_mode = "slice"

                scene_outputs, scene_previews = self._assemble_canvases(
                    scene_idx=scene_idx,
                    cfg=cfg,
                    ref_tiles=ref_tiles,
                    positions=positions,
                    correction_dir=correction_dir,
                    tile_h=tile_h,
                    tile_w=tile_w,
                    num_channels=num_channels,
                    num_z=num_z,
                    z_slices_to_stitch=z_slices_to_stitch,
                    stitching_z_mode=stitching_z_mode,
                )
                output_paths.extend(scene_outputs)
                preview_paths.extend(scene_previews)
                total_tiles_processed += len(ref_tiles)
                scenes_processed += 1

            self._emit(100, "✓ Stitching operation completed successfully!", PipelineStage.DONE)
            return StitchResult(
                success=True,
                output_paths=output_paths,
                preview_paths=preview_paths,
                duration_seconds=time.time() - start,
                scenes_processed=scenes_processed,
                tiles_processed=total_tiles_processed,
            )

        except Exception as exc:
            self._emit(0, f"[ERROR] {exc}", PipelineStage.FAILED)
            return StitchResult(
                success=False,
                error_message=str(exc),
                duration_seconds=time.time() - start,
            )

    def inspect_metadata(self) -> dict:
        """Parse XML and return scene/tile metadata without running stitching."""
        scene_list, xml_type, pixel_scale_um = parse_zeiss_xml_to_models(self.config.xml_path)
        result = InspectResult(
            xml_path=str(self.config.xml_path),
            xml_type=xml_type,
            scenes=scene_list,
            pixel_scale_um=pixel_scale_um,
        )
        return result.to_dict()

    def validate_config(self) -> dict:
        """Validate configuration without running the pipeline. Returns ValidationResult dict."""
        cfg = self.config
        errors: list[str] = []
        warnings: list[str] = []

        # XML
        if not cfg.xml_path.exists():
            errors.append(f"XML file does not exist: {cfg.xml_path}")

        # Output directory writability
        try:
            cfg.out_dir.mkdir(parents=True, exist_ok=True)
            test_file = cfg.out_dir / ".axio_write_test"
            test_file.touch()
            test_file.unlink()
        except OSError as e:
            errors.append(f"Output directory not writable: {e}")

        # Dependency checks
        if cfg.correction.value == "basicpy":
            try:
                import basicpy  # noqa: F401
            except ImportError:
                errors.append("BaSiCPy not installed. Run: pip install basicpy")

        if cfg.algorithm.value == "sift":
            try:
                import cv2  # noqa: F401
            except ImportError:
                errors.append("OpenCV not installed for SIFT. Run: pip install opencv-python")

        # Tile existence check (sample only)
        if cfg.xml_path.exists():
            try:
                scenes_raw, _, _ = parse_zeiss_xml(cfg.xml_path)
                if not scenes_raw:
                    errors.append("No scenes found in XML file.")
                else:
                    raw_dir = cfg.xml_path.parent
                    first_scene = next(iter(scenes_raw.values()))
                    missing = sum(1 for t in first_scene if not (raw_dir / t["filename"]).exists())
                    if missing > 0:
                        warnings.append(
                            f"{missing}/{len(first_scene)} tile files missing in raw data directory."
                        )
                    if cfg.scene is not None and cfg.scene not in scenes_raw:
                        errors.append(f"Scene {cfg.scene} not found. Available: {sorted(scenes_raw.keys())}")
            except Exception as e:
                errors.append(f"XML parse error: {e}")

        return ValidationResult(
            valid=len(errors) == 0,
            warnings=warnings,
            errors=errors,
        ).to_dict()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _emit(self, percent: int, msg: str, stage: PipelineStage) -> None:
        self._progress(ProgressEvent(percent=percent, status_message=msg, stage=stage))

    def _assemble_canvases(
        self, *, scene_idx, cfg, ref_tiles, positions,
        correction_dir, tile_h, tile_w, num_channels,
        num_z, z_slices_to_stitch, stitching_z_mode,
    ) -> tuple[list[Path], list[Path]]:
        """Assemble and save all output canvas TIFFs for a single scene."""
        output_paths: list[Path] = []
        preview_paths: list[Path] = []
        downsample = 1  # Always full resolution
        suffix = ""

        if cfg.ref_tag:
            # --- Split-channel flow ---
            all_tags = [cfg.ref_tag] + cfg.target_tags
            for tag in all_tags:
                self._emit(80, f"Stitching split channel for tag '{tag}'...", PipelineStage.CANVAS)
                tag_tiles = [
                    {**t, "filename": t["filename"].replace(cfg.ref_tag, tag)}
                    for t in ref_tiles
                ]
                tag_positions = {
                    fn.replace(cfg.ref_tag, tag): pos
                    for fn, pos in positions.items()
                }
                tag_clean = tag.strip("_")

                if num_z > 1:
                    z_canvases = []
                    for z in z_slices_to_stitch:
                        self._emit(80, f"  Stitching Z-slice {z}/{num_z}...", PipelineStage.CANVAS)
                        canvas_z = stitch_canvas(
                            tag_positions, correction_dir, tag_tiles, tile_h, tile_w,
                            downsample=downsample, channel_idx=0, z_idx=z,
                            channel_mode="reference", z_mode=stitching_z_mode,
                            ref_tag=cfg.ref_tag, channel_tag=tag,
                        )
                        if canvas_z is not None:
                            z_canvases.append(canvas_z)
                    if z_canvases:
                        final_volume = np.stack(z_canvases, axis=0)
                        out_fn = f"stitched_scene{scene_idx}_{tag_clean}_{cfg.algorithm.value}{suffix}.tif"
                        out_path = cfg.out_dir / out_fn
                        self._emit(90, f"Saving stitched 3D volume: {out_fn}", PipelineStage.OUTPUT)
                        save_tiff(final_volume, out_path, axes_hint="ZYX")
                        preview_p = out_path.with_name(out_path.stem + "_preview.png")
                        save_preview_thumbnail(final_volume, out_path, num_channels=1, num_z=num_z, stitching_z_mode=stitching_z_mode)
                        output_paths.append(out_path)
                        if preview_p.exists():
                            preview_paths.append(preview_p)
                else:
                    canvas = stitch_canvas(
                        tag_positions, correction_dir, tag_tiles, tile_h, tile_w,
                        downsample=downsample, channel_idx=0, z_idx=0,
                        channel_mode="reference", z_mode=stitching_z_mode,
                        ref_tag=cfg.ref_tag, channel_tag=tag,
                    )
                    if canvas is not None:
                        out_fn = f"stitched_scene{scene_idx}_{tag_clean}_{cfg.algorithm.value}{suffix}.tif"
                        out_path = cfg.out_dir / out_fn
                        self._emit(90, f"Saving stitched image: {out_fn}", PipelineStage.OUTPUT)
                        save_tiff(canvas, out_path, axes_hint="YX")
                        preview_p = out_path.with_name(out_path.stem + "_preview.png")
                        save_preview_thumbnail(canvas, out_path, num_channels=1, num_z=1, stitching_z_mode=stitching_z_mode)
                        output_paths.append(out_path)
                        if preview_p.exists():
                            preview_paths.append(preview_p)

            self._emit(95, f"[SUCCESS] All split channels stitched for scene {scene_idx}", PipelineStage.OUTPUT)

        else:
            # --- Multi-page stack flow ---
            if num_z > 1:
                z_canvases = []
                for z in z_slices_to_stitch:
                    self._emit(80, f"  Stitching Z-slice {z}/{num_z}...", PipelineStage.CANVAS)
                    ch_canvases = []
                    for c in range(num_channels):
                        canvas_zc = stitch_canvas(
                            positions, correction_dir, ref_tiles, tile_h, tile_w,
                            downsample=downsample, channel_idx=c, z_idx=z,
                            channel_mode="reference", z_mode=stitching_z_mode,
                        )
                        if canvas_zc is not None:
                            ch_canvases.append(canvas_zc)
                    if ch_canvases:
                        if num_channels > 1:
                            z_canvases.append(np.stack(ch_canvases, axis=0))
                        else:
                            z_canvases.append(ch_canvases[0])

                if z_canvases:
                    final_volume = np.stack(z_canvases, axis=0)
                    out_fn = f"stitched_scene{scene_idx}_{cfg.algorithm.value}{suffix}.tif"
                    out_path = cfg.out_dir / out_fn
                    self._emit(90, f"Saving stitched 3D/4D volume: {out_fn}", PipelineStage.OUTPUT)
                    save_tiff(final_volume, out_path, axes_hint="ZCYX" if num_channels > 1 else "ZYX")
                    preview_p = out_path.with_name(out_path.stem + "_preview.png")
                    save_preview_thumbnail(final_volume, out_path, num_channels=num_channels, num_z=num_z, stitching_z_mode=stitching_z_mode)
                    output_paths.append(out_path)
                    if preview_p.exists():
                        preview_paths.append(preview_p)
                    self._emit(95, f"[SUCCESS] Stitched scene {scene_idx} saved at: {out_path}", PipelineStage.OUTPUT)

            else:
                canvases = []
                for c in range(num_channels):
                    self._emit(80, f"Stitching channel {c} canvas...", PipelineStage.CANVAS)
                    canvas_c = stitch_canvas(
                        positions, correction_dir, ref_tiles, tile_h, tile_w,
                        downsample=downsample, channel_idx=c, z_idx=0,
                        channel_mode="reference", z_mode=stitching_z_mode,
                    )
                    if canvas_c is not None:
                        canvases.append(canvas_c)

                if canvases:
                    stacked = np.stack(canvases, axis=0) if len(canvases) > 1 else canvases[0]
                    out_fn = f"stitched_scene{scene_idx}_{cfg.algorithm.value}{suffix}.tif"
                    out_path = cfg.out_dir / out_fn
                    self._emit(90, f"Saving stitched multi-channel image: {out_fn}", PipelineStage.OUTPUT)
                    save_tiff(stacked, out_path, axes_hint="CYX" if len(canvases) > 1 else "YX")
                    preview_p = out_path.with_name(out_path.stem + "_preview.png")
                    save_preview_thumbnail(stacked, out_path, num_channels=len(canvases), num_z=1, stitching_z_mode=stitching_z_mode)
                    output_paths.append(out_path)
                    if preview_p.exists():
                        preview_paths.append(preview_p)
                    self._emit(95, f"[SUCCESS] Stitched scene {scene_idx} saved at: {out_path}", PipelineStage.OUTPUT)

        return output_paths, preview_paths


# ---------------------------------------------------------------------------
# Default progress emitter (stdout format for gui_worker.py compatibility)
# ---------------------------------------------------------------------------

def _default_progress(event: ProgressEvent) -> None:
    """
    Fallback when no callback is supplied.

    Writes to STDERR, not stdout: when the pipeline runs as an MCP stdio server, stdout is
    the JSON-RPC transport and a stray line corrupts the frame stream. The GUI supplies its
    own stdout-printing callback (scripts/gui_runner.py), which is what gui_worker.py parses.
    """
    print(f"[STATUS] {event.status_message}", file=sys.stderr, flush=True)
    print(f"[PROGRESS] {event.percent}", file=sys.stderr, flush=True)
