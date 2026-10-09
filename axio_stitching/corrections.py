"""
corrections.py
--------------
Illumination / shading correction engines for AXIO tile scans.

Extracted verbatim from gui_runner.py (correct_scene_shading function,
lines 141-405). The three code paths (split-channel, multi-page stack,
spatial) are unified under a single run_correction() interface while
preserving exact algorithmic behavior.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import numpy as np
import scipy.ndimage
import tifffile

from .canvas import detect_tile_axes, read_tile_frame
from .models import ProgressCallback, ProgressEvent, PipelineStage



def _log(message: str) -> None:
    """
    Emit a diagnostic line on STDERR.

    Never stdout: when this pipeline runs as an MCP stdio server, stdout IS the JSON-RPC
    transport and a stray line corrupts the frame stream. The GUI worker merges stderr into
    stdout, so its log view is unaffected.
    """
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Output-path safety
# ---------------------------------------------------------------------------

class CorrectionPathError(ValueError):
    """A corrected tile would be written outside the correction directory or onto its source."""


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_inside(child: Path, parent: Path) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep + "/") + os.sep)


def _same_file(a: Path, b: Path) -> bool:
    if _norm(a) == _norm(b):
        return True
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def corrected_tile_path(out_dir: Path, raw_dir: Path, filename: str) -> Path:
    """
    Where the corrected copy of ``filename`` goes — or a :class:`CorrectionPathError`.

    Tile names are joined onto ``out_dir``. ``pathlib`` silently DROPS the left operand when
    the right one is absolute, so an absolute tile name (common in hand-written positions
    JSONs) would turn ``out_dir / name`` into the raw tile itself: the "already corrected"
    check then sees the raw file and skips the correction, and only an existence check stands
    between the writer and the raw data. Every output path is therefore checked to be a
    RELATIVE name that stays inside ``out_dir`` and is not the source tile.
    """
    rel = Path(filename)
    if rel.is_absolute() or rel.drive or rel.root:
        raise CorrectionPathError(
            f"refusing to write a corrected tile for the absolute tile path {filename!r}: "
            "correction outputs are named after the tile, so the tile names must be relative "
            "to the tile directory. Use relative names (or put the positions file next to the "
            "tiles), or use correction='none'."
        )
    out_p = Path(os.path.abspath(Path(out_dir) / rel))
    if not _is_inside(out_p, Path(out_dir)) or _norm(out_p) == _norm(out_dir):
        raise CorrectionPathError(f"refusing to write outside {out_dir}: {out_p}")
    if _same_file(out_p, Path(raw_dir) / rel):
        raise CorrectionPathError(
            f"refusing to overwrite the source tile {out_p}: the correction directory resolves "
            "to the tile directory"
        )
    return out_p


def _preflight_output_paths(raw_dir: Path, filenames: list[str], out_dir: Path) -> None:
    """Fail BEFORE any flatfield is fitted if a single output path would be unsafe."""
    if _same_file(Path(out_dir), Path(raw_dir)) or _norm(out_dir) == _norm(raw_dir):
        raise CorrectionPathError(
            f"the correction output directory {out_dir} is the tile directory; corrected tiles "
            "would overwrite the raw tiles"
        )
    for fn in filenames:
        corrected_tile_path(out_dir, raw_dir, fn)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def run_correction(
    raw_dir: Path,
    tile_list: list[dict],
    out_dir: Path,
    method: str = "basicpy",
    ref_channel_idx: int = 0,
    ref_tag: str = "",
    target_tags: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """
    Apply shading/flatfield correction to all tiles and write results to out_dir.

    Parameters mirror the correct_scene_shading() signature from gui_runner.py.

    Returns the correction output directory (same as out_dir, or raw_dir if method='none').
    """
    def _progress(percent: int, msg: str = "") -> None:
        if progress_callback:
            progress_callback(ProgressEvent(
                percent=percent, status_message=msg, stage=PipelineStage.CORRECTION
            ))
        else:
            _log(f"[STATUS] {msg}")
            _log(f"[PROGRESS] {percent}")

    if method == "none":
        _progress(50, "Shading correction skipped. Stitching on raw tiles.")
        return raw_dir

    # Path safety first, before any directory is created or any flatfield is fitted.
    if ref_tag:
        names = [
            t["filename"].replace(ref_tag, tag)
            for tag in [ref_tag] + list(target_tags or [])
            for t in tile_list
        ]
    else:
        names = [t["filename"] for t in tile_list]
    _preflight_output_paths(Path(raw_dir), names, Path(out_dir))
    if _is_inside(Path(out_dir), Path(raw_dir)):
        _progress(1, f"[warning] corrected tiles will be written inside the tile directory "
                     f"({out_dir}); the raw tiles are not modified, but consider an out_dir "
                     "outside the raw data.")

    out_dir.mkdir(parents=True, exist_ok=True)

    if method == "basicpy":
        os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
        try:
            from basicpy import BaSiC
        except ImportError:
            raise ImportError("BaSiCPy package is required but not installed. Run: pip install basicpy")

    if ref_tag:
        _run_split_channel_correction(
            raw_dir, tile_list, out_dir, method,
            ref_tag=ref_tag, target_tags=target_tags or [],
            progress_fn=_progress,
        )
    else:
        _run_stack_correction(
            raw_dir, tile_list, out_dir, method,
            ref_channel_idx=ref_channel_idx,
            progress_fn=_progress,
        )

    return out_dir


# ---------------------------------------------------------------------------
# Split-channel flow (ref_tag present)
# ---------------------------------------------------------------------------

def _run_split_channel_correction(
    raw_dir: Path,
    tile_list: list[dict],
    out_dir: Path,
    method: str,
    ref_tag: str,
    target_tags: list[str],
    progress_fn: Callable,
) -> None:
    """
    Preserved verbatim from gui_runner.py lines 159-268.
    Handles per-tag BaSiCPy / median / spatial corrections for split-channel TIFFs.
    """
    all_tags = [ref_tag] + target_tags
    progress_fn(0, f"Split-channel {method} correction for tags: {all_tags}")

    for tag_idx, tag in enumerate(all_tags):
        progress_fn(0, f"Processing shading correction ({method}) for tag '{tag}'...")

        tag_tiles = [t["filename"].replace(ref_tag, tag) for t in tile_list]
        todo_tiles = [fn for fn in tag_tiles
                      if not corrected_tile_path(out_dir, raw_dir, fn).exists()]

        if not todo_tiles:
            progress_fn(0, f"[warning] all {len(tag_tiles)} corrected tiles for tag '{tag}' "
                           f"already exist in {out_dir}; reusing them. Delete that folder to "
                           "recompute the correction.")
            continue

        if method in ["basicpy", "median"]:
            if method == "basicpy":  # median needs only numpy/scipy
                from basicpy import BaSiC

            np.random.seed(42)
            sample_size = min(len(tag_tiles), 300)
            sample_filenames = np.random.choice(tag_tiles, sample_size, replace=False)

            images_for_fit = []
            for idx, fn in enumerate(sample_filenames):
                p = raw_dir / fn
                if p.exists():
                    images_for_fit.append(np.squeeze(tifffile.imread(str(p))))
                if idx % 10 == 0:
                    progress_fn(int(5 + (idx / sample_size) * 15), f"Loading samples for tag '{tag}'...")

            if not images_for_fit:
                progress_fn(0, f"Warning: No source tiles found for tag '{tag}'. Skipping.")
                continue

            images_for_fit = np.array(images_for_fit)

            if method == "basicpy":
                progress_fn(0, f"Fitting BaSiCPy flatfield for tag '{tag}'...")
                basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
                basic.fit(images_for_fit)
                flatfield = basic.flatfield + 1e-6
                ff_mean = 1.0
            else:  # median
                progress_fn(0, f"Fitting Median flatfield for tag '{tag}'...")
                flatfield = np.nanmedian(images_for_fit, axis=0)
                flatfield = scipy.ndimage.gaussian_filter(flatfield, sigma=50)
                flatfield = flatfield + 1e-6
                ff_mean = flatfield.mean()

            progress_fn(0, f"Applying flatfield correction ({method}) for tag '{tag}'...")
            n_tiles = len(tag_tiles)
            for idx, fn in enumerate(tag_tiles):
                in_p = raw_dir / fn
                out_p = corrected_tile_path(out_dir, raw_dir, fn)
                if not in_p.exists() or out_p.exists():
                    continue

                raw = tifffile.imread(str(in_p))
                raw_sq = np.squeeze(raw)
                if method == "basicpy":
                    corrected = raw_sq / flatfield
                else:
                    corrected = raw_sq / (flatfield / ff_mean)

                if raw.dtype == np.uint8:
                    corrected = np.clip(corrected, 0, 255).astype(np.uint8)
                else:
                    corrected = np.clip(corrected, 0, 65535).astype(np.uint16)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                tifffile.imwrite(str(out_p), corrected, compression="deflate")

                if idx % max(1, n_tiles // 20) == 0:
                    progress_base = 20 + int((tag_idx / len(all_tags)) * 30)
                    progress_span = int(30 / len(all_tags))
                    progress_fn(int(progress_base + (idx / n_tiles) * progress_span), "")

        else:
            # Spatial rolling ball — split channel
            progress_fn(0, f"Applying spatial rolling-ball correction for tag '{tag}'...")
            n_tiles = len(tag_tiles)
            for idx, fn in enumerate(tag_tiles):
                in_p = raw_dir / fn
                out_p = corrected_tile_path(out_dir, raw_dir, fn)
                if not in_p.exists() or out_p.exists():
                    continue

                raw = tifffile.imread(str(in_p))
                raw_sq = np.squeeze(raw).astype(np.float32)
                h, w = raw_sq.shape
                ball_r = min(h, w) // 8
                bg = scipy.ndimage.grey_erosion(raw_sq, size=(ball_r, ball_r))
                bg = scipy.ndimage.gaussian_filter(bg, sigma=ball_r // 2)
                bg_median = np.median(bg)
                corrected = raw_sq - bg + bg_median

                if raw.dtype == np.uint8:
                    corrected = np.clip(corrected, 0, 255).astype(np.uint8)
                else:
                    corrected = np.clip(corrected, 0, 65535).astype(np.uint16)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                tifffile.imwrite(str(out_p), corrected, compression="deflate")

                if idx % max(1, n_tiles // 20) == 0:
                    progress_base = 20 + int((tag_idx / len(all_tags)) * 30)
                    progress_span = int(30 / len(all_tags))
                    progress_fn(int(progress_base + (idx / n_tiles) * progress_span), "")


# ---------------------------------------------------------------------------
# Multi-page stack flow (no ref_tag)
# ---------------------------------------------------------------------------

def _run_stack_correction(
    raw_dir: Path,
    tile_list: list[dict],
    out_dir: Path,
    method: str,
    ref_channel_idx: int,
    progress_fn: Callable,
) -> None:
    """
    Preserved verbatim from gui_runner.py lines 270-405.
    Handles per-channel BaSiCPy / median / spatial corrections for multi-page TIFF stacks.
    """
    sample_path = raw_dir / tile_list[0]["filename"]
    if not sample_path.exists():
        raise FileNotFoundError(f"Source tile not found: {sample_path}")

    sample_info = detect_tile_axes(sample_path)
    num_channels = sample_info["num_channels"]
    axes_str = sample_info["axes"]
    channel_axis = axes_str.find("C") if "C" in axes_str else None

    if all(corrected_tile_path(out_dir, raw_dir, t["filename"]).exists() for t in tile_list):
        progress_fn(50, f"[warning] all {len(tile_list)} corrected tiles ({method}) already "
                        f"exist in {out_dir}; reusing them. Delete that folder to recompute "
                        "the correction.")
        return

    if method in ["basicpy", "median"]:
        if method == "basicpy":  # median needs only numpy/scipy
            from basicpy import BaSiC

        progress_fn(0, f"Multi-page stack detected with {num_channels} channels. Fitting flatfield for each channel...")
        flatfields = []
        ff_means = []

        for c in range(num_channels):
            progress_fn(0, f"Fitting flatfield for channel {c}...")
            np.random.seed(42)
            sample_size = min(len(tile_list), 300)
            sample_tiles = np.random.choice(tile_list, sample_size, replace=False)

            images_for_fit = []
            for idx, t in enumerate(sample_tiles):
                p = raw_dir / t["filename"]
                if p.exists():
                    images_for_fit.append(
                        read_tile_frame(p, channel_idx=c, z_idx=0,
                                        channel_mode="reference", z_mode="slice")
                    )
                if idx % 10 == 0:
                    progress_fn(int(5 + (idx / sample_size) * 15), f"Loading samples ch{c}...")

            if not images_for_fit:
                raise FileNotFoundError(f"Could not load any tile frames for channel {c}")

            images_for_fit = np.array(images_for_fit)
            if method == "basicpy":
                basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
                basic.fit(images_for_fit)
                flatfields.append(basic.flatfield + 1e-6)
                ff_means.append(1.0)
            else:
                flatfield = np.nanmedian(images_for_fit, axis=0)
                flatfield = scipy.ndimage.gaussian_filter(flatfield, sigma=50)
                flatfields.append(flatfield + 1e-6)
                ff_means.append(flatfield.mean())

        progress_fn(0, f"Applying flatfield corrections ({method}) and writing multi-page stack tiles...")
        n_tiles = len(tile_list)
        for idx, t in enumerate(tile_list):
            in_p = raw_dir / t["filename"]
            out_p = corrected_tile_path(out_dir, raw_dir, t["filename"])
            if not in_p.exists() or out_p.exists():
                continue

            raw = tifffile.imread(str(in_p))
            corrected_channels = []
            for c in range(num_channels):
                chan_img = read_tile_frame(in_p, channel_idx=c, z_idx=0,
                                           channel_mode="reference", z_mode="slice").astype(np.float32)
                if method == "basicpy":
                    corr_img = chan_img / flatfields[c]
                else:
                    corr_img = chan_img / (flatfields[c] / ff_means[c])

                if raw.dtype == np.uint8:
                    corr_img = np.clip(corr_img, 0, 255).astype(np.uint8)
                else:
                    corr_img = np.clip(corr_img, 0, 65535).astype(np.uint16)
                corrected_channels.append(corr_img)

            if channel_axis == 0 or channel_axis is None:
                corrected_stack = np.stack(corrected_channels, axis=0)
                axes_meta = "CYX"
            else:
                corrected_stack = np.stack(corrected_channels, axis=2)
                axes_meta = "YXC"

            out_p.parent.mkdir(parents=True, exist_ok=True)
            tifffile.imwrite(
                str(out_p), corrected_stack,
                compression="deflate",
                metadata={"axes": axes_meta},
            )

            if idx % max(1, n_tiles // 20) == 0:
                progress_fn(int(20 + (idx / n_tiles) * 30), "")

    else:
        # Spatial rolling ball — stack
        progress_fn(0, "Applying spatial rolling-ball correction and writing multi-page stack tiles...")
        n_tiles = len(tile_list)
        for idx, t in enumerate(tile_list):
            in_p = raw_dir / t["filename"]
            out_p = corrected_tile_path(out_dir, raw_dir, t["filename"])
            if not in_p.exists() or out_p.exists():
                continue

            raw = tifffile.imread(str(in_p))
            corrected_channels = []
            for c in range(num_channels):
                chan_img = read_tile_frame(in_p, channel_idx=c, z_idx=0,
                                           channel_mode="reference", z_mode="slice").astype(np.float32)
                h, w = chan_img.shape
                ball_r = min(h, w) // 8
                bg = scipy.ndimage.grey_erosion(chan_img, size=(ball_r, ball_r))
                bg = scipy.ndimage.gaussian_filter(bg, sigma=ball_r // 2)
                bg_median = np.median(bg)
                corr_img = chan_img - bg + bg_median

                if raw.dtype == np.uint8:
                    corr_img = np.clip(corr_img, 0, 255).astype(np.uint8)
                else:
                    corr_img = np.clip(corr_img, 0, 65535).astype(np.uint16)
                corrected_channels.append(corr_img)

            if channel_axis == 0 or channel_axis is None:
                corrected_stack = np.stack(corrected_channels, axis=0)
                axes_meta = "CYX"
            else:
                corrected_stack = np.stack(corrected_channels, axis=2)
                axes_meta = "YXC"

            out_p.parent.mkdir(parents=True, exist_ok=True)
            tifffile.imwrite(
                str(out_p), corrected_stack,
                compression="deflate",
                metadata={"axes": axes_meta},
            )

            if idx % max(1, n_tiles // 20) == 0:
                progress_fn(int(20 + (idx / n_tiles) * 30), "")
