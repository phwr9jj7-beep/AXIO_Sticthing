"""
gui_runner.py
-------------
A CLI wrapper to run the AXIO microscopy stitching pipeline, specifically designed
to be invoked by the PySide6 desktop GUI. It processes any arbitrary Zeiss XML metadata
file (_info.xml or _meta.xml) and outputs progress indicators in a format that the
GUI can capture and display.

Supports Consensus-Channel Alignment (Reference, Average, Max Projection) and Z-Stack Stitching.

Usage:
    python scripts/gui_runner.py --xml "path/to/xml" --out-dir "path/to/output" --correction basicpy --algorithm phase --downsample 4
"""

import os
import sys
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict, Counter
import numpy as np
import tifffile
from scipy.optimize import least_squares
from skimage.registration import phase_cross_correlation

sys.path.append(str(Path(__file__).resolve().parent))
from lib_shared import stitch_canvas, save_tiff, save_preview_thumbnail, read_tile_frame, detect_tile_axes


# Constants
TILE_PX = 1020
MAX_PHASE_SHIFT = 25
LAMBDA_ANCHOR = 0.5

def report_status(status: str):
    """Print status updates for the GUI to parse."""
    print(f"[STATUS] {status}", flush=True)

def report_progress(percent: int):
    """Print progress percentage for the GUI to parse."""
    print(f"[PROGRESS] {percent}", flush=True)

def parse_info_xml(xml_path: Path):
    """Parses standard _info.xml to extract stages and files."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    images = root.findall("Image")
    if not images:
        return {}

    scenes = defaultdict(list)
    for img in images:
        fn = img.findtext("Filename")
        if not fn:
            continue
        fn = fn.replace("%20", " ")
        b = img.find("Bounds")
        if b is None:
            continue
        attrib = b.attrib
        s = int(attrib.get("StartS", 0))
        scenes[s].append({
            "filename": fn,
            "x": float(attrib["StartX"]),
            "y": float(attrib["StartY"]),
            "w": int(attrib["SizeX"]),
            "h": int(attrib["SizeY"])
        })
    return dict(scenes)

def parse_meta_xml(xml_path: Path):
    """Parses _meta.xml to extract dimensions and scales for meander grids."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    scale_m = None
    for d in root.findall('.//Scaling/Items/Distance'):
        if d.get('Id') == 'X':
            scale_m = float(d.findtext('Value'))
            break
            
    if scale_m is None:
        raise RuntimeError("Could not find pixel scaling factor in _meta.xml")
        
    scenes = {}
    for i, tr in enumerate(root.findall('.//TileRegion')):
        name = tr.get('Name')
        cols = int(tr.findtext('Columns'))
        rows = int(tr.findtext('Rows'))
        size_w, size_h = [float(v) for v in tr.findtext('ContourSize').split(',')]
        
        step_x_px = (size_w / cols) / (scale_m * 1e6)
        step_y_px = (size_h / rows) / (scale_m * 1e6)
        
        scenes[i] = {
            "name": name,
            "cols": cols,
            "rows": rows,
            "step_x_px": step_x_px,
            "step_y_px": step_y_px
        }
    return scale_m, scenes

def build_meander_scene_tiles(raw_dir: Path, xml_name_prefix: str, scene_idx: int, cols: int, rows: int,
                             step_x_px: float, step_y_px: float):
    """Reconstruct tile coordinates from meander scan grid."""
    s_id = scene_idx + 1
    tiles = []
    m = 1
    for row in range(rows):
        cols_range = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
        for col in cols_range:
            # Zeiss typically numbers tiles with m001, m002...
            fn = f"{xml_name_prefix}_s{s_id}m{m:03d}_ORG.tif"
            fpath = raw_dir / fn
            if fpath.exists():
                x_px = int(round(col * step_x_px))
                y_px = int(round(row * step_y_px))
                tiles.append({
                    "filename": fn,
                    "x": float(x_px),
                    "y": float(y_px),
                    "w": TILE_PX,
                    "h": TILE_PX
                })
            m += 1
    return tiles

def correct_scene_shading(raw_dir: Path, tile_list: list, out_dir: Path, method: str = "basicpy",
                          ref_channel_idx: int = 0, ref_tag: str = "", target_tags: list = None):
    """Run specified shading correction (basicpy, median, spatial) on all channels."""
    out_dir.mkdir(parents=True, exist_ok=True)
    
    if method == "none":
        return
        
    if method == "basicpy":
        os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
        try:
            from basicpy import BaSiC
        except ImportError:
            raise ImportError("BaSiCPy package is required but not installed.")
            
    import scipy.ndimage

    # Let's check if we are dealing with split-channel or multi-page stack
    if ref_tag:
        # Split channel flow
        all_tags = [ref_tag] + (target_tags if target_tags else [])
        report_status(f"Split-channel {method} correction for tags: {all_tags}")
        
        # Fit and correct each tag separately
        for tag_idx, tag in enumerate(all_tags):
            report_status(f"Processing shading correction ({method}) for tag '{tag}'...")
            
            # Map filenames for this tag
            tag_tiles = []
            for t in tile_list:
                fn_tag = t["filename"].replace(ref_tag, tag)
                tag_tiles.append(fn_tag)
                
            # Filter existing corrected files for this tag
            todo_tiles = [fn for fn in tag_tiles if not (out_dir / fn).exists()]
            if not todo_tiles:
                report_status(f"Corrected files for tag '{tag}' already exist. Skipping.")
                continue
                
            if method in ["basicpy", "median"]:
                # Load sample tiles for fitting
                np.random.seed(42)
                sample_size = min(len(tag_tiles), 300)
                sample_filenames = np.random.choice(tag_tiles, sample_size, replace=False)
                
                images_for_fit = []
                for idx, fn in enumerate(sample_filenames):
                    p = raw_dir / fn
                    if p.exists():
                        images_for_fit.append(np.squeeze(tifffile.imread(str(p))))
                    if idx % 10 == 0:
                        report_progress(int(5 + (idx / sample_size) * 15))
                        
                if not images_for_fit:
                    report_status(f"Warning: No source tiles found for tag '{tag}'. Skipping.")
                    continue
                    
                images_for_fit = np.array(images_for_fit)
                
                if method == "basicpy":
                    report_status(f"Fitting BaSiCPy flatfield for tag '{tag}'...")
                    basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
                    basic.fit(images_for_fit)
                    flatfield = basic.flatfield + 1e-6
                    ff_mean = 1.0
                else:  # median
                    report_status(f"Fitting Median flatfield for tag '{tag}'...")
                    flatfield = np.nanmedian(images_for_fit, axis=0)
                    flatfield = scipy.ndimage.gaussian_filter(flatfield, sigma=50)
                    flatfield = flatfield + 1e-6
                    ff_mean = flatfield.mean()
                
                # Apply correction
                report_status(f"Applying flatfield correction ({method}) for tag '{tag}'...")
                n_tiles = len(tag_tiles)
                for idx, fn in enumerate(tag_tiles):
                    in_p = raw_dir / fn
                    out_p = out_dir / fn
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
                    tifffile.imwrite(str(out_p), corrected, compression="deflate")
                    
                    if idx % max(1, n_tiles // 20) == 0:
                        progress_base = 20 + int((tag_idx / len(all_tags)) * 30)
                        progress_span = int(30 / len(all_tags))
                        report_progress(int(progress_base + (idx / n_tiles) * progress_span))
            else:
                # Spatial background subtraction
                report_status(f"Applying spatial rolling-ball correction for tag '{tag}'...")
                n_tiles = len(tag_tiles)
                for idx, fn in enumerate(tag_tiles):
                    in_p = raw_dir / fn
                    out_p = out_dir / fn
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
                    tifffile.imwrite(str(out_p), corrected, compression="deflate")
                    
                    if idx % max(1, n_tiles // 20) == 0:
                        progress_base = 20 + int((tag_idx / len(all_tags)) * 30)
                        progress_span = int(30 / len(all_tags))
                        report_progress(int(progress_base + (idx / n_tiles) * progress_span))
                        
    else:
        # Multi-page stack flow
        sample_path = raw_dir / tile_list[0]["filename"]
        if not sample_path.exists():
            raise FileNotFoundError(f"Source tile not found: {sample_path}")
            
        sample_info = detect_tile_axes(sample_path)
        num_channels = sample_info['num_channels']
        axes_str = sample_info['axes']
        channel_axis = axes_str.find('C') if 'C' in axes_str else None
        
        # Check if corrected files already exist
        if all((out_dir / t["filename"]).exists() for t in tile_list):
            report_status(f"Corrected stack files ({method}) already exist. Skipping.")
            return
            
        if method in ["basicpy", "median"]:
            report_status(f"Multi-page stack detected with {num_channels} channels. Fitting flatfield for each channel...")
            
            flatfields = []
            ff_means = []
            for c in range(num_channels):
                report_status(f"Fitting flatfield for channel {c}...")
                
                np.random.seed(42)
                sample_size = min(len(tile_list), 300)
                sample_tiles = np.random.choice(tile_list, sample_size, replace=False)
                
                images_for_fit = []
                for idx, t in enumerate(sample_tiles):
                    p = raw_dir / t["filename"]
                    if p.exists():
                        chan_img = read_tile_frame(p, channel_idx=c, z_idx=0, channel_mode='reference', z_mode='slice')
                        images_for_fit.append(chan_img)
                    if idx % 10 == 0:
                        report_progress(int(5 + (idx / sample_size) * 15))
                        
                if not images_for_fit:
                    raise FileNotFoundError(f"Could not load any tile frames for channel {c}")
                    
                images_for_fit = np.array(images_for_fit)
                if method == "basicpy":
                    basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
                    basic.fit(images_for_fit)
                    flatfields.append(basic.flatfield + 1e-6)
                    ff_means.append(1.0)
                else:  # median
                    flatfield = np.nanmedian(images_for_fit, axis=0)
                    flatfield = scipy.ndimage.gaussian_filter(flatfield, sigma=50)
                    flatfields.append(flatfield + 1e-6)
                    ff_means.append(flatfield.mean())
                
            report_status(f"Applying flatfield corrections ({method}) and writing multi-page stack tiles...")
            n_tiles = len(tile_list)
            for idx, t in enumerate(tile_list):
                in_p = raw_dir / t["filename"]
                out_p = out_dir / t["filename"]
                if not in_p.exists() or out_p.exists():
                    continue
                    
                raw = tifffile.imread(str(in_p))
                
                corrected_channels = []
                for c in range(num_channels):
                    chan_img = read_tile_frame(in_p, channel_idx=c, z_idx=0, channel_mode='reference', z_mode='slice').astype(np.float32)
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
                    axes_meta = 'CYX'
                else:
                    corrected_stack = np.stack(corrected_channels, axis=2)
                    axes_meta = 'YXC'
                    
                tifffile.imwrite(
                    str(out_p),
                    corrected_stack,
                    compression="deflate",
                    metadata={'axes': axes_meta}
                )
                
                if idx % max(1, n_tiles // 20) == 0:
                    report_progress(int(20 + (idx / n_tiles) * 30))
        else:
            # Spatial rolling ball correction for stacks
            report_status("Applying spatial rolling-ball correction and writing multi-page stack tiles...")
            n_tiles = len(tile_list)
            for idx, t in enumerate(tile_list):
                in_p = raw_dir / t["filename"]
                out_p = out_dir / t["filename"]
                if not in_p.exists() or out_p.exists():
                    continue
                    
                raw = tifffile.imread(str(in_p))
                
                corrected_channels = []
                for c in range(num_channels):
                    chan_img = read_tile_frame(in_p, channel_idx=c, z_idx=0, channel_mode='reference', z_mode='slice').astype(np.float32)
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
                    axes_meta = 'CYX'
                else:
                    corrected_stack = np.stack(corrected_channels, axis=2)
                    axes_meta = 'YXC'
                    
                tifffile.imwrite(
                    str(out_p),
                    corrected_stack,
                    compression="deflate",
                    metadata={'axes': axes_meta}
                )
                
                if idx % max(1, n_tiles // 20) == 0:
                    report_progress(int(20 + (idx / n_tiles) * 30))

def solve_optimal_positions(grid: dict, idx_map: dict, refined_shifts: dict, tiles: list):
    """Run global least-squares solvers using Tikhonov regulization."""
    n = len(tiles)
    init_pos = np.zeros((n, 2), dtype=np.float64)
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        init_pos[i, 0] = t["y"]
        init_pos[i, 1] = t["x"]
        
    if not refined_shifts:
        report_status("No shifts detected. Stitching falls back to coordinate alignment.")
        return {t["filename"]: (t["y"], t["x"]) for t in tiles}

    def residuals(pos_flat):
        pos = pos_flat.reshape(n, 2)
        res = []
        for (ka, kb), (dy_ref, dx_ref) in refined_shifts.items():
            ia, ib = idx_map[ka], idx_map[kb]
            res.append(pos[ib, 0] - pos[ia, 0] - dy_ref)
            res.append(pos[ib, 1] - pos[ia, 1] - dx_ref)
        drift = (pos - init_pos) * LAMBDA_ANCHOR
        res.extend(drift.flatten())
        return np.array(res)

    report_status("Executing Tikhonov-anchored least-squares optimization...")
    result = least_squares(residuals, init_pos.flatten(), method="lm", max_nfev=5000)
    opt = result.x.reshape(n, 2)
    
    positions = {}
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        positions[t["filename"]] = (opt[i, 0], opt[i, 1])
    return positions

def construct_grid(tiles: list):
    """Construct a discrete row/col layout from tile positions."""
    xs = sorted(list(set(t["x"] for t in tiles)))
    ys = sorted(list(set(t["y"] for t in tiles)))
    
    step_x = float(np.median(np.diff(xs))) if len(xs) > 1 else TILE_PX
    step_y = float(np.median(np.diff(ys))) if len(ys) > 1 else TILE_PX
    min_x, min_y = xs[0], ys[0]
    
    grid = {}
    idx_map = {}
    for i, t in enumerate(tiles):
        col = int(round((t["x"] - min_x) / step_x))
        row = int(round((t["y"] - min_y) / step_y))
        grid[(row, col)] = t
        idx_map[(row, col)] = i
    return grid, idx_map

def run_bounded_phase_corr(tiles: list, source_dir: Path, ref_channel_idx: int = 0,
                           alignment_mode: str = 'reference', z_mode: str = 'none', ref_z_slice: int = 0):
    """Perform bounded phase correlation on tile overlaps."""
    grid, idx_map = construct_grid(tiles)
    pairs = []
    for (row, col) in sorted(grid.keys()):
        if (row, col + 1) in grid:
            pairs.append(("horizontal", (row, col), (row, col + 1)))
        if (row + 1, col) in grid:
            pairs.append(("vertical", (row, col), (row + 1, col)))
            
    # Detect actual tile width and height from the first tile
    sample_info = detect_tile_axes(source_dir / tiles[0]["filename"])
    tile_h = sample_info['H']
    tile_w = sample_info['W']
            
    refined_shifts = {}
    n_pairs = len(pairs)
    report_status(f"Computing {n_pairs} pairwise phase-correlations...")
    
    # Determine z_read_mode and z_idx for registration
    if z_mode == 'none':
        z_read_mode = 'slice'
        reg_z_idx = 0
    elif z_mode == 'ref_slice_3d':
        z_read_mode = 'slice'
        reg_z_idx = ref_z_slice
    else:  # mip_align_3d or mip_output_only
        z_read_mode = 'mip'
        reg_z_idx = 0
    
    for idx, (direction, ka, kb) in enumerate(pairs):
        pa = source_dir / grid[ka]["filename"]
        pb = source_dir / grid[kb]["filename"]
        if not pa.exists() or not pb.exists():
            continue
            
        a_geo, b_geo = grid[ka], grid[kb]
        dx_nom = b_geo["x"] - a_geo["x"]
        dy_nom = b_geo["y"] - a_geo["y"]
        
        ov_x = max(0, int(tile_w - dx_nom)) if direction == "horizontal" else tile_w
        ov_y = max(0, int(tile_h - dy_nom)) if direction == "vertical" else tile_h
        
        if ov_x <= 0 or ov_y <= 0:
            continue
            
        img_a = read_tile_frame(pa, channel_idx=ref_channel_idx, z_idx=reg_z_idx,
                                channel_mode=alignment_mode, z_mode=z_read_mode).astype(np.float32)
        img_b = read_tile_frame(pb, channel_idx=ref_channel_idx, z_idx=reg_z_idx,
                                channel_mode=alignment_mode, z_mode=z_read_mode).astype(np.float32)
        
        # Extract overlap strips
        if direction == "horizontal":
            sa = img_a[:, -ov_x:]
            sb = img_b[:, :ov_x]
        else:
            sa = img_a[-ov_y:, :]
            sb = img_b[:ov_y, :]
            
        # Downsize overlap area for speed
        MAX_D = 800
        if direction == "horizontal" and sa.shape[0] > MAX_D:
            mid = sa.shape[0] // 2; half = MAX_D // 2
            sa = sa[mid-half:mid+half, :]; sb = sb[mid-half:mid+half, :]
        elif direction == "vertical" and sa.shape[1] > MAX_D:
            mid = sa.shape[1] // 2; half = MAX_D // 2
            sa = sa[:, mid-half:mid+half]; sb = sb[:, mid-half:mid+half]
            
        try:
            shift, _, _ = phase_cross_correlation(sa, sb, normalization="phase", upsample_factor=10)
            dy, dx = float(shift[0]), float(shift[1])
            if abs(dy) > MAX_PHASE_SHIFT or abs(dx) > MAX_PHASE_SHIFT:
                dy, dx = 0.0, 0.0
        except Exception:
            dy, dx = 0.0, 0.0
            
        refined_shifts[(ka, kb)] = (dy_nom + dy, dx_nom + dx)
        
        # Mapping phase progress to 50% to 80%
        if idx % max(1, n_pairs // 20) == 0:
            report_progress(int(50 + (idx / n_pairs) * 30))
            
    return solve_optimal_positions(grid, idx_map, refined_shifts, tiles)


def run_sift_alignment(tiles: list, source_dir: Path, ref_channel_idx: int = 0,
                       alignment_mode: str = 'reference', z_mode: str = 'none', ref_z_slice: int = 0):
    """Perform SIFT feature-based alignment on tile overlaps."""
    from lib_stitch_sift import compute_sift_shift
    
    grid, idx_map = construct_grid(tiles)
    pairs = []
    for (row, col) in sorted(grid.keys()):
        if (row, col + 1) in grid:
            pairs.append(("horizontal", (row, col), (row, col + 1)))
        if (row + 1, col) in grid:
            pairs.append(("vertical", (row, col), (row + 1, col)))
            
    sample_info = detect_tile_axes(source_dir / tiles[0]["filename"])
    tile_h = sample_info['H']
    tile_w = sample_info['W']
    max_shift_x = int(tile_w * 0.25)
    max_shift_y = int(tile_h * 0.25)
            
    refined_shifts = {}
    n_pairs = len(pairs)
    report_status(f"Computing {n_pairs} pairwise SIFT alignments...")
    
    # Determine z_read_mode and z_idx for registration
    if z_mode == 'none':
        z_read_mode = 'slice'
        reg_z_idx = 0
    elif z_mode == 'ref_slice_3d':
        z_read_mode = 'slice'
        reg_z_idx = ref_z_slice
    else:  # mip_align_3d or mip_output_only
        z_read_mode = 'mip'
        reg_z_idx = 0
        
    for idx, (direction, ka, kb) in enumerate(pairs):
        pa = source_dir / grid[ka]["filename"]
        pb = source_dir / grid[kb]["filename"]
        if not pa.exists() or not pb.exists():
            continue
            
        a_geo, b_geo = grid[ka], grid[kb]
        dx_nom = b_geo["x"] - a_geo["x"]
        dy_nom = b_geo["y"] - a_geo["y"]
        
        ov_x = max(0, int(tile_w - dx_nom)) if direction == "horizontal" else tile_w
        ov_y = max(0, int(tile_h - dy_nom)) if direction == "vertical" else tile_h
        
        overlap_px = ov_x if direction == "horizontal" else ov_y
        if overlap_px <= 0:
            continue
            
        img_a = read_tile_frame(pa, channel_idx=ref_channel_idx, z_idx=reg_z_idx,
                                channel_mode=alignment_mode, z_mode=z_read_mode).astype(np.float32)
        img_b = read_tile_frame(pb, channel_idx=ref_channel_idx, z_idx=reg_z_idx,
                                channel_mode=alignment_mode, z_mode=z_read_mode).astype(np.float32)
        
        try:
            dy, dx, inliers = compute_sift_shift(img_a, img_b, direction, overlap_px)
            
            # SIFT computes shift of B relative to A, so absolute B is A + shift
            # If inliers < 8, or shift is wildly off, fall back to nominal
            if inliers < 8 or abs(dy - dy_nom) > max_shift_y or abs(dx - dx_nom) > max_shift_x:
                dy, dx = dy_nom, dx_nom
        except Exception:
            dy, dx = dy_nom, dx_nom
            
        refined_shifts[(ka, kb)] = (dy, dx)
        
        # Mapping SIFT progress to 50% to 80%
        if idx % max(1, n_pairs // 20) == 0:
            report_progress(int(50 + (idx / n_pairs) * 30))
            
    return solve_optimal_positions(grid, idx_map, refined_shifts, tiles)


def main():
    parser = argparse.ArgumentParser(description="AXIO Stitching GUI Custom Runner")
    parser.add_argument("--xml", required=True, help="Path to Zeiss XML file (_info.xml or _meta.xml)")
    parser.add_argument("--out-dir", required=True, help="Directory to save output files")
    parser.add_argument("--correction", choices=["basicpy", "median", "spatial", "none"], default="basicpy", help="Illumination correction method")
    parser.add_argument("--algorithm", choices=["phase", "coordinate", "sift"], default="phase", help="Stitching algorithm")
    parser.add_argument("--scene", type=int, default=None, help="Restrict to single scene (0-indexed)")
    parser.add_argument("--ref-channel", type=int, default=0, help="Reference channel index for multi-page TIFF stacks")
    parser.add_argument("--ref-tag", type=str, default="", help="Reference channel filename tag for split channel TIFFs (e.g. _c1_)")
    parser.add_argument("--target-tags", type=str, default="", help="Target channel filename tags for split channel TIFFs, comma separated (e.g. _c2_,_c3_)")
    
    # Phase 4 new options
    parser.add_argument("--alignment-mode", choices=["reference", "average", "max_projection"], default="reference", help="Channel fusion method for alignment")
    parser.add_argument("--z-mode", choices=["none", "mip_align_3d", "ref_slice_3d", "mip_output_only"], default="none", help="Z-stack handling mode")
    parser.add_argument("--ref-z-slice", type=int, default=0, help="Reference Z-slice index (for ref_slice_3d mode)")
    args = parser.parse_args()

    xml_path = Path(args.xml).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    
    target_tags_list = [t.strip() for t in args.target_tags.split(",") if t.strip()] if args.target_tags else []
    
    # Enforce full-resolution stitching globally (downsample=1, no suffix)
    downsample = 1
    suffix = ""
    
    report_status(f"Starting stitching runner...")
    report_status(f"XML Metadata Path: {xml_path}")
    report_status(f"Output Directory  : {out_dir}")
    report_status(f"Correction        : {args.correction}")
    report_status(f"Algorithm         : {args.algorithm}")
    report_status(f"Downsample        : 1x (Full Resolution Enforced)")
    report_status(f"Reference Channel : {args.ref_channel}")
    report_status(f"Reference Tag     : {args.ref_tag}")
    report_status(f"Target Tags       : {target_tags_list}")
    report_status(f"Alignment Mode    : {args.alignment_mode}")
    report_status(f"Z-Stack Mode      : {args.z_mode}")
    report_status(f"Reference Z Slice : {args.ref_z_slice}")
    report_progress(1)
    
    if not xml_path.exists():
        print(f"[ERROR] Specified XML file does not exist: {xml_path}")
        sys.exit(1)
        
    raw_dir = xml_path.parent
    report_status(f"Raw data directory parsed from XML: {raw_dir}")
    
    # Check what kind of XML we have
    is_meta = "_meta.xml" in xml_path.name.lower() or xml_path.name.endswith("meta.xml")
    
    scenes = {}
    
    if not is_meta:
        report_status("Attempting to parse XML as standard _info.xml layout...")
        try:
            scenes = parse_info_xml(xml_path)
        except Exception as e:
            report_status(f"Info XML parsing failed: {e}. Falling back to _meta.xml format.")
            is_meta = True
            
    if is_meta or not scenes:
        # Fall back or run _meta.xml
        meta_path = xml_path
        if not is_meta:
            # If we started with info.xml and failed, look for a meta.xml file
            meta_path = xml_path.parent / xml_path.name.replace("_info.xml", "_meta.xml")
            
        if not meta_path.exists():
            print(f"[ERROR] Could not find grid coordinates. Standard info.xml failed and meta.xml does not exist at: {meta_path}")
            sys.exit(1)
            
        report_status(f"Parsing meander grid layout from meta XML: {meta_path.name}")
        scale_m, meta_scenes = parse_meta_xml(meta_path)
        xml_prefix = meta_path.name.replace("_meta.xml", "")
        
        for scene_idx, scene_info in meta_scenes.items():
            cols, rows = scene_info["cols"], scene_info["rows"]
            step_x_px, step_y_px = scene_info["step_x_px"], scene_info["step_y_px"]
            tiles = build_meander_scene_tiles(raw_dir, xml_prefix, scene_idx, cols, rows, step_x_px, step_y_px)
            if tiles:
                scenes[scene_idx] = tiles
                
    if not scenes:
        print("[ERROR] No tiles or scene grid geometry could be extracted.")
        sys.exit(1)
        
    report_status(f"Extracted {len(scenes)} scenes to stitch.")
    report_progress(5)
    
    target_scenes = [args.scene] if args.scene is not None else sorted(scenes.keys())
    
    for scene_idx in target_scenes:
        if scene_idx not in scenes:
            report_status(f"Scene {scene_idx} does not exist. Skipping.")
            continue
            
        report_status(f"--- Processing Scene {scene_idx} ({len(scenes[scene_idx])} tiles) ---")
        scene_tiles = scenes[scene_idx]
        
        # Filter raw tiles matching ref-tag if split channel is used
        if args.ref_tag:
            ref_tiles = [t for t in scene_tiles if args.ref_tag in t["filename"]]
            if not ref_tiles:
                report_status(f"Warning: No tiles matched reference tag '{args.ref_tag}'. Using all tiles.")
                ref_tiles = scene_tiles
        else:
            ref_tiles = scene_tiles
            
        # Step 1: Flatfield correction
        correction_dir = raw_dir
        if args.correction != "none":
            correction_dir = out_dir / "intermediate" / f"scene{scene_idx}" / f"{args.correction}_corrected"
            correct_scene_shading(
                raw_dir, 
                scene_tiles, 
                correction_dir, 
                method=args.correction,
                ref_channel_idx=args.ref_channel, 
                ref_tag=args.ref_tag, 
                target_tags=target_tags_list
            )
        else:
            report_status("Shading correction skipped. Stitching on raw tiles.")
            report_progress(50)
            
        # Step 2: Offsets and Alignment (strictly on the reference channel/tiles)
        if args.algorithm == "phase":
            positions = run_bounded_phase_corr(
                ref_tiles, 
                correction_dir, 
                ref_channel_idx=args.ref_channel,
                alignment_mode=args.alignment_mode,
                z_mode=args.z_mode,
                ref_z_slice=args.ref_z_slice
            )
        elif args.algorithm == "sift":
            positions = run_sift_alignment(
                ref_tiles, 
                correction_dir, 
                ref_channel_idx=args.ref_channel,
                alignment_mode=args.alignment_mode,
                z_mode=args.z_mode,
                ref_z_slice=args.ref_z_slice
            )
        else:
            report_status("Using coordinates directly from Zeiss stage limits.")
            positions = {t["filename"]: (t["y"], t["x"]) for t in ref_tiles}
            report_progress(80)
            
        # Step 3: Stitching final canvas(es)
        report_status("Blending tiles and building output canvas...")
        
        # Detect dimensions from the first reference tile using metadata-aware detect_tile_axes
        sample_info = detect_tile_axes(correction_dir / ref_tiles[0]["filename"])
        num_channels = sample_info['num_channels']
        tile_h = sample_info['H']
        tile_w = sample_info['W']
        
        # Probe Z-slices from filenames or metadata
        import re
        z_pattern = re.compile(r'_z(\d+)_')
        xml_z_indices = sorted(set(
            int(m.group(1)) for t in scene_tiles
            for m in [z_pattern.search(t["filename"])] if m
        ))
        
        if not xml_z_indices:
            # Check raw_dir files
            first_fn = scene_tiles[0]["filename"]
            prefix = first_fn.split('_ORG.tif')[0]
            try:
                raw_files = os.listdir(raw_dir)
                for f in raw_files:
                    if f.startswith(prefix.split('_c')[0]):
                        m = z_pattern.search(f)
                        if m:
                            xml_z_indices.append(int(m.group(1)))
                xml_z_indices = sorted(list(set(xml_z_indices)))
            except Exception:
                pass
                
        num_z_slices = len(xml_z_indices) if xml_z_indices else sample_info['num_z']
        
        if args.z_mode == 'none':
            z_slices_to_stitch = [0]
            num_z = 1
            stitching_z_mode = 'slice'
        elif args.z_mode == 'mip_output_only':
            z_slices_to_stitch = [0]
            num_z = 1
            stitching_z_mode = 'mip'
        else:
            num_z = num_z_slices
            z_slices_to_stitch = list(range(num_z))
            stitching_z_mode = 'slice'
            
        if args.ref_tag:
            # Split-channel stitching flow
            all_tags = [args.ref_tag] + target_tags_list
            for tag in all_tags:
                report_status(f"Stitching split channel for tag '{tag}'...")
                
                # Map tiles and positions for this tag
                tag_tiles = []
                for t in ref_tiles:
                    t_copy = t.copy()
                    t_copy["filename"] = t["filename"].replace(args.ref_tag, tag)
                    tag_tiles.append(t_copy)
                    
                tag_positions = {}
                for fn, pos in positions.items():
                    fn_tag = fn.replace(args.ref_tag, tag)
                    tag_positions[fn_tag] = pos
                    
                if num_z > 1:
                    z_canvases = []
                    for z in z_slices_to_stitch:
                        report_status(f"  Stitching Z-slice {z}/{num_z}...")
                        canvas_z = stitch_canvas(
                            tag_positions, correction_dir, tag_tiles, tile_h, tile_w, 
                            downsample=downsample, channel_idx=0, z_idx=z,
                            channel_mode='reference', z_mode=stitching_z_mode,
                            ref_tag=args.ref_tag, channel_tag=tag
                        )
                        if canvas_z is not None:
                            z_canvases.append(canvas_z)
                    
                    if z_canvases:
                        final_volume = np.stack(z_canvases, axis=0)
                        tag_clean = tag.strip("_")
                        out_fn = f"stitched_scene{scene_idx}_{tag_clean}_{args.algorithm}{suffix}.tif"
                        out_path = out_dir / out_fn
                        report_status(f"Saving stitched 3D volume: {out_fn}")
                        save_tiff(final_volume, out_path, axes_hint='ZYX')
                        save_preview_thumbnail(final_volume, out_path, num_channels=1, num_z=num_z, stitching_z_mode=stitching_z_mode)
                    else:
                        print(f"[ERROR] Stitching returned an empty canvas list for tag {tag}")
                        sys.exit(1)
                else:
                    canvas = stitch_canvas(
                        tag_positions, correction_dir, tag_tiles, tile_h, tile_w, 
                        downsample=downsample, channel_idx=0, z_idx=0,
                        channel_mode='reference', z_mode=stitching_z_mode,
                        ref_tag=args.ref_tag, channel_tag=tag
                    )
                    
                    if canvas is not None:
                        tag_clean = tag.strip("_")
                        out_fn = f"stitched_scene{scene_idx}_{tag_clean}_{args.algorithm}{suffix}.tif"
                        out_path = out_dir / out_fn
                        report_status(f"Saving stitched image: {out_fn}")
                        save_tiff(canvas, out_path, axes_hint='YX')
                        save_preview_thumbnail(canvas, out_path, num_channels=1, num_z=1, stitching_z_mode=stitching_z_mode)
                    else:
                        print(f"[ERROR] Stitching returned an empty canvas for tag {tag}")
                        sys.exit(1)
            report_status(f"[SUCCESS] All split channels stitched for scene {scene_idx}")
            
        else:
            # Multi-page stack stitching flow
            if num_z > 1:
                z_canvases = []
                for z in z_slices_to_stitch:
                    report_status(f"  Stitching Z-slice {z}/{num_z}...")
                    ch_canvases = []
                    for c in range(num_channels):
                        canvas_zc = stitch_canvas(
                            positions, correction_dir, ref_tiles, tile_h, tile_w, 
                            downsample=downsample, channel_idx=c, z_idx=z,
                            channel_mode='reference', z_mode=stitching_z_mode
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
                    out_fn = f"stitched_scene{scene_idx}_{args.algorithm}{suffix}.tif"
                    out_path = out_dir / out_fn
                    report_status(f"Saving stitched 3D/4D multi-channel volume to disk: {out_fn}")
                    save_tiff(final_volume, out_path, axes_hint='ZCYX' if num_channels > 1 else 'ZYX')
                    save_preview_thumbnail(final_volume, out_path, num_channels=num_channels, num_z=num_z, stitching_z_mode=stitching_z_mode)
                    report_status(f"[SUCCESS] Stitched scene {scene_idx} saved at: {out_path}")
                else:
                    print(f"[ERROR] Stitching returned empty stack for scene {scene_idx}")
                    sys.exit(1)
            else:
                canvases = []
                for c in range(num_channels):
                    report_status(f"Stitching channel {c} canvas...")
                    canvas_c = stitch_canvas(
                        positions, correction_dir, ref_tiles, tile_h, tile_w, 
                        downsample=downsample, channel_idx=c, z_idx=0,
                        channel_mode='reference', z_mode=stitching_z_mode
                    )
                    if canvas_c is not None:
                        canvases.append(canvas_c)
                        
                if canvases:
                    stacked_canvas = np.stack(canvases, axis=0) if len(canvases) > 1 else canvases[0]
                    out_fn = f"stitched_scene{scene_idx}_{args.algorithm}{suffix}.tif"
                    out_path = out_dir / out_fn
                    report_status(f"Saving stitched multi-channel image to disk: {out_fn}")
                    save_tiff(stacked_canvas, out_path, axes_hint='CYX' if len(canvases) > 1 else 'YX')
                    save_preview_thumbnail(stacked_canvas, out_path, num_channels=len(canvases), num_z=1, stitching_z_mode=stitching_z_mode)
                    report_status(f"[SUCCESS] Stitched scene {scene_idx} saved at: {out_path}")
                else:
                    print(f"[ERROR] Stitching returned an empty canvas for scene {scene_idx}")
                    sys.exit(1)
            
    report_progress(100)
    report_status("✓ Stitching operation completed successfully!")

if __name__ == "__main__":
    main()
