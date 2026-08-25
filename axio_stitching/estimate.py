"""
estimate.py — size a stitching job BEFORE committing an hour of compute to it.

AXIO canvases are routinely gigapixel: a 5,000-tile scene at 1020x1020 with three channels
and forty Z-slices is terabytes of intermediate. The failure mode that costs the most is a
run that dies on memory forty minutes in, so this module models the pipeline's actual
allocations and returns a verdict the caller can act on.

The memory model tracks what :func:`axio_stitching.canvas.stitch_canvas` and
:meth:`axio_stitching.engine.StitchingEngine._assemble_canvases` really allocate:

* while a single frame is being blended, the canvas is held as
  ``accumulator`` (float32) + ``weight_map`` (float32) + ``valid`` (bool) + ``canvas``
  (float32) + the uint16 result = **15 bytes per canvas pixel**;
* every completed frame is retained in a Python list until the scene is finished
  (2 bytes/px each), and ``np.stack`` then allocates the whole volume a second time —
  so a multi-frame output costs **2 x frames x 2 bytes per canvas pixel** on top.

Time estimates are explicitly order-of-magnitude: they scale measured-ish per-megapixel
constants and are reported with ``confidence: "order-of-magnitude"`` so a caller never
presents them as a promise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .doctor import disk_free_bytes, human_bytes, memory_bytes
from .models import CorrectionMethod, StitchAlgorithm, StitchConfig, ZMode
from .parsers import parse_zeiss_xml

# ---------------------------------------------------------------------------
# Cost constants — order-of-magnitude, single modern CPU core-set
# ---------------------------------------------------------------------------

#: Seconds per tile for the shading-correction pass, by method.
CORRECTION_SECONDS_PER_TILE = {
    "basicpy": 0.9,
    "median": 0.08,
    "spatial": 0.15,
    "none": 0.0,
}

#: Seconds per tile-pair for registration, by algorithm. Roughly 2 pairs per tile in a grid.
REGISTRATION_SECONDS_PER_PAIR = {
    "phase": 0.25,
    "sift": 1.4,
    "coordinate": 0.0,
}

#: Seconds per megapixel of assembled canvas (blend + compress + write), per output frame.
ASSEMBLY_SECONDS_PER_MEGAPIXEL = 0.05

#: Bytes held per canvas pixel while one frame is being blended (see module docstring).
BLEND_BYTES_PER_PIXEL = 15

#: Bytes per canvas pixel per retained output frame, doubled by the final ``np.stack``.
RETAINED_BYTES_PER_PIXEL_PER_FRAME = 4

_Z_PATTERN = re.compile(r"_z(\d+)_")


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class SceneEstimate:
    scene_id: int
    tiles: int
    canvas_width: int
    canvas_height: int
    canvas_megapixels: float
    output_frames: int
    channels: int
    z_slices: int
    output_bytes: int
    peak_ram_bytes: int
    intermediate_bytes: int
    estimated_seconds: float

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "tiles": self.tiles,
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "canvas_megapixels": round(self.canvas_megapixels, 1),
            "output_frames": self.output_frames,
            "channels": self.channels,
            "z_slices": self.z_slices,
            "output_bytes": self.output_bytes,
            "output_size": human_bytes(self.output_bytes),
            "peak_ram_bytes": self.peak_ram_bytes,
            "peak_ram": human_bytes(self.peak_ram_bytes),
            "intermediate_bytes": self.intermediate_bytes,
            "intermediate_size": human_bytes(self.intermediate_bytes),
            "estimated_seconds": round(self.estimated_seconds),
            "estimated_time": _human_duration(self.estimated_seconds),
        }


@dataclass
class StitchEstimate:
    scenes: list[SceneEstimate] = field(default_factory=list)
    verdict: str = "ok"  # 'ok' | 'tight' | 'will_not_fit'
    reasons: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    available_ram_bytes: int | None = None
    total_ram_bytes: int | None = None
    free_disk_bytes: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def peak_ram_bytes(self) -> int:
        """Scenes are processed one at a time, so the peak is the worst single scene."""
        return max((s.peak_ram_bytes for s in self.scenes), default=0)

    @property
    def total_output_bytes(self) -> int:
        return sum(s.output_bytes for s in self.scenes)

    @property
    def total_intermediate_bytes(self) -> int:
        return sum(s.intermediate_bytes for s in self.scenes)

    @property
    def total_seconds(self) -> float:
        return sum(s.estimated_seconds for s in self.scenes)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reasons": self.reasons,
            "advice": self.advice,
            "scenes": [s.to_dict() for s in self.scenes],
            "totals": {
                "scenes": len(self.scenes),
                "peak_ram_bytes": self.peak_ram_bytes,
                "peak_ram": human_bytes(self.peak_ram_bytes),
                "output_bytes": self.total_output_bytes,
                "output_size": human_bytes(self.total_output_bytes),
                "intermediate_bytes": self.total_intermediate_bytes,
                "intermediate_size": human_bytes(self.total_intermediate_bytes),
                "disk_needed_bytes": self.total_output_bytes + self.total_intermediate_bytes,
                "disk_needed": human_bytes(self.total_output_bytes + self.total_intermediate_bytes),
                "estimated_seconds": round(self.total_seconds),
                "estimated_time": _human_duration(self.total_seconds),
                "time_confidence": "order-of-magnitude",
            },
            "machine": {
                "ram_total": human_bytes(self.total_ram_bytes),
                "ram_available": human_bytes(self.available_ram_bytes),
                "disk_free": human_bytes(self.free_disk_bytes),
                "ram_total_bytes": self.total_ram_bytes,
                "ram_available_bytes": self.available_ram_bytes,
                "free_disk_bytes": self.free_disk_bytes,
            },
            "warnings": self.warnings,
        }


def _human_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} days"


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

def _tile_geometry(raw_dir: Path, tiles: list[dict]) -> tuple[int, int, int, int, list[str]]:
    """
    ``(tile_h, tile_w, channels, z_slices, warnings)`` for a scene.

    Prefers reading a real tile (the metadata's ``SizeX``/``SizeY`` describe the stage
    footprint, not necessarily the file's pixel dimensions, and only the file knows how many
    channels and Z-slices it holds). Falls back to the metadata when no tile is readable.
    """
    warnings: list[str] = []
    from .canvas import detect_tile_axes

    for tile in tiles[:8]:
        path = raw_dir / tile["filename"]
        if not path.exists():
            continue
        try:
            info = detect_tile_axes(path)
        except Exception as exc:  # noqa: BLE001 - a broken tile must not sink the estimate
            warnings.append(f"could not read {path.name}: {exc}")
            continue
        return int(info["H"]), int(info["W"]), int(info["num_channels"]), int(info["num_z"]), warnings

    warnings.append(
        "no tile file could be read next to the XML - falling back to the metadata's tile "
        "size, and assuming 1 channel / 1 Z-slice"
    )
    first = tiles[0]
    return int(first.get("h") or 1020), int(first.get("w") or 1020), 1, 1, warnings


def _z_slice_count(tiles: list[dict], file_z: int) -> int:
    """Z-slices implied by ``_z<NN>_`` filename tags, falling back to the file's own Z axis."""
    indices = {int(m.group(1)) for t in tiles for m in [_Z_PATTERN.search(t["filename"])] if m}
    return len(indices) if indices else max(1, file_z)


def estimate_stitch(config: StitchConfig) -> StitchEstimate:
    """
    Model the cost of running ``config``, without touching a single pixel of real work.

    Returns a :class:`StitchEstimate` whose ``verdict`` is:

    ``ok``
        Peak RAM fits comfortably in available memory and the output fits on disk.
    ``tight``
        Peak RAM is more than 60% of available memory, or the output would leave less than
        10% of the volume free. The run will probably succeed but the caller should say so.
    ``will_not_fit``
        Peak RAM exceeds available memory, or the output plus intermediates exceed free
        disk. Do not start; narrow the job.
    """
    estimate = StitchEstimate()
    total_ram, available_ram = memory_bytes()
    estimate.total_ram_bytes = total_ram
    estimate.available_ram_bytes = available_ram if available_ram is not None else total_ram
    estimate.free_disk_bytes = disk_free_bytes(config.out_dir)

    # A dataset we cannot read is a verdict, not an exception: every caller of this function
    # is told to "act on the verdict", so an unreadable XML must arrive through that channel
    # rather than as a traceback the caller has to special-case.
    try:
        scenes_raw, _xml_type, _pixel_scale = parse_zeiss_xml(config.xml_path)
    except Exception as exc:  # noqa: BLE001 - parsers raise several unrelated types
        estimate.verdict = "will_not_fit"
        estimate.reasons.append(f"the XML could not be parsed: {exc}")
        return estimate

    if not scenes_raw:
        estimate.verdict = "will_not_fit"
        estimate.reasons.append("no scenes or tile geometry could be extracted from the XML")
        return estimate

    raw_dir = config.xml_path.parent
    target_scenes = [config.scene] if config.scene is not None else sorted(scenes_raw.keys())

    for scene_id in target_scenes:
        scene_tiles = scenes_raw.get(scene_id)
        if not scene_tiles:
            estimate.warnings.append(f"scene {scene_id} is not present in the XML")
            continue

        ref_tiles = scene_tiles
        if config.ref_tag:
            matched = [t for t in scene_tiles if config.ref_tag in t["filename"]]
            if matched:
                ref_tiles = matched
            else:
                estimate.warnings.append(
                    f"scene {scene_id}: no tile matched ref_tag {config.ref_tag!r}; "
                    "estimating over every tile instead"
                )

        tile_h, tile_w, file_channels, file_z, geom_warnings = _tile_geometry(raw_dir, ref_tiles)
        estimate.warnings.extend(f"scene {scene_id}: {w}" for w in geom_warnings)

        xs = [float(t["x"]) for t in ref_tiles]
        ys = [float(t["y"]) for t in ref_tiles]
        canvas_w = int(max(xs) - min(xs)) + tile_w
        canvas_h = int(max(ys) - min(ys)) + tile_h
        canvas_px = max(1, canvas_w * canvas_h)

        z_slices = _z_slice_count(scene_tiles, file_z)

        # How many 2-D frames the chosen mode actually writes.
        if config.z_mode in (ZMode.NONE, ZMode.MIP_OUTPUT_ONLY):
            out_z = 1
        else:
            out_z = z_slices

        if config.ref_tag:
            # Split-channel: each tag is stitched and saved as its OWN file, so the retained
            # volume is per-tag, not per-dataset.
            channels_out = 1 + len(config.target_tags)
            frames_per_file = out_z
        else:
            channels_out = max(1, file_channels)
            frames_per_file = out_z * channels_out

        output_bytes = canvas_px * 2 * out_z * channels_out
        peak_ram = canvas_px * BLEND_BYTES_PER_PIXEL + canvas_px * RETAINED_BYTES_PER_PIXEL_PER_FRAME * frames_per_file

        tiles_read = len(scene_tiles)
        intermediate_bytes = (
            0 if config.correction == CorrectionMethod.NONE
            else tiles_read * tile_h * tile_w * 2 * max(1, file_channels) * max(1, file_z)
        )

        pairs = max(0, 2 * len(ref_tiles) - 2)  # ~2 neighbours per tile in a meander grid
        seconds = (
            CORRECTION_SECONDS_PER_TILE.get(config.correction.value, 0.1) * tiles_read
            + REGISTRATION_SECONDS_PER_PAIR.get(config.algorithm.value, 0.3) * pairs
            + ASSEMBLY_SECONDS_PER_MEGAPIXEL * (canvas_px / 1e6) * out_z * channels_out
        )

        estimate.scenes.append(
            SceneEstimate(
                scene_id=scene_id,
                tiles=len(ref_tiles),
                canvas_width=canvas_w,
                canvas_height=canvas_h,
                canvas_megapixels=canvas_px / 1e6,
                output_frames=out_z * channels_out,
                channels=channels_out,
                z_slices=out_z,
                output_bytes=output_bytes,
                peak_ram_bytes=peak_ram,
                intermediate_bytes=intermediate_bytes,
                estimated_seconds=seconds,
            )
        )

    _decide(estimate, config)
    return estimate


def _decide(estimate: StitchEstimate, config: StitchConfig) -> None:
    """Turn the numbers into a verdict plus concrete, job-narrowing advice."""
    if not estimate.scenes:
        estimate.verdict = "will_not_fit"
        estimate.reasons.append("no scene could be estimated")
        return

    peak = estimate.peak_ram_bytes
    available = estimate.available_ram_bytes
    disk_needed = estimate.total_output_bytes + estimate.total_intermediate_bytes
    free_disk = estimate.free_disk_bytes

    verdict = "ok"

    if available:
        if peak > available:
            verdict = "will_not_fit"
            estimate.reasons.append(
                f"peak RAM {human_bytes(peak)} exceeds the {human_bytes(available)} available"
            )
        elif peak > 0.6 * available:
            verdict = "tight"
            estimate.reasons.append(
                f"peak RAM {human_bytes(peak)} is {peak / available:.0%} of the "
                f"{human_bytes(available)} available"
            )
    else:
        estimate.warnings.append("available RAM is unknown; the memory verdict is unverified")

    if free_disk is not None:
        if disk_needed > free_disk:
            verdict = "will_not_fit"
            estimate.reasons.append(
                f"output plus intermediates need {human_bytes(disk_needed)} but only "
                f"{human_bytes(free_disk)} is free on the output volume"
            )
        elif disk_needed > 0.9 * free_disk and verdict != "will_not_fit":
            verdict = "tight"
            estimate.reasons.append(
                f"output plus intermediates need {human_bytes(disk_needed)} of the "
                f"{human_bytes(free_disk)} free"
            )

    estimate.verdict = verdict

    if verdict == "ok":
        return

    # Advice is ordered by how much it saves, and every item is something the caller can
    # literally do by changing one argument.
    if config.scene is None and len(estimate.scenes) > 1:
        worst = max(estimate.scenes, key=lambda s: s.peak_ram_bytes)
        estimate.advice.append(
            f"stitch one scene at a time (scene=0..{max(s.scene_id for s in estimate.scenes)}); "
            f"the largest single scene needs {human_bytes(worst.peak_ram_bytes)}"
        )
    if config.z_mode in (ZMode.MIP_ALIGN_3D, ZMode.REF_SLICE_3D):
        estimate.advice.append(
            'use z_mode="mip_output_only" to write a single projected frame instead of the '
            "whole volume - it cuts both RAM and output size by the Z-slice count"
        )
    if config.ref_tag and config.target_tags:
        estimate.advice.append(
            "stitch fewer target_tags per run (registration is computed on the reference "
            "channel and re-applied, so splitting the run does not change the result)"
        )
    if config.correction != CorrectionMethod.NONE:
        estimate.advice.append(
            f'correction="{config.correction.value}" writes {human_bytes(estimate.total_intermediate_bytes)} '
            'of corrected tiles; correction="none" skips that if the tiles are already flat'
        )
    if config.correction == CorrectionMethod.BASICPY:
        estimate.advice.append(
            'correction="median" is far cheaper than "basicpy" and is usually good enough for '
            "a first look"
        )
