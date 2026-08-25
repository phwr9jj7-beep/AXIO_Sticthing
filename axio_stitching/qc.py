"""
qc.py — bounded quality control over a stitched mosaic.

A stitch that "succeeded" can still be wrong: a diverged registration produces duplicated or
torn tissue, a missing tile leaves a hole, and an over-aggressive shading correction flattens
real signal. Exit status does not show any of that, so this module measures it.

Everything here is **memory-bounded**. Stitched outputs are routinely gigapixel, so the
statistics are accumulated by streaming the TIFF's decoded strips/tiles
(:meth:`tifffile.TiffPage.segments`) rather than materialising the frame, and the only
full-size buffers ever allocated are two 1-D gradient profiles (one value per column, one
per row) plus a 65,536-bin histogram.

Metrics
-------
``empty_fraction``
    Share of pixels that are exactly zero — the canvas is zero-initialised and only written
    where a tile landed, so this is directly "how much of the mosaic has no data". A high
    value means tiles were missing, or registration flung one off-canvas.

``saturated_fraction``
    Share of pixels at the dtype maximum. High values mean clipped signal upstream.

``dynamic_range`` / ``percentiles``
    Where the signal actually lives, from the streamed histogram.

``seam_prominence_x`` / ``seam_prominence_y``
    The ratio of the strongest gradient ridge to the typical gradient, measured along each
    axis. Tile boundaries that are visible as seams appear as a regular comb of ridges in
    these profiles; a value near 1 means no ridge stands out (good), and a large value means
    a hard edge runs straight across the mosaic (bad). The positions of the top ridges are
    returned too, so they can be compared against the tile pitch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

#: Frames larger than this are streamed segment-by-segment rather than read whole.
STREAM_THRESHOLD_PIXELS = 32_000_000

#: Bins used for the intensity histogram (matches uint16's full range).
HISTOGRAM_BINS = 65536

#: Upper bound on a reported seam-prominence ratio. Also the value used when the baseline
#: gradient is exactly zero, where the ratio would otherwise be undefined.
SEAM_PROMINENCE_CAP = 999.0


@dataclass
class QCReport:
    path: str
    ok: bool
    frame_index: int = 0
    axes: str = ""
    shape: tuple[int, ...] = ()
    dtype: str = ""
    width: int = 0
    height: int = 0
    total_pixels: int = 0
    method: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "frame_index": self.frame_index,
            "axes": self.axes,
            "shape": list(self.shape),
            "dtype": self.dtype,
            "width": self.width,
            "height": self.height,
            "total_pixels": self.total_pixels,
            "method": self.method,
            "metrics": self.metrics,
            "findings": self.findings,
            "error": self.error,
        }


class _Accumulator:
    """Streaming statistics over an image read in blocks of whole rows or tiles."""

    def __init__(self, height: int, width: int, dtype: np.dtype) -> None:
        self.height = height
        self.width = width
        self.dtype = dtype
        self.count = 0
        self.zeros = 0
        self.saturated = 0
        self.total = 0.0
        self.total_sq = 0.0
        self.minimum: float = math.inf
        self.maximum: float = -math.inf
        self.histogram = np.zeros(HISTOGRAM_BINS, dtype=np.int64)
        # Gradient energy per column (vertical seams) and per row (horizontal seams).
        self.grad_x = np.zeros(max(1, width - 1), dtype=np.float64)
        self.grad_x_n = np.zeros(max(1, width - 1), dtype=np.int64)
        self.grad_y = np.zeros(max(1, height - 1), dtype=np.float64)
        self.grad_y_n = np.zeros(max(1, height - 1), dtype=np.int64)
        self._max_value = float(np.iinfo(dtype).max) if np.issubdtype(dtype, np.integer) else None

    def add_block(self, block: np.ndarray, y_offset: int, x_offset: int) -> None:
        """Fold a 2-D block located at ``(y_offset, x_offset)`` into the statistics."""
        if block.size == 0:
            return
        flat = block.reshape(-1)
        self.count += flat.size
        self.zeros += int(np.count_nonzero(flat == 0))
        if self._max_value is not None:
            self.saturated += int(np.count_nonzero(flat == self._max_value))
        as_float = flat.astype(np.float64, copy=False)
        self.total += float(as_float.sum())
        self.total_sq += float(np.dot(as_float, as_float))
        self.minimum = min(self.minimum, float(flat.min()))
        self.maximum = max(self.maximum, float(flat.max()))

        if np.issubdtype(block.dtype, np.integer):
            binned = np.clip(flat.astype(np.int64), 0, HISTOGRAM_BINS - 1)
            self.histogram += np.bincount(binned, minlength=HISTOGRAM_BINS)

        h, w = block.shape
        if w > 1:
            dx = np.abs(np.diff(block.astype(np.float32), axis=1)).sum(axis=0)
            stop = min(x_offset + dx.size, self.grad_x.size)
            span = stop - x_offset
            if span > 0:
                self.grad_x[x_offset:stop] += dx[:span]
                self.grad_x_n[x_offset:stop] += h
        if h > 1:
            dy = np.abs(np.diff(block.astype(np.float32), axis=0)).sum(axis=1)
            stop = min(y_offset + dy.size, self.grad_y.size)
            span = stop - y_offset
            if span > 0:
                self.grad_y[y_offset:stop] += dy[:span]
                self.grad_y_n[y_offset:stop] += w

    # -- derived -------------------------------------------------------------

    def percentile(self, q: float) -> float:
        """Percentile from the streamed histogram (integer images only)."""
        total = int(self.histogram.sum())
        if total == 0:
            return 0.0
        target = q / 100.0 * total
        cumulative = np.cumsum(self.histogram)
        idx = int(np.searchsorted(cumulative, target, side="left"))
        return float(min(idx, HISTOGRAM_BINS - 1))

    def seam_profile(self, which: str) -> tuple[float, list[int]]:
        """
        ``(prominence, top_positions)`` for one axis.

        Prominence is the strongest ridge divided by the median ridge height, computed on the
        mean absolute gradient per column (or row). Values near 1 mean nothing stands out.

        Two degenerate cases are handled explicitly rather than by dividing by zero:

        * **no gradient anywhere** (a constant frame) — there is no seam to find, so 0.
        * **gradient ONLY at the ridges** (a flat canvas cut by one hard edge, which is the
          exact signature of a catastrophically diverged registration) — the median baseline
          is 0, so the ratio is undefined; report the cap, because this is the strongest
          possible seam signal, not the weakest.

        The ratio is capped either way so a near-flat real image cannot produce an absurd
        number that swamps the report.
        """
        values = self.grad_x if which == "x" else self.grad_y
        counts = self.grad_x_n if which == "x" else self.grad_y_n
        valid = counts > 0
        if not np.any(valid):
            return 0.0, []
        profile = np.zeros_like(values)
        profile[valid] = values[valid] / counts[valid]
        peak = float(profile.max())
        if peak <= 0:
            return 0.0, []
        positions = [int(i) for i in np.argsort(profile)[::-1][:5]]
        median = float(np.median(profile[valid]))
        if median <= 0:
            return SEAM_PROMINENCE_CAP, positions
        return min(peak / median, SEAM_PROMINENCE_CAP), positions


def _iter_page_blocks(page: "tifffile.TiffPage", accumulator: _Accumulator) -> str:
    """
    Feed ``page`` into ``accumulator``, streaming when the frame is large.

    Returns the method used, for the report — callers should surface it, because a
    ``full`` read and a ``streamed`` read have the same numbers but very different costs.
    """
    height, width = int(page.shape[-2]), int(page.shape[-1])
    if height * width <= STREAM_THRESHOLD_PIXELS:
        data = np.squeeze(page.asarray())
        if data.ndim > 2:
            data = data.reshape(-1, data.shape[-2], data.shape[-1])[0]
        accumulator.add_block(data, 0, 0)
        return "full"

    used_segments = False
    for segment in page.segments():
        data, index = segment[0], segment[1]
        if data is None:
            continue
        used_segments = True
        block = np.squeeze(np.asarray(data))
        if block.ndim > 2:
            block = block.reshape(-1, block.shape[-2], block.shape[-1])[0]
        if block.ndim != 2:
            continue
        y_off, x_off = int(index[2]), int(index[3])
        # The final strip/tile of an axis is padded to the full chunk size; clip it.
        block = block[: max(0, height - y_off), : max(0, width - x_off)]
        accumulator.add_block(block, y_off, x_off)
    if used_segments:
        return "streamed"

    data = np.squeeze(page.asarray())
    if data.ndim > 2:
        data = data.reshape(-1, data.shape[-2], data.shape[-1])[0]
    accumulator.add_block(data, 0, 0)
    return "full"


def qc_report(path: str | Path, frame: int | None = None) -> QCReport:
    """
    Measure one 2-D frame of a stitched TIFF.

    ``frame`` selects the page for a multi-channel / Z-stack file; the default is the middle
    page, which for a Z-stack is the slice most likely to be in focus and for a channel stack
    is an arbitrary but stable choice. Use :func:`qc_report` once per channel when it matters.
    """
    target = Path(path)
    report = QCReport(path=str(target), ok=False)
    if not target.exists():
        report.error = f"file does not exist: {target}"
        return report

    try:
        with tifffile.TiffFile(str(target)) as tif:
            series = tif.series[0]
            report.axes = str(series.axes)
            report.shape = tuple(int(s) for s in series.shape)
            report.dtype = str(series.dtype)
            pages = list(series.pages)
            if not pages:
                report.error = "TIFF has no pages"
                return report
            index = len(pages) // 2 if frame is None else frame
            if not 0 <= index < len(pages):
                report.error = f"frame {index} out of range (file has {len(pages)} frames)"
                return report
            page = pages[index]
            report.frame_index = index
            height, width = int(page.shape[-2]), int(page.shape[-1])
            report.width, report.height = width, height
            report.total_pixels = width * height

            accumulator = _Accumulator(height, width, np.dtype(page.dtype))
            report.method = _iter_page_blocks(page, accumulator)
    except Exception as exc:  # noqa: BLE001 - a malformed TIFF must return a report, not raise
        report.error = f"{type(exc).__name__}: {exc}"
        return report

    if accumulator.count == 0:
        report.error = "no pixel data could be decoded"
        return report

    mean = accumulator.total / accumulator.count
    variance = max(0.0, accumulator.total_sq / accumulator.count - mean * mean)
    empty_fraction = accumulator.zeros / accumulator.count
    saturated_fraction = accumulator.saturated / accumulator.count
    seam_x, seam_x_at = accumulator.seam_profile("x")
    seam_y, seam_y_at = accumulator.seam_profile("y")

    report.metrics = {
        "mean": round(mean, 2),
        "std": round(math.sqrt(variance), 2),
        "min": accumulator.minimum,
        "max": accumulator.maximum,
        "dynamic_range": accumulator.maximum - accumulator.minimum,
        "empty_fraction": round(empty_fraction, 4),
        "saturated_fraction": round(saturated_fraction, 6),
        "percentiles": {
            "p1": accumulator.percentile(1),
            "p50": accumulator.percentile(50),
            "p99": accumulator.percentile(99),
            "p99_9": accumulator.percentile(99.9),
        },
        "seam_prominence_x": round(seam_x, 2),
        "seam_prominence_y": round(seam_y, 2),
        "seam_ridges_x": seam_x_at,
        "seam_ridges_y": seam_y_at,
    }
    report.findings = _interpret(report.metrics)
    report.ok = True
    return report


def _interpret(metrics: dict[str, Any]) -> list[str]:
    """Turn numbers into statements a caller can act on. Silence means nothing stood out."""
    findings: list[str] = []
    empty = metrics["empty_fraction"]
    if empty > 0.5:
        findings.append(
            f"{empty:.0%} of the canvas is empty - most tiles did not land. Check that the raw "
            "tiles sit beside the XML and that the scene index is right."
        )
    elif empty > 0.15:
        findings.append(
            f"{empty:.0%} of the canvas is empty - expected for a non-rectangular scan, but "
            "check for missing tiles if the scan was a full rectangle."
        )

    saturated = metrics["saturated_fraction"]
    if saturated > 0.01:
        findings.append(
            f"{saturated:.2%} of pixels are clipped at the sensor maximum - signal was lost "
            "during acquisition, not during stitching."
        )

    if metrics["dynamic_range"] <= 0:
        findings.append("the frame is a single constant value - the stitch produced no image.")
    elif metrics["percentiles"]["p99"] <= metrics["percentiles"]["p1"]:
        findings.append("almost all signal sits in one intensity bin - check the shading correction.")

    for axis in ("x", "y"):
        prominence = metrics[f"seam_prominence_{axis}"]
        if prominence >= 6:
            findings.append(
                f"a hard edge runs across the {axis} axis (seam prominence {prominence:.1f}x the "
                f"typical gradient, at {axis}={metrics[f'seam_ridges_{axis}'][:3]}). Registration "
                "likely did not converge - try a different algorithm."
            )
        elif prominence >= 3:
            findings.append(
                f"visible seams along the {axis} axis (prominence {prominence:.1f}x). Blending is "
                "working but tile alignment is imperfect."
            )
    return findings


def list_outputs(directory: str | Path) -> list[dict[str, Any]]:
    """
    Stitched outputs already present in ``directory`` — so a caller can resume rather than
    re-run an hour of work. Previews are attached to the TIFF they belong to.
    """
    target = Path(directory)
    if not target.exists():
        return []
    outputs: list[dict[str, Any]] = []
    for tif_path in sorted(target.glob("stitched_*.tif")):
        try:
            stat = tif_path.stat()
        except OSError:
            continue
        preview = tif_path.with_name(tif_path.stem + "_preview.png")
        entry: dict[str, Any] = {
            "path": str(tif_path),
            "name": tif_path.name,
            "size_bytes": stat.st_size,
            "modified": stat.st_mtime,
            "preview_path": str(preview) if preview.exists() else None,
        }
        try:
            with tifffile.TiffFile(str(tif_path)) as tif:
                series = tif.series[0]
                entry["axes"] = str(series.axes)
                entry["shape"] = [int(s) for s in series.shape]
                entry["dtype"] = str(series.dtype)
        except Exception:  # noqa: BLE001 - a listing must not fail on one bad file
            entry["axes"] = None
        outputs.append(entry)
    return outputs
