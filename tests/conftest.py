"""
conftest.py
-----------
Shared pytest fixtures for the AXIO Stitching test suite.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import tifffile


# ---------------------------------------------------------------------------
# Synthetic XML fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def info_xml_path(tmp_path: Path) -> Path:
    """Minimal synthetic _info.xml with 4 tiles in 2 scenes."""
    tiles = [
        dict(filename="tile_s0m1_ORG.tif", StartX=0, StartY=0, SizeX=1020, SizeY=1020, StartS=0),
        dict(filename="tile_s0m2_ORG.tif", StartX=1020, StartY=0, SizeX=1020, SizeY=1020, StartS=0),
        dict(filename="tile_s0m3_ORG.tif", StartX=0, StartY=1020, SizeX=1020, SizeY=1020, StartS=0),
        dict(filename="tile_s0m4_ORG.tif", StartX=1020, StartY=1020, SizeX=1020, SizeY=1020, StartS=0),
        dict(filename="tile_s1m1_ORG.tif", StartX=0, StartY=0, SizeX=1020, SizeY=1020, StartS=1),
    ]
    # parse_info_xml expects <Image> as direct children of root
    root = ET.Element("ZoomImageDocument")
    for t in tiles:
        img = ET.SubElement(root, "Image")
        fn_el = ET.SubElement(img, "Filename")
        fn_el.text = t["filename"]
        bounds = ET.SubElement(img, "Bounds")
        bounds.set("StartX", str(t["StartX"]))
        bounds.set("StartY", str(t["StartY"]))
        bounds.set("SizeX", str(t["SizeX"]))
        bounds.set("SizeY", str(t["SizeY"]))
        bounds.set("StartS", str(t["StartS"]))

    xml_path = tmp_path / "test_info.xml"
    ET.ElementTree(root).write(str(xml_path))
    return xml_path


@pytest.fixture()
def raw_tiles_dir(tmp_path: Path) -> Path:
    """Create 4 synthetic 1020x1020 TIFF tiles."""
    tile_dir = tmp_path / "raw"
    tile_dir.mkdir()
    for i in range(1, 5):
        img = np.random.randint(100, 3000, (1020, 1020), dtype=np.uint16)
        tifffile.imwrite(str(tile_dir / f"tile_s0m{i}_ORG.tif"), img)
    return tile_dir


@pytest.fixture()
def info_xml_with_tiles(raw_tiles_dir: Path) -> Path:
    """_info.xml whose tile files actually exist in raw_tiles_dir."""
    tiles_data = [
        dict(filename="tile_s0m1_ORG.tif", StartX=0, StartY=0, SizeX=1020, SizeY=1020, StartS=0),
        dict(filename="tile_s0m2_ORG.tif", StartX=1020, StartY=0, SizeX=1020, SizeY=1020, StartS=0),
        dict(filename="tile_s0m3_ORG.tif", StartX=0, StartY=1020, SizeX=1020, SizeY=1020, StartS=0),
        dict(filename="tile_s0m4_ORG.tif", StartX=1020, StartY=1020, SizeX=1020, SizeY=1020, StartS=0),
    ]
    # XML placed in same dir as tiles so parse_zeiss_xml can locate them
    root = ET.Element("ZoomImageDocument")
    for t in tiles_data:
        img = ET.SubElement(root, "Image")
        fn_el = ET.SubElement(img, "Filename")
        fn_el.text = t["filename"]
        bounds = ET.SubElement(img, "Bounds")
        for k, v in t.items():
            if k != "filename":
                bounds.set(k, str(v))

    xml_path = raw_tiles_dir / "test_info.xml"
    ET.ElementTree(root).write(str(xml_path))
    return xml_path


# ---------------------------------------------------------------------------
# Fixtures for estimate / qc / jobs
# ---------------------------------------------------------------------------

@pytest.fixture()
def small_dataset(tmp_path: Path) -> Path:
    """
    A 2x2 tile scan whose files really exist, with a deliberate overlap so registration has
    something to do. Returns the path to the _info.xml.
    """
    raw = tmp_path / "dataset"
    raw.mkdir()
    rng = np.random.default_rng(1234)
    base = (rng.random((1400, 1400)) * 20000 + 2000).astype(np.uint16)

    tiles = []
    for index, (x, y) in enumerate([(0, 0), (900, 0), (0, 900), (900, 900)], start=1):
        filename = f"tile_s0m{index}_ORG.tif"
        crop = base[y:y + 1020, x:x + 1020]
        tifffile.imwrite(str(raw / filename), np.ascontiguousarray(crop))
        tiles.append(
            dict(filename=filename, StartX=x, StartY=y, SizeX=1020, SizeY=1020, StartS=0)
        )

    root = ET.Element("ZoomImageDocument")
    for tile in tiles:
        image = ET.SubElement(root, "Image")
        ET.SubElement(image, "Filename").text = tile["filename"]
        bounds = ET.SubElement(image, "Bounds")
        for key, value in tile.items():
            if key != "filename":
                bounds.set(key, str(value))

    xml_path = raw / "scan_info.xml"
    ET.ElementTree(root).write(str(xml_path))
    return xml_path


@pytest.fixture()
def stitched_tiff(tmp_path: Path) -> Path:
    """A small deflate-compressed 16-bit mosaic, written the way the pipeline writes them."""
    rng = np.random.default_rng(99)
    canvas = (rng.random((600, 800)) * 30000 + 500).astype(np.uint16)
    out = tmp_path / "stitched_scene0_phase.tif"
    tifffile.imwrite(
        str(out), canvas, imagej=True, photometric="minisblack",
        compression="deflate", metadata={"axes": "YX"},
    )
    return out


@pytest.fixture()
def seamed_tiff(tmp_path: Path) -> Path:
    """A mosaic with a hard vertical edge and a large empty region - QC must notice both."""
    canvas = np.zeros((600, 800), dtype=np.uint16)
    canvas[:, :400] = 30000
    out = tmp_path / "stitched_scene1_phase.tif"
    tifffile.imwrite(
        str(out), canvas, imagej=True, photometric="minisblack",
        compression="deflate", metadata={"axes": "YX"},
    )
    return out
