"""
stage_model.py — measure a tile scan's stage geometry from the raw tiles themselves.

Why this exists (issue #12)
---------------------------
Grid-scan metadata rarely states the tile step directly. Keyence BZ-X ``.bcf`` files carry
the user-set scan-region corner points (``ImageJoint/EdgePoint0..3``); dividing their span by
``columns - 1`` gives a step that is only right if the corners are tile centres. On a BZ-X
they are not: the camera grid is a fixed-overlap lattice that overshoots the region, and the
corner-derived x-step came out 2.3 % short on real 51-column scans (1,299 px against a true
1,330 px). Every x-overlap zone of such a mosaic is a double image ~31 px apart.

The fix is to measure. Neighbouring raw tiles overlap, so their offset is observable: a
phase correlation of a sample of horizontal and vertical neighbour pairs, unwrapped to the
candidate nearest the metadata's prior, gives the true offsets to a fraction of a pixel.

Model
-----
For a grid tile at row ``r``, column ``c`` (physical position; odd rows of a serpentine scan
run the other way, which shows up as a small offset), with ``o = r % 2``::

    X(r, c) = c * sx + r * kx + o * bx
    Y(r, c) = r * sy + c * ky + o * by

``sx``/``sy`` are the steps, ``kx``/``ky`` the shears (stage-camera rotation) and ``bx``/``by``
the odd-row (serpentine/backlash) offsets. Horizontal pairs give ``sx, ky``; vertical pairs
give ``kx, bx, sy, by``, and they must come from rows of BOTH parities, or the odd-row terms
are collinear with the steps and the fit degenerates (a real failure in the first version of
the reference implementation). On 43 BZ-X scans the odd-row offsets were consistent
(x +1.2..+1.9 px, y +4.1..+5.0 px) while per-parity step differences changed sign from scan
to scan, so the latter are not modelled.

Sampling
--------
Neighbours are measured along whole CHAINS — every consecutive pair along a few grid rows
(horizontal pairs) and a few grid columns (vertical pairs). Individual row steps scatter by
about a pixel on a real stage, so a handful of scattered sites can misjudge the plate-wide
step by ~0.4 px per row (tens of pixels across 70 rows); a chain averages every step it
crosses, and each tile is read once for two pairs.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

#: Bump when the measurement or the model changes, so cached models are not reused.
MODEL_VERSION = 3

#: Pairs whose band-passed overlap correlates below this are discarded (empty background,
#: debris, or a wrong unwrap).
NCC_MIN = 0.3

#: Tiles are block-averaged by this factor before correlation: 4x fewer pixels, and the
#: upsampled phase correlation still resolves ~0.1 full-resolution px.
MEASURE_DOWNSAMPLE = 2


@dataclass
class StageModel:
    sx: float
    sy: float
    ky: float = 0.0
    kx: float = 0.0
    bx: float = 0.0
    by: float = 0.0
    n_pairs: int = 0
    n_used_horizontal: int = 0
    n_used_vertical: int = 0
    residual_sd_x: float = 0.0
    residual_sd_y: float = 0.0
    median_ncc: float = 0.0
    method: str = "measured"
    model_version: int = MODEL_VERSION

    def position(self, r: int, c: int) -> tuple[float, float]:
        o = r % 2
        x = c * self.sx + r * self.kx + o * self.bx
        y = r * self.sy + c * self.ky + o * self.by
        return float(x), float(y)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


# ---------------------------------------------------------------------------
# Pixel helpers
# ---------------------------------------------------------------------------

def _load_gray(path: Path) -> np.ndarray:
    import tifffile

    a = np.squeeze(tifffile.imread(str(path)))
    if a.ndim == 3:
        a = a.mean(axis=-1) if a.shape[-1] in (3, 4) else a.mean(axis=0)
    while a.ndim > 2:
        a = a.mean(axis=0)
    a = a.astype(np.float64, copy=False)
    f = MEASURE_DOWNSAMPLE
    if f > 1:
        h, w = (a.shape[0] // f) * f, (a.shape[1] // f) * f
        a = a[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))
    return a


def _bandpass(a: np.ndarray) -> np.ndarray:
    from scipy import ndimage as ndi

    f = MEASURE_DOWNSAMPLE
    return ndi.gaussian_filter(a, 1.0 / f) - ndi.gaussian_filter(a, 8.0 / f)


def _unwrap(shift: float, nominal: float, period: int) -> float:
    candidates = np.array([shift - period, shift, shift + period])
    return float(candidates[np.argmin(np.abs(candidates - nominal))])


def _overlap_ncc(a: np.ndarray, b: np.ndarray, dx: float, dy: float) -> float:
    """NCC of A and B over their overlap when B's origin sits at A's (dy, dx)."""
    h, w = a.shape
    ix, iy = int(round(dx)), int(round(dy))
    ax0, ay0 = max(0, ix), max(0, iy)
    ax1, ay1 = min(w, w + ix), min(h, h + iy)
    if ax1 - ax0 < 32 or ay1 - ay0 < 32:
        return float("nan")
    pa = a[ay0:ay1, ax0:ax1]
    pb = b[ay0 - iy:ay1 - iy, ax0 - ix:ax1 - ix]
    pa = pa - pa.mean()
    pb = pb - pb.mean()
    den = float(np.sqrt((pa * pa).sum() * (pb * pb).sum()))
    return float((pa * pb).sum() / den) if den > 0 else float("nan")


def _offset(a: np.ndarray, b: np.ndarray, nominal_dx: float, nominal_dy: float) -> tuple[float, float, float]:
    from skimage.registration import phase_cross_correlation

    f = MEASURE_DOWNSAMPLE
    h, w = a.shape
    shift, _, _ = phase_cross_correlation(a, b, upsample_factor=20, normalization=None)
    dy = _unwrap(float(shift[0]), nominal_dy / f, h)
    dx = _unwrap(float(shift[1]), nominal_dx / f, w)
    return dx * f, dy * f, _overlap_ncc(a, b, dx, dy)


# ---------------------------------------------------------------------------
# Sampling and fitting
# ---------------------------------------------------------------------------

def _spread(lo: int, hi: int, n: int) -> list[int]:
    """Up to ``n`` distinct integers evenly spread over [lo, hi]."""
    if hi < lo:
        return []
    if hi - lo + 1 <= n:
        return list(range(lo, hi + 1))
    return sorted({int(round(v)) for v in np.linspace(lo, hi, n)})


def sample_chains(
    grid: dict[tuple[int, int], str], n_rows: int = 4, n_cols: int = 2, segment: int = 12,
) -> list[list[tuple[int, int]]]:
    """
    Chains of grid sites to read in order; every consecutive pair in a chain is measured.

    Horizontal chains run along ``n_rows`` rows of alternating parity, vertical chains down
    ``n_cols`` columns. Chains are cut into segments of ``segment`` tiles (sharing their end
    tile) so they can be measured in parallel.
    """
    rows = sorted({r for r, _ in grid})
    cols = sorted({c for _, c in grid})
    chains: list[list[tuple[int, int]]] = []
    if len(cols) >= 2:
        picks = _spread(rows[0], rows[-1], n_rows) if len(rows) > n_rows else list(rows)
        if len(rows) > n_rows:
            fracs = np.linspace(0.2, 0.8, n_rows) if len(rows) >= 10 else np.linspace(0, 1, n_rows)
            picks = []
            for i, f in enumerate(fracs):
                r = int(round(rows[0] + f * (rows[-1] - rows[0])))
                if r % 2 != i % 2:
                    r = r + 1 if r + 1 <= rows[-1] else r - 1
                if r not in picks:
                    picks.append(r)
        for r in picks:
            chains.append([(r, c) for c in cols if (r, c) in grid])
    if len(rows) >= 2:
        if len(cols) > n_cols:
            fracs = np.linspace(1 / 3, 2 / 3, n_cols) if len(cols) >= 6 else np.linspace(0, 1, n_cols)
            pick_c = sorted({int(round(cols[0] + f * (cols[-1] - cols[0]))) for f in fracs})
        else:
            pick_c = list(cols)
        for c in pick_c:
            chains.append([(r, c) for r in rows if (r, c) in grid])
    out: list[list[tuple[int, int]]] = []
    for chain in chains:
        if len(chain) < 2:
            continue
        i = 0
        while i < len(chain) - 1:
            out.append(chain[i:i + segment])
            i += segment - 1
    return out


def _trimmed_lstsq(X: np.ndarray, yx: np.ndarray, yy: np.ndarray):
    keep = np.ones(len(yx), bool)
    px = py = None
    for _ in range(5):
        px, *_ = np.linalg.lstsq(X[keep], yx[keep], rcond=None)
        py, *_ = np.linalg.lstsq(X[keep], yy[keep], rcond=None)
        rx = yx - X @ px
        ry = yy - X @ py
        rr = np.hypot(rx, ry)
        mad = 1.4826 * np.median(np.abs(rr[keep] - np.median(rr[keep]))) + 0.05
        new = rr < np.median(rr[keep]) + 3 * mad
        if new.sum() < X.shape[1] + 1 or (new == keep).all():
            break
        keep = new
    return px, py, rx, ry, keep


def fit_stage_model(pairs: list[dict]) -> tuple[StageModel | None, str]:
    """
    Fit the model to measured neighbour offsets.

    ``pairs`` items: ``{r, c, r2, c2, dx, dy, ncc}``. Returns ``(model, "")`` or
    ``(None, reason)``.
    """
    good = [p for p in pairs if np.isfinite(p["ncc"]) and p["ncc"] >= NCC_MIN]
    hor = [p for p in good if p["r2"] == p["r"] and p["c2"] == p["c"] + 1]
    ver = [p for p in good if p["c2"] == p["c"] and p["r2"] == p["r"] + 1]
    if len(hor) < 3 or len(ver) < 3:
        return None, (
            f"too few usable neighbour pairs ({len(hor)} horizontal, {len(ver)} vertical with "
            f"NCC >= {NCC_MIN}; at least 3 of each are needed) - the tiles may be empty or "
            "featureless"
        )

    # Horizontal: dx = sx, dy = ky
    Xh = np.ones((len(hor), 1))
    pxh, pyh, rxh, ryh, kh = _trimmed_lstsq(
        Xh, np.array([p["dx"] for p in hor]), np.array([p["dy"] for p in hor]))
    sx, ky = float(pxh[0]), float(pyh[0])

    # Vertical: dx = kx + bx*d, dy = sy + by*d, with d = odd(r+1) - odd(r) = +1 / -1
    d = np.array([(p["r2"] % 2) - (p["r"] % 2) for p in ver], float)
    both = (d > 0).any() and (d < 0).any()
    Xv = np.c_[np.ones(len(ver)), d] if both else np.ones((len(ver), 1))
    pxv, pyv, rxv, ryv, kv = _trimmed_lstsq(
        Xv, np.array([p["dx"] for p in ver]), np.array([p["dy"] for p in ver]))
    kx, sy = float(pxv[0]), float(pyv[0])
    # With one parity only (a 2-row grid), kx/sy absorb the odd-row offset, which is exact
    # for the only odd row such a grid has.
    bx, by = (float(pxv[1]), float(pyv[1])) if both else (0.0, 0.0)

    res_x = np.r_[rxh[kh], rxv[kv]]
    res_y = np.r_[ryh[kh], ryv[kv]]
    model = StageModel(
        sx=sx, sy=sy, ky=ky, kx=kx, bx=bx, by=by,
        n_pairs=len(pairs), n_used_horizontal=int(kh.sum()), n_used_vertical=int(kv.sum()),
        residual_sd_x=float(np.std(res_x)), residual_sd_y=float(np.std(res_y)),
        median_ncc=float(np.median([p["ncc"] for p in good])),
    )
    if max(model.residual_sd_x, model.residual_sd_y) > 3.0:
        return None, (
            f"neighbour offsets do not fit a rigid grid (residual SD {model.residual_sd_x:.2f} / "
            f"{model.residual_sd_y:.2f} px); the stage model is not trustworthy"
        )
    return model, ""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_dir() -> Path | None:
    if os.environ.get("AXIO_STITCHING_CACHE", "1") == "0":
        return None
    explicit = os.environ.get("AXIO_STITCHING_CACHE_DIR")
    if explicit:
        return Path(explicit)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "axio_stitching" / "cache"


def _cache_key(metadata_file: Path | None, raw_dir: Path, grid: dict, chains: list, prior: tuple) -> str:
    h = hashlib.sha256()
    h.update(f"v{MODEL_VERSION}|{os.path.abspath(raw_dir)}|{prior}|".encode())
    if metadata_file is not None and metadata_file.exists():
        h.update(hashlib.sha256(metadata_file.read_bytes()).digest())
    for chain in chains:
        for key in chain:
            name = grid.get(key)
            try:
                st = (Path(raw_dir) / name).stat()
                h.update(f"{name}|{st.st_size}|{int(st.st_mtime)}|".encode())
            except OSError:
                h.update(f"{name}|missing|".encode())
    return h.hexdigest()[:32]


def _cache_get(key: str) -> dict | None:
    d = _cache_dir()
    if d is None:
        return None
    try:
        return json.loads((d / f"stage_model_{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_put(key: str, payload: dict) -> None:
    d = _cache_dir()
    if d is None:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"stage_model_{key}.json").write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def measure_stage_model(
    grid: dict[tuple[int, int], str],
    raw_dir: Path,
    prior_step: tuple[float, float],
    *,
    metadata_file: Path | None = None,
    n_rows: int = 4,
    n_cols: int = 2,
    workers: int | None = None,
) -> tuple[StageModel | None, str]:
    """
    Measure the stage model of a grid scan from its raw tiles (read-only).

    Args:
        grid: ``{(row, col): filename}`` with physical grid positions.
        raw_dir: Directory the filenames are relative to.
        prior_step: ``(step_x, step_y)`` from metadata, used only to unwrap the periodic
            phase-correlation shifts; it may be off by a large fraction of the overlap.
        metadata_file: The scan's metadata file, hashed into the cache key.

    Returns:
        ``(model, "")`` or ``(None, reason)``. Never raises for data problems.
    """
    chains = sample_chains(grid, n_rows=n_rows, n_cols=n_cols)
    if not chains:
        return None, "the grid is too small to measure neighbour offsets"

    key = _cache_key(metadata_file, raw_dir, grid, chains, tuple(round(v, 3) for v in prior_step))
    cached = _cache_get(key)
    if cached and cached.get("model_version") == MODEL_VERSION:
        if cached.get("model"):
            m = StageModel(**cached["model"])
            m.method = "measured"
            return m, ""
        return None, cached.get("reason", "cached measurement failed")

    psx, psy = float(prior_step[0]), float(prior_step[1])

    def measure_chain(chain: list[tuple[int, int]]) -> list[dict]:
        out: list[dict] = []
        prev = prev_img = None
        for site in chain:
            try:
                img = _bandpass(_load_gray(Path(raw_dir) / grid[site]))
            except Exception:  # noqa: BLE001 - a missing/unreadable tile only breaks its pairs
                prev, prev_img = None, None
                continue
            if prev is not None and prev_img is not None and prev_img.shape == img.shape:
                (r, c), (r2, c2) = prev, site
                nominal = (psx * (c2 - c), psy * (r2 - r))
                try:
                    dx, dy, ncc = _offset(prev_img, img, *nominal)
                    out.append({"r": r, "c": c, "r2": r2, "c2": c2, "dx": dx, "dy": dy, "ncc": ncc})
                except Exception:  # noqa: BLE001
                    pass
            prev, prev_img = site, img
        return out

    n_workers = workers or min(8, os.cpu_count() or 1)
    pairs: list[dict] = []
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for result in pool.map(measure_chain, chains):
            pairs.extend(result)
    if not pairs:
        model, reason = None, "no neighbour tile could be read (are the tile files next to the metadata?)"
    else:
        model, reason = fit_stage_model(pairs)
    # Only cache outcomes that came from readable tiles.
    if pairs:
        _cache_put(key, {"model_version": MODEL_VERSION,
                         "model": model.to_dict() if model else None, "reason": reason})
    return model, reason


def layout(grid: dict[tuple[int, int], str], model: StageModel) -> dict[str, tuple[float, float]]:
    """``{filename: (x, y)}`` from the model, shifted so the smallest x and y are 0."""
    pos = {name: model.position(r, c) for (r, c), name in grid.items()}
    x0 = min(x for x, _ in pos.values())
    y0 = min(y for _, y in pos.values())
    return {name: (x - x0, y - y0) for name, (x, y) in pos.items()}
