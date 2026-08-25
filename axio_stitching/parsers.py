"""
parsers.py
----------
Zeiss microscopy XML metadata parsing.

Extracted verbatim from gui_runner.py (parse_info_xml, parse_meta_xml,
build_meander_scene_tiles) plus a unified facade (parse_zeiss_xml) and
the legacy parse_xml from lib_shared.py.

All logic is preserved exactly as in the source — no algorithmic changes.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from .models import TileInfo, SceneInfo

# Default tile pixel size (matched to gui_runner.py constant)
_TILE_PX = 1020


# ---------------------------------------------------------------------------
# Primary: info.xml parser (direct stage coordinates)
# ---------------------------------------------------------------------------

def parse_info_xml(xml_path: Path) -> dict[int, list[dict]]:
    """
    Parse a standard Zeiss _info.xml to extract stage coordinates per scene.

    Returns a dict mapping scene_id -> list of raw tile dicts:
        {"filename": str, "x": float, "y": float, "w": int, "h": int}

    Preserved verbatim from gui_runner.py lines 43-69.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    images = root.findall("Image")
    if not images:
        return {}

    scenes: dict[int, list[dict]] = defaultdict(list)
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
            "h": int(attrib["SizeY"]),
        })
    return dict(scenes)


# ---------------------------------------------------------------------------
# Secondary: meta.xml parser (meander grid)
# ---------------------------------------------------------------------------

def parse_meta_xml(xml_path: Path) -> tuple[float | None, dict[int, dict]]:
    """
    Parse a Zeiss _meta.xml to extract pixel scale and meander grid geometry.

    Returns (scale_m, scenes) where:
        scale_m  – metres per pixel (or None if not found)
        scenes   – dict mapping scene_id -> {"name", "cols", "rows", "step_x_px", "step_y_px"}

    Preserved verbatim from gui_runner.py lines 71-101.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    scale_m: float | None = None

    for d in root.findall(".//Scaling/Items/Distance"):
        if d.get("Id") == "X":
            scale_m = float(d.findtext("Value"))  # type: ignore[arg-type]
            break

    if scale_m is None:
        raise RuntimeError("Could not find pixel scaling factor in _meta.xml")

    scenes: dict[int, dict] = {}
    for i, tr in enumerate(root.findall(".//TileRegion")):
        name = tr.get("Name", f"Scene{i}")
        cols = int(tr.findtext("Columns"))  # type: ignore[arg-type]
        rows = int(tr.findtext("Rows"))  # type: ignore[arg-type]
        size_w, size_h = [float(v) for v in tr.findtext("ContourSize").split(",")]  # type: ignore[union-attr]

        step_x_px = (size_w / cols) / (scale_m * 1e6)
        step_y_px = (size_h / rows) / (scale_m * 1e6)

        scenes[i] = {
            "name": name,
            "cols": cols,
            "rows": rows,
            "step_x_px": step_x_px,
            "step_y_px": step_y_px,
        }
    return scale_m, scenes


# ---------------------------------------------------------------------------
# Meander grid tile reconstruction
# ---------------------------------------------------------------------------

def build_meander_scene_tiles(
    raw_dir: Path,
    xml_name_prefix: str,
    scene_idx: int,
    cols: int,
    rows: int,
    step_x_px: float,
    step_y_px: float,
) -> list[dict]:
    """
    Reconstruct tile coordinate list from a meander scan grid pattern.

    Preserved verbatim from gui_runner.py lines 103-139.
    """
    s_id = scene_idx + 1
    m_pattern = re.compile(rf"_s{s_id}(?:[^0-9].*?)?m(\d+)", re.IGNORECASE)
    scene_files: dict[int, list[str]] = {}

    for f in raw_dir.glob("*.tif"):
        match = m_pattern.search(f.name)
        if match:
            m_idx = int(match.group(1))
            scene_files.setdefault(m_idx, []).append(f.name)

    tiles: list[dict] = []
    m = 1
    for row in range(rows):
        cols_range = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
        for col in cols_range:
            if m in scene_files:
                x_px = int(round(col * step_x_px))
                y_px = int(round(row * step_y_px))
                for fn in scene_files[m]:
                    tiles.append({
                        "filename": fn,
                        "x": float(x_px),
                        "y": float(y_px),
                        "w": _TILE_PX,
                        "h": _TILE_PX,
                    })
            m += 1
    return tiles


# ---------------------------------------------------------------------------
# Unified facade — auto-detects XML format
# ---------------------------------------------------------------------------

def parse_zeiss_xml(
    xml_path: Path,
) -> tuple[dict[int, list[dict]], str, float | None]:
    """
    Auto-detect XML format and parse scene/tile data.

    Returns (scenes, xml_type, pixel_scale_um) where:
        scenes         – dict mapping scene_id -> list of raw tile dicts
        xml_type       – "info" | "meta"
        pixel_scale_um – µm per pixel from _meta.xml, or None for _info.xml

    Mirrors the detection logic in gui_runner.py main() lines 668-701.
    """
    is_meta = "_meta.xml" in xml_path.name.lower() or xml_path.name.endswith("meta.xml")
    scenes: dict[int, list[dict]] = {}
    pixel_scale_um: float | None = None

    # Attempt info.xml first
    if not is_meta:
        try:
            scenes = parse_info_xml(xml_path)
        except Exception:
            is_meta = True

    # Fall back to or use meta.xml
    if is_meta or not scenes:
        meta_path = xml_path
        if not is_meta:
            meta_path = xml_path.parent / xml_path.name.replace("_info.xml", "_meta.xml")

        if not meta_path.exists():
            raise FileNotFoundError(
                f"Could not find grid coordinates. "
                f"Standard _info.xml failed and _meta.xml not found at: {meta_path}"
            )

        xml_prefix = meta_path.name.replace("_meta.xml", "")
        raw_dir = meta_path.parent
        scale_m, meta_scenes = parse_meta_xml(meta_path)

        if scale_m is not None:
            pixel_scale_um = scale_m * 1e6

        for scene_idx, scene_info in meta_scenes.items():
            tiles = build_meander_scene_tiles(
                raw_dir, xml_prefix, scene_idx,
                scene_info["cols"], scene_info["rows"],
                scene_info["step_x_px"], scene_info["step_y_px"],
            )
            if tiles:
                scenes[scene_idx] = tiles

        return scenes, "meta", pixel_scale_um

    return scenes, "info", None


def parse_zeiss_xml_to_models(
    xml_path: Path,
) -> tuple[list[SceneInfo], str, float | None]:
    """
    Auto-detect and parse — returns typed SceneInfo models.

    Returns (scene_list, xml_type, pixel_scale_um).
    """
    raw_scenes, xml_type, pixel_scale_um = parse_zeiss_xml(xml_path)
    scene_list = [
        SceneInfo(
            scene_id=sid,
            tiles=[
                TileInfo(
                    filename=t["filename"],
                    x=t["x"],
                    y=t["y"],
                    w=t["w"],
                    h=t["h"],
                )
                for t in tiles
            ],
        )
        for sid, tiles in sorted(raw_scenes.items())
    ]
    return scene_list, xml_type, pixel_scale_um


# ---------------------------------------------------------------------------
# Legacy compatibility — mirrors lib_shared.parse_xml interface
# ---------------------------------------------------------------------------

def parse_xml(xml_path: Path) -> dict[int, list[dict]]:
    """
    Legacy interface used by the numbered pipeline scripts via lib_shared.
    Delegates to parse_info_xml for _info.xml files.
    """
    if not xml_path.exists():
        raise FileNotFoundError(f"Missing XML file: {xml_path}")

    tree = ET.parse(xml_path)
    root = tree.getroot()
    scenes: dict[int, list[dict]] = defaultdict(list)

    for img in root.findall("Image"):
        fn = img.findtext("Filename")
        if not fn:
            continue
        b = img.find("Bounds")
        if b is None:
            continue
        attrib = b.attrib
        s = int(attrib.get("StartS", 0))
        scenes[s].append({
            "filename": fn,
            "x": int(attrib["StartX"]),
            "y": int(attrib["StartY"]),
            "w": int(attrib["SizeX"]),
            "h": int(attrib["SizeY"]),
            "path": None,
        })
    return dict(scenes)
