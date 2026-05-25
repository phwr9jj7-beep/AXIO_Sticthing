"""
lib_shared.py
-------------
Shared utility functions for the AXIO stitching pipeline. Includes Zeiss XML metadata
parsing, linear feathered blending weight mask creation, canvas assembly, and ImageJ-compatible
compressed 16-bit TIFF serialization.
"""

import xml.etree.ElementTree as ET
import numpy as np
import tifffile
from pathlib import Path
from collections import defaultdict
from tqdm import tqdm


def parse_xml(xml_path: Path):
    """
    Parses Zeiss XML metadata to extract tile boundaries.
    Returns: dict {scene_id: [{"filename": str, "x": int, "y": int, "w": int, "h": int}, ...]}
    """
    if not xml_path.exists():
        raise FileNotFoundError(f"Missing XML file: {xml_path}")
        
    tree = ET.parse(xml_path)
    root = tree.getroot()
    scenes = defaultdict(list)
    for img in root.findall("Image"):
        fn = img.findtext("Filename")
        if not fn: continue
        b = img.find("Bounds")
        if b is None: continue
        attrib = b.attrib
        s = int(attrib.get("StartS", 0))
        scenes[s].append({
            "filename": fn,
            "x": int(attrib["StartX"]),
            "y": int(attrib["StartY"]),
            "w": int(attrib["SizeX"]),
            "h": int(attrib["SizeY"]),
            "path": None  # Will be populated by the correction modules
        })
    return dict(scenes)


def detect_tile_axes(tif_path):
    """Detect tile TIFF dimensions using tifffile metadata.
    Returns: dict with keys 'axes', 'shape', 'num_channels', 'num_z', 'H', 'W'
    """
    try:
        with tifffile.TiffFile(str(tif_path)) as tif:
            s = tif.series[0]
            axes = s.axes.upper()  # e.g. 'YX', 'CYX', 'ZYX', 'ZCYX'
            shape = s.shape
            
            # Map axis letters to dimension sizes
            dim_dict = {axes[i]: shape[i] for i in range(len(axes))}
            
            num_channels = dim_dict.get('C', 1)
            num_z = dim_dict.get('Z', 1)
            H = dim_dict.get('Y', shape[-2] if len(shape) >= 2 else shape[0])
            W = dim_dict.get('X', shape[-1] if len(shape) >= 1 else shape[0])
            
            # If axes string is empty or unrecognized, or shape mismatch, fallback
            if not axes or len(axes) != len(shape):
                raise ValueError("Unrecognized axes format")
                
            return {
                'axes': axes,
                'shape': shape,
                'num_channels': num_channels,
                'num_z': num_z,
                'H': H,
                'W': W
            }
    except Exception:
        # Fallback to legacy shape heuristic
        try:
            img = tifffile.imread(str(tif_path))
            shape = img.shape
            ndim = img.ndim
            if ndim == 2:
                return {'axes': 'YX', 'shape': shape, 'num_channels': 1, 'num_z': 1, 'H': shape[0], 'W': shape[1]}
            elif ndim == 3:
                # Shape (3, 1020, 1020) vs (1020, 1020, 3)
                if shape[0] < shape[2]:
                    # Assume CYX (or ZYX - we default to CYX for backward compatibility)
                    return {'axes': 'CYX', 'shape': shape, 'num_channels': shape[0], 'num_z': 1, 'H': shape[1], 'W': shape[2]}
                else:
                    # Assume YXC
                    return {'axes': 'YXC', 'shape': shape, 'num_channels': shape[2], 'num_z': 1, 'H': shape[0], 'W': shape[1]}
        except Exception:
            pass
        # Final emergency fallback
        return {'axes': 'YX', 'shape': (1020, 1020), 'num_channels': 1, 'num_z': 1, 'H': 1020, 'W': 1020}


def read_tile_frame(tif_path, channel_idx=0, z_idx=0, channel_mode='reference', z_mode='slice'):
    """Read a 2D frame from a multi-dimensional TIFF tile.
    
    channel_mode: 'reference' (single channel), 'average' (mean across C), 'max_projection' (MIP across C)
    z_mode:       'slice' (single Z-plane), 'mip' (MIP across Z)
    Returns: 2D np.ndarray (H, W)
    """
    info = detect_tile_axes(tif_path)
    axes = info['axes']
    shape = info['shape']
    
    # Read the full image
    img = tifffile.imread(str(tif_path))
    
    # Map each dimension to a standard index in the loaded array
    axis_to_idx = {axes[i]: i for i in range(len(axes))}
    
    # If the axes string is YX or fallback:
    if len(axes) == 2 and 'Y' in axis_to_idx and 'X' in axis_to_idx:
        return np.squeeze(img)
        
    # Handle Z axis projection/slicing
    if 'Z' in axis_to_idx:
        z_pos = axis_to_idx['Z']
        if z_mode == 'mip':
            img = np.max(img, axis=z_pos)
            new_axes = axes[:z_pos] + axes[z_pos+1:]
            axes = new_axes
            axis_to_idx = {axes[i]: i for i in range(len(axes))}
        else:  # slice mode
            z_len = shape[z_pos]
            z_idx_clamped = max(0, min(z_idx, z_len - 1))
            slicer = [slice(None)] * img.ndim
            slicer[z_pos] = z_idx_clamped
            img = img[tuple(slicer)]
            new_axes = axes[:z_pos] + axes[z_pos+1:]
            axes = new_axes
            axis_to_idx = {axes[i]: i for i in range(len(axes))}
            
    # Handle C axis projection/slicing
    if 'C' in axis_to_idx:
        c_pos = axis_to_idx['C']
        if channel_mode == 'max_projection':
            img = np.max(img, axis=c_pos)
            new_axes = axes[:c_pos] + axes[c_pos+1:]
            axes = new_axes
            axis_to_idx = {axes[i]: i for i in range(len(axes))}
        elif channel_mode == 'average':
            img = np.mean(img, axis=c_pos)
            new_axes = axes[:c_pos] + axes[c_pos+1:]
            axes = new_axes
            axis_to_idx = {axes[i]: i for i in range(len(axes))}
        else:  # reference mode
            c_len = img.shape[c_pos]
            c_idx_clamped = max(0, min(channel_idx, c_len - 1))
            slicer = [slice(None)] * img.ndim
            slicer[c_pos] = c_idx_clamped
            img = img[tuple(slicer)]
            new_axes = axes[:c_pos] + axes[c_pos+1:]
            axes = new_axes
            axis_to_idx = {axes[i]: i for i in range(len(axes))}
            
    img = np.squeeze(img)
    return img


def read_tile_channel(tif_path, channel_idx=0):
    """Read a specific channel page from a TIFF tile, handles stacks and 2D files."""
    return read_tile_frame(tif_path, channel_idx=channel_idx, z_idx=0, channel_mode='reference', z_mode='slice')


def make_feather_weight(tile_h: int, tile_w: int, border_frac: float = 0.12):
    """Create a float32 weight mask with linear feathering at borders."""
    border_y = max(1, int(tile_h * border_frac))
    border_x = max(1, int(tile_w * border_frac))
    wy = np.ones(tile_h, dtype=np.float32)
    wx = np.ones(tile_w, dtype=np.float32)
    for i in range(border_y):
        v = (i + 1) / (border_y + 1)
        wy[i] = v
        wy[tile_h - 1 - i] = v
    for j in range(border_x):
        v = (j + 1) / (border_x + 1)
        wx[j] = v
        wx[tile_w - 1 - j] = v
    return np.outer(wy, wx)


def resolve_tile_filename(base_fn, channel_tag=None, z_idx=None, ref_tag=None):
    """
    Given a tile filename (e.g., from XML or reference), resolve it for the target channel/Z index.
    """
    import re
    fn = base_fn
    
    # Replace reference tag with target channel tag if provided
    if ref_tag and channel_tag:
        fn = fn.replace(ref_tag, channel_tag)
        
    # Check for Z-stack pattern _z\d+_
    z_pattern = re.compile(r'_z(\d+)_')
    m = z_pattern.search(fn)
    if m:
        digits_len = len(m.group(1))
        z_str = f"_z{z_idx:0{digits_len}d}_"
        fn = z_pattern.sub(z_str, fn)
    else:
        # If no Z pattern, but z_idx is specified and non-zero, append _z{idx} before suffix
        if z_idx is not None and z_idx > 0:
            if '_ORG.tif' in fn:
                fn = fn.replace('_ORG.tif', f'_z{z_idx:02d}_ORG.tif')
            else:
                parts = fn.rsplit('.', 1)
                if len(parts) == 2:
                    fn = f"{parts[0]}_z{z_idx:02d}.{parts[1]}"
    return fn


def stitch_canvas(positions: dict, source_dir: Path, tile_list: list, tile_h: int, tile_w: int, downsample: int, 
                  channel_idx: int = 0, z_idx: int = 0, read_func=None, channel_mode: str = 'reference', z_mode: str = 'slice',
                  ref_tag: str = "", channel_tag: str = ""):
    """
    Assembles a unified 16-bit canvas using global coordinates and feathered blending.
    positions: dict mapping reference `filename` -> (abs_y, abs_x)
    source_dir: directory where corrected tiles are stored
    tile_list: list of tile dictionaries for this scene
    """
    print(f"      Assembling canvas with {len(positions)} tiles at {downsample}x downsample...")
    
    # Calculate bounds
    abs_ys = [y for y, x in positions.values()]
    abs_xs = [x for y, x in positions.values()]
    y_min, x_min = min(abs_ys), min(abs_xs)
    y_max, x_max = max(abs_ys), max(abs_xs)
    
    canvas_h_full = int((y_max - y_min) + tile_h)
    canvas_w_full = int((x_max - x_min) + tile_w)
    
    canvas_h = canvas_h_full // downsample
    canvas_w = canvas_w_full // downsample
    tile_h_ds = tile_h // downsample
    tile_w_ds = tile_w // downsample
    
    accumulator = np.zeros((canvas_h, canvas_w), dtype=np.float32)
    weight_map  = np.zeros((canvas_h, canvas_w), dtype=np.float32)
    
    tile_weight = make_feather_weight(tile_h_ds, tile_w_ds)
    
    files_processed = 0
    for t in tqdm(tile_list, desc="      Blending", leave=False):
        ref_fn = t["filename"]
        if ref_fn not in positions:
            continue
            
        fn = resolve_tile_filename(ref_fn, channel_tag=channel_tag, z_idx=z_idx, ref_tag=ref_tag)
        tif_path = source_dir / fn
        if not tif_path.exists():
            continue
            
        if read_func is not None:
            tile_img = read_func(tif_path, channel_idx=channel_idx, z_idx=z_idx, channel_mode=channel_mode, z_mode=z_mode).astype(np.float32)
        else:
            tile_img = read_tile_frame(tif_path, channel_idx=channel_idx, z_idx=z_idx, channel_mode=channel_mode, z_mode=z_mode).astype(np.float32)
                
        if downsample > 1:
            from skimage.transform import downscale_local_mean
            tile_img = downscale_local_mean(tile_img, (downsample, downsample)).astype(np.float32)
            tile_img = tile_img[:tile_h_ds, :tile_w_ds]
            
        abs_y, abs_x = positions[ref_fn]
        cy = int((abs_y - y_min) / downsample)
        cx = int((abs_x - x_min) / downsample)
        th, tw = tile_img.shape
        
        cy2 = min(cy + th, canvas_h)
        cx2 = min(cx + tw, canvas_w)
        tile_img = tile_img[:cy2 - cy, :cx2 - cx]
        w = tile_weight[:cy2 - cy, :cx2 - cx]
        
        accumulator[cy:cy2, cx:cx2] += tile_img * w
        weight_map[cy:cy2, cx:cx2]  += w
        files_processed += 1
        
    if files_processed == 0:
        return None
        
    valid = weight_map > 0
    canvas = np.zeros_like(accumulator)
    canvas[valid] = accumulator[valid] / weight_map[valid]
    canvas = np.clip(canvas, 0, 65535).astype(np.uint16)
    
    return canvas


def save_tiff(canvas: np.ndarray, out_path: Path, axes_hint=None):
    """Save as an ImageJ-compatible 16-bit uncompressed TIFF."""
    if axes_hint is not None:
        axes_meta = axes_hint
    else:
        if canvas.ndim == 4:
            axes_meta = 'ZCYX'
        elif canvas.ndim == 3:
            if canvas.shape[0] <= 5:
                axes_meta = 'CYX'
            else:
                axes_meta = 'ZYX'
        else:
            axes_meta = 'YX'
        
    tifffile.imwrite(
        str(out_path), 
        canvas, 
        photometric="minisblack",
        metadata={'axes': axes_meta}
    )
    print(f"      Saved uncompressed {axes_meta} [{canvas.shape}] -> {out_path.name}")


def save_preview_thumbnail(canvas: np.ndarray, out_path: Path, num_channels=1, num_z=1, stitching_z_mode='slice'):
    """Generates a lightweight 8-bit visual preview PNG from a 3D/4D canvas and saves it."""
    try:
        from PIL import Image
        
        # Extract a 2D representative frame
        img_2d = canvas
        if canvas.ndim == 4:
            # (Z, C, Y, X) -> select middle Z, channel 0
            z_mid = canvas.shape[0] // 2
            img_2d = canvas[z_mid, 0]
        elif canvas.ndim == 3:
            # (Z, Y, X) or (C, Y, X)
            if num_channels > 1: # (C, Y, X)
                img_2d = canvas[0]
            else: # (Z, Y, X)
                z_mid = canvas.shape[0] // 2
                img_2d = canvas[z_mid]
                
        img_2d = np.squeeze(img_2d)
        if img_2d.ndim > 2:
            # Fallback
            img_2d = img_2d[0]
            
        # Downscale to max 1024px width/height for instant preview loading
        h, w = img_2d.shape
        max_dim = 1024
        if h > max_dim or w > max_dim:
            # Calculate downscale factor
            ds_factor = max(1, int(round(max(h, w) / max_dim)))
            if ds_factor > 1:
                # Fast downsampling by striding
                img_2d = img_2d[::ds_factor, ::ds_factor]
                
        # Normalize to 8-bit
        img_min = img_2d.min()
        img_max = img_2d.max()
        if img_max > img_min:
            img_8 = ((img_2d - img_min) / (img_max - img_min) * 255).astype(np.uint8)
        else:
            img_8 = img_2d.astype(np.uint8)
            
        # Convert grayscale to RGB format to satisfy standard display requirements (visually grayscale)
        img_rgb = np.stack([img_8, img_8, img_8], axis=-1)
        
        pil_img = Image.fromarray(img_rgb)
        
        # Save as PNG
        preview_path = out_path.with_name(out_path.stem + "_preview.png")
        pil_img.save(str(preview_path), "PNG")
        print(f"      Saved preview thumbnail -> {preview_path.name}")
    except Exception as e:
        print(f"      Warning: Failed to generate preview thumbnail: {e}")
