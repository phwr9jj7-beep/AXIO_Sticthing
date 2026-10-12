"""
ghost.py — detect double images in the tile overlap zones of a stitched mosaic.

Why this exists (issue #12)
---------------------------
A wrong tile step does not produce a visible seam. Feathered blending averages the two tiles
that cover an overlap zone, so when they are placed a few pixels apart the zone becomes a
smooth blend of two shifted copies of the sample: a *ghost*. The gradient-ridge "seam
prominence" in :mod:`axio_stitching.qc` is blind to that (on real BZ-X plates it was 21-24
for both the broken and the corrected mosaics, dominated by canvas borders and wells).

A ghost has a signature that a single tile cannot have: the image correlates with itself at
the ghost offset. So the test compares, at lags along each axis, the autocorrelation of
band-passed intensity inside overlap zones with that inside single-tile zones of the same
mosaic:

    excess(L) = median ACF_overlap(L) - median ACF_single(L),   L in [LAG_MIN, LAG_MAX]

and reports the maximum and its lag. A correct mosaic scores ~0 (the two zone types differ
only in noise averaging, which the band-pass and LAG_MIN keep out of the window); a ghosted
one scores well above it, at the lag of the placement error.

Only the textured half of each zone type is used (empty background has no texture to echo), and
the layout comes from the ``<mosaic>_positions.json`` sidecar that AXIO >= 1.3 writes next to
every mosaic (or from a positions file passed explicitly).
"""

from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: Lags (mosaic pixels) searched for a secondary autocorrelation peak. Below LAG_MIN the
#: band-passed texture itself still correlates and noise averaging in overlaps biases the
#: comparison; ghosts of a few pixels need the full-resolution mosaic.
LAG_MIN = 8
LAG_MAX = 64
#: Interpretation thresholds for the excess.
GHOST_FAIL = 0.15
GHOST_WARN = 0.05
#: Band-pass (difference of Gaussians, pixels) applied before the autocorrelation.
BANDPASS = (1.0, 4.0)
#: At most this many tile rows are sampled for each axis.
MAX_BANDS_X = 10
MAX_BANDS_Y = 6
#: Zones narrower than this (mosaic pixels) are not analysed.
MIN_ZONE = 2 * LAG_MIN + 8


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def find_positions_sidecar(mosaic: Path) -> tuple[Path | None, float]:
    """``(sidecar, scale)`` for a mosaic, also for its ``*_imagej_dsN`` overview."""
    m = re.match(r"^(?P<base>.*)_imagej_ds(?P<n>\d+)$", mosaic.stem)
    if m:
        sidecar = mosaic.with_name(m.group("base") + "_positions.json")
        return (sidecar if sidecar.exists() else None), 1.0 / int(m.group("n"))
    sidecar = mosaic.with_name(mosaic.stem + "_positions.json")
    return (sidecar if sidecar.exists() else None), 1.0


def load_layout(path: Path, image_w: int, image_h: int, scale: float | None = None) -> list[tuple[float, float, float, float]]:
    """
    Tile rectangles ``(x, y, w, h)`` in the pixels of the image being measured.

    Accepts an AXIO positions sidecar (canvas pixels, with ``canvas`` size — the scale is then
    taken from the image width) or any positions JSON (``tiles``/``positions`` list or a bare
    list of ``{x, y[, w, h]}``), which is shifted so the smallest x and y are 0.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tiles = data.get("tiles") or data.get("positions") if isinstance(data, dict) else data
    if not tiles:
        raise ValueError(f"{path} contains no tile positions")
    tw = float((data.get("tile_width") if isinstance(data, dict) else None) or tiles[0].get("w") or 0)
    th = float((data.get("tile_height") if isinstance(data, dict) else None) or tiles[0].get("h") or 0)
    xs = [float(t["x"]) for t in tiles]
    ys = [float(t["y"]) for t in tiles]
    x0, y0 = min(xs), min(ys)
    if isinstance(data, dict) and data.get("canvas", {}).get("width"):
        scale = image_w / float(data["canvas"]["width"])
    elif scale is None:
        span = (max(xs) - x0) + (tw or 0)
        scale = image_w / span if span > 0 and tw else 1.0
    rects = []
    for t, x, y in zip(tiles, xs, ys):
        w = float(t.get("w") or tw)
        h = float(t.get("h") or th)
        if not w or not h:
            raise ValueError(f"{path}: tile size unknown (give w/h per tile or tile_width/height)")
        rects.append(((x - x0) * scale, (y - y0) * scale, w * scale, h * scale))
    return rects


def _rows(rects: list[tuple[float, float, float, float]]) -> list[list[tuple[float, float, float, float]]]:
    """Group tiles into grid rows (by y), each sorted by x."""
    ordered = sorted(rects, key=lambda r: r[1])
    rows: list[list] = []
    for rect in ordered:
        if rows and rect[1] - rows[-1][0][1] < rect[3] / 2:
            rows[-1].append(rect)
        else:
            rows.append([rect])
    return [sorted(row, key=lambda r: r[0]) for row in rows]


def _x_zones(row: list) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Overlap and single-tile column intervals along one tile row."""
    overlaps, singles = [], []
    for i, (x, _y, w, _h) in enumerate(row):
        left = row[i - 1][0] + row[i - 1][2] if i > 0 else x
        right = row[i + 1][0] if i + 1 < len(row) else x + w
        lo, hi = int(np.ceil(max(x, left))), int(np.floor(min(x + w, right)))
        if hi - lo >= MIN_ZONE:
            singles.append((lo, hi))
        if i + 1 < len(row):
            olo, ohi = int(np.ceil(row[i + 1][0])), int(np.floor(x + w))
            if ohi - olo >= MIN_ZONE:
                overlaps.append((olo, ohi))
    return overlaps, singles


@dataclass
class _Band:
    axis: str                       # 'x' or 'y'
    y0: int
    y1: int
    cols: list[tuple[int, int]]     # column intervals stored
    zones: list[tuple[str, int, int, int, int]]   # (kind, y0, y1, x0, x1) in image px
    data: list[np.ndarray] = field(default_factory=list)
    filled: int = 0

    @property
    def expected(self) -> int:
        return (self.y1 - self.y0) * sum(b - a for a, b in self.cols)


def plan_bands(rects, image_w: int, image_h: int) -> list[_Band]:
    rows = _rows(rects)
    bands: list[_Band] = []
    if len(rows) == 0:
        return bands

    def y_single(i: int) -> tuple[int, int]:
        top = max(r[1] for r in rows[i])
        bottom = min(r[1] + r[3] for r in rows[i])
        if i > 0:
            top = max(top, max(r[1] + r[3] for r in rows[i - 1]))
        if i + 1 < len(rows):
            bottom = min(bottom, min(r[1] for r in rows[i + 1]))
        return int(np.ceil(top)), int(np.floor(bottom))

    # x-bands: a strip inside each sampled row's single-tile height, full width
    step = max(1, int(np.ceil(len(rows) / MAX_BANDS_X)))
    for i in range(0, len(rows), step):
        lo, hi = y_single(i)
        lo, hi = max(lo, 0), min(hi, image_h)
        if hi - lo < 16:
            continue
        height = min(hi - lo, 256)
        y0 = lo + (hi - lo - height) // 2
        overlaps, singles = _x_zones(rows[i])
        zones = [("overlap", y0, y0 + height, a, min(b, image_w)) for a, b in overlaps]
        zones += [("single", y0, y0 + height, a, min(b, image_w)) for a, b in singles]
        zones = [z for z in zones if z[4] - z[3] >= MIN_ZONE]
        if zones:
            bands.append(_Band("x", y0, y0 + height, [(0, image_w)], zones))

    # y-bands: from the single zone of row i to that of row i+1, in columns that are
    # single-tile in BOTH rows; the y-overlap rows between them are the 'overlap' zone.
    if len(rows) >= 2:
        step = max(1, int(np.ceil((len(rows) - 1) / MAX_BANDS_Y)))
        for i in range(0, len(rows) - 1, step):
            a_lo, a_hi = y_single(i)
            b_lo, b_hi = y_single(i + 1)
            ov_lo = int(np.ceil(min(r[1] for r in rows[i + 1])))
            ov_hi = int(np.floor(max(r[1] + r[3] for r in rows[i])))
            if ov_hi - ov_lo < MIN_ZONE or a_hi - a_lo < MIN_ZONE or b_hi - b_lo < MIN_ZONE:
                continue
            _, sa = _x_zones(rows[i])
            _, sb = _x_zones(rows[i + 1])
            cols = []
            for x0, x1 in sa:
                for u0, u1 in sb:
                    lo, hi = max(x0, u0), min(x1, u1, image_w)
                    if hi - lo >= 32:
                        cols.append((lo, hi))
            if not cols:
                continue
            y0, y1 = max(a_lo, 0), min(b_hi, image_h)
            zones = []
            for x0, x1 in cols:
                zones.append(("overlap", max(ov_lo, y0), min(ov_hi, y1), x0, x1))
                zones.append(("single", max(a_lo, y0), min(a_hi, ov_lo), x0, x1))
                zones.append(("single", max(ov_hi, b_lo), min(b_hi, y1), x0, x1))
            zones = [z for z in zones if z[2] - z[1] >= MIN_ZONE]
            if zones:
                bands.append(_Band("y", y0, y1, cols, zones))
    return bands


# ---------------------------------------------------------------------------
# Streaming collector (fed by qc._iter_page_blocks)
# ---------------------------------------------------------------------------

def _acf(zone: np.ndarray, axis: int, lag_max: int) -> tuple[float, np.ndarray] | None:
    """(texture variance, normalised unbiased ACF for lags 0..lag_max) along ``axis``."""
    z = np.moveaxis(zone, axis, -1)
    n = z.shape[-1]
    if n < 2 * lag_max + 2 or z.shape[0] < 4:
        return None
    z = z - z.mean(axis=-1, keepdims=True)
    spec = np.fft.rfft(z, n=2 * n, axis=-1)
    full = np.fft.irfft(spec * np.conj(spec), n=2 * n, axis=-1)[..., : lag_max + 1].sum(axis=0)
    unbiased = full / (n - np.arange(lag_max + 1))
    if unbiased[0] <= 0:
        return None
    return float(unbiased[0] / z.shape[0]), unbiased / unbiased[0]


class GhostCollector:
    """Collects the planned bands from streamed blocks and analyses each when complete."""

    def __init__(self, bands: list[_Band], dtype: np.dtype) -> None:
        self.bands = bands
        self.dtype = dtype
        self.curves: dict[str, dict[str, list[tuple[float, np.ndarray]]]] = {
            "x": {"overlap": [], "single": []}, "y": {"overlap": [], "single": []}}
        # Bands are allocated when their first rows arrive and freed once analysed, so only
        # the one or two bands the stream is currently passing through are held in memory.
        self._store = np.dtype(dtype) if np.dtype(dtype).itemsize <= 2 else np.dtype(np.float32)

    def add_block(self, block: np.ndarray, y_off: int, x_off: int) -> None:
        bh, bw = block.shape
        for band in self.bands:
            if band.filled >= band.expected or y_off >= band.y1 or y_off + bh <= band.y0:
                continue
            r0, r1 = max(band.y0, y_off), min(band.y1, y_off + bh)
            if not band.data:
                band.data = [np.zeros((band.y1 - band.y0, b - a), dtype=self._store)
                             for a, b in band.cols]
            for (a, b), arr in zip(band.cols, band.data):
                c0, c1 = max(a, x_off), min(b, x_off + bw)
                if c1 <= c0:
                    continue
                arr[r0 - band.y0:r1 - band.y0, c0 - a:c1 - a] = block[r0 - y_off:r1 - y_off, c0 - x_off:c1 - x_off]
                band.filled += (r1 - r0) * (c1 - c0)
            if band.filled >= band.expected:
                self._analyse(band)

    def _analyse(self, band: _Band) -> None:
        from scipy import ndimage as ndi

        s1, s2 = BANDPASS
        for (a, b), arr in zip(band.cols, band.data):
            arr = arr.astype(np.float32, copy=False)
            bp = ndi.gaussian_filter(arr, s1) - ndi.gaussian_filter(arr, s2)
            for kind, zy0, zy1, zx0, zx1 in band.zones:
                if zx0 < a or zx1 > b:
                    continue
                zone = bp[zy0 - band.y0:zy1 - band.y0, zx0 - a:zx1 - a]
                axis = 1 if band.axis == "x" else 0
                length = zone.shape[axis]
                lag_max = min(LAG_MAX, (length - 2) // 2)
                if lag_max < LAG_MIN:
                    continue
                res = _acf(zone, axis, LAG_MAX if lag_max >= LAG_MAX else lag_max)
                if res is not None:
                    var, curve = res
                    padded = np.full(LAG_MAX + 1, np.nan)
                    padded[: curve.size] = curve
                    self.curves[band.axis][kind].append((var, padded))
        band.data = []

    def result(self) -> dict:
        out: dict = {}
        for axis in ("x", "y"):
            groups = self.curves[axis]
            if not groups["overlap"] or not groups["single"]:
                out[axis] = None
                continue
            # The textured half of EACH zone type: a ghosted overlap is a blend of two
            # misaligned copies and loses variance, so a shared threshold would discard
            # exactly the zones that carry the ghost.
            kept = {}
            for k in groups:
                threshold = float(np.median([v for v, _ in groups[k]]))
                kept[k] = np.array([c for v, c in groups[k] if v >= threshold])
            if kept["overlap"].size == 0 or kept["single"].size == 0:
                out[axis] = None
                continue
            with warnings.catch_warnings():   # lags beyond a narrow zone's reach are NaN
                warnings.simplefilter("ignore", RuntimeWarning)
                med_ov = np.nanmedian(kept["overlap"], axis=0)
                med_si = np.nanmedian(kept["single"], axis=0)
            excess = med_ov - med_si
            window = excess[LAG_MIN:]
            if np.all(np.isnan(window)):
                out[axis] = None
                continue
            k = int(np.nanargmax(window))
            out[axis] = {
                "excess": round(float(window[k]), 4),
                "lag": LAG_MIN + k,
                "zones_overlap": int(kept["overlap"].shape[0]),
                "zones_single": int(kept["single"].shape[0]),
            }
        return out
