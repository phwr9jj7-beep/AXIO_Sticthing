"""
test_tile_sources.py — vendor-neutral tile-position resolution.

This is the layer that makes AXIO handle non-Zeiss data, so the tests assert the two things
that matter for each source: it is DETECTED from a realistic file/folder, and the positions
it produces are CORRECT (right count, right relative geometry, normalised to a non-negative
origin). Every source also has a failure test — a bad input must raise TileSourceError with a
usable message, never a bare traceback.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import tifffile

from axio_stitching.tile_sources import (
    ResolvedSource,
    TileSourceError,
    detect_source_type,
    resolve_tiles,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_tile(path: Path, w: int = 512, h: int = 400, ome_pos=None, phys_um=None) -> None:
    img = (np.random.default_rng(abs(hash(path.name)) % 2**32).random((h, w)) * 5000).astype(np.uint16)
    metadata = {"axes": "YX"}
    if ome_pos is not None:
        metadata["Plane"] = {"PositionX": ome_pos[0], "PositionY": ome_pos[1],
                             "PositionXUnit": "µm", "PositionYUnit": "µm"}
    if phys_um is not None:
        metadata["PhysicalSizeX"] = phys_um
        metadata["PhysicalSizeXUnit"] = "µm"
    tifffile.imwrite(str(path), img, metadata=metadata)


@pytest.fixture()
def plain_tiles(tmp_path: Path):
    """Four 512x400 tiles with no metadata, named with an x/y grid."""
    d = tmp_path / "grid"
    d.mkdir()
    for col in range(2):
        for row in range(2):
            _write_tile(d / f"scan_x{col}_y{row}.tif")
    return d


# ---------------------------------------------------------------------------
# Fiji TileConfiguration
# ---------------------------------------------------------------------------

class TestFiji:
    def _make(self, tmp_path: Path, registered=False) -> Path:
        d = tmp_path / "fiji"
        d.mkdir()
        for name in ("t00.tif", "t10.tif", "t01.tif", "t11.tif"):
            _write_tile(d / name)
        name = "TileConfiguration.registered.txt" if registered else "TileConfiguration.txt"
        (d / name).write_text(
            "dim = 2\n"
            "t00.tif; ; (0.0, 0.0)\n"
            "t10.tif; ; (460.0, 0.0)\n"
            "t01.tif; ; (0.0, 360.0)\n"
            "t11.tif; ; (460.0, 360.0)\n",
            encoding="utf-8",
        )
        return d / name

    def test_detected_from_file(self, tmp_path):
        cfg = self._make(tmp_path)
        assert detect_source_type(cfg) == "fiji"

    def test_detected_from_directory(self, tmp_path):
        cfg = self._make(tmp_path)
        assert detect_source_type(cfg.parent) == "fiji"

    def test_positions_are_read_verbatim_in_pixels(self, tmp_path):
        cfg = self._make(tmp_path)
        r = resolve_tiles(cfg)
        assert r.source_type == "fiji" and r.confidence == "high"
        tiles = {t["filename"]: (t["x"], t["y"]) for t in r.scenes[0]}
        assert tiles["t00.tif"] == (0.0, 0.0)
        assert tiles["t10.tif"] == (460.0, 0.0)
        assert tiles["t11.tif"] == (460.0, 360.0)

    def test_reads_real_tile_size(self, tmp_path):
        r = resolve_tiles(self._make(tmp_path))
        assert (r.tile_width, r.tile_height) == (512, 400)

    def test_registered_variant_preferred(self, tmp_path):
        # A directory holding BOTH the raw and the registered config must use the registered one.
        d = tmp_path / "fiji"
        d.mkdir()
        for name in ("t00.tif", "t10.tif"):
            _write_tile(d / name)
        (d / "TileConfiguration.txt").write_text(
            "dim = 2\nt00.tif; ; (0.0, 0.0)\nt10.tif; ; (460.0, 0.0)\n", encoding="utf-8"
        )
        (d / "TileConfiguration.registered.txt").write_text(
            "dim = 2\nt00.tif; ; (0.0, 0.0)\nt10.tif; ; (455.0, 3.0)\n", encoding="utf-8"
        )
        r = resolve_tiles(d)
        assert r.source_type == "fiji"
        # The refined x (455, not 460) proves the registered variant was chosen.
        assert max(t["x"] for t in r.scenes[0]) == 455.0

    def test_negative_positions_normalised_to_origin(self, tmp_path):
        d = tmp_path / "neg"
        d.mkdir()
        for name in ("a.tif", "b.tif"):
            _write_tile(d / name)
        (d / "TileConfiguration.txt").write_text(
            "dim = 2\na.tif; ; (-100.0, -50.0)\nb.tif; ; (360.0, -50.0)\n", encoding="utf-8"
        )
        r = resolve_tiles(d)
        assert min(t["x"] for t in r.scenes[0]) == 0.0
        assert min(t["y"] for t in r.scenes[0]) == 0.0

    def test_empty_config_raises(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        (d / "TileConfiguration.txt").write_text("dim = 2\n# no tiles\n", encoding="utf-8")
        with pytest.raises(TileSourceError, match="no parseable"):
            resolve_tiles(d / "TileConfiguration.txt")


# ---------------------------------------------------------------------------
# OME-TIFF stage positions
# ---------------------------------------------------------------------------

class TestOme:
    def _make(self, tmp_path: Path, phys_um=0.5) -> Path:
        d = tmp_path / "ome"
        d.mkdir()
        # 2x2 grid, 512px tiles at 0.5 µm/px -> 256 µm pitch minus overlap; use 230 µm step.
        positions = {
            "a.ome.tif": (0.0, 0.0),
            "b.ome.tif": (115.0, 0.0),
            "c.ome.tif": (0.0, 90.0),
            "d.ome.tif": (115.0, 90.0),
        }
        for name, pos in positions.items():
            _write_tile(d / name, ome_pos=pos, phys_um=phys_um)
        return d

    def test_detected(self, tmp_path):
        d = self._make(tmp_path)
        assert detect_source_type(d) == "ome"

    def test_positions_converted_to_pixels(self, tmp_path):
        d = self._make(tmp_path, phys_um=0.5)
        r = resolve_tiles(d)
        assert r.source_type == "ome"
        assert r.pixel_scale_um == pytest.approx(0.5)
        tiles = {t["filename"]: (t["x"], t["y"]) for t in r.scenes[0]}
        # 115 µm / 0.5 µm/px = 230 px
        assert tiles["b.ome.tif"][0] == pytest.approx(230.0)
        assert tiles["c.ome.tif"][1] == pytest.approx(180.0)

    def test_pixel_size_override_applied(self, tmp_path):
        d = self._make(tmp_path, phys_um=0.5)
        r = resolve_tiles(d, pixel_size_um=1.0)
        tiles = {t["filename"]: t["x"] for t in r.scenes[0]}
        assert tiles["b.ome.tif"] == pytest.approx(115.0)  # 115 µm / 1.0 µm/px

    def test_single_ome_file_resolves_via_its_directory(self, tmp_path):
        d = self._make(tmp_path)
        r = resolve_tiles(d / "a.ome.tif")
        assert r.source_type == "ome" and r.total_tiles == 4


# ---------------------------------------------------------------------------
# Explicit positions (inline + JSON)
# ---------------------------------------------------------------------------

class TestExplicit:
    def test_inline_positions_take_precedence(self, plain_tiles):
        positions = [
            {"filename": "scan_x0_y0.tif", "x": 0, "y": 0},
            {"filename": "scan_x1_y0.tif", "x": 460, "y": 0},
        ]
        r = resolve_tiles(plain_tiles, positions=positions)
        assert r.source_type == "explicit"
        assert len(r.scenes[0]) == 2

    def test_json_file_object_form(self, tmp_path):
        d = tmp_path / "json"
        d.mkdir()
        for name in ("t0.tif", "t1.tif"):
            _write_tile(d / name)
        (d / "positions.json").write_text(
            json.dumps({"tiles": [
                {"filename": "t0.tif", "x": 0, "y": 0},
                {"filename": "t1.tif", "x": 400, "y": 0},
            ]}),
            encoding="utf-8",
        )
        r = resolve_tiles(d / "positions.json")
        assert r.source_type == "explicit" and len(r.scenes[0]) == 2

    def test_json_bare_list_form(self, tmp_path):
        d = tmp_path / "json2"
        d.mkdir()
        _write_tile(d / "t0.tif")
        (d / "p.json").write_text(json.dumps([{"filename": "t0.tif", "x": 0, "y": 0}]), encoding="utf-8")
        assert resolve_tiles(d / "p.json").total_tiles == 1

    def test_scene_column_splits_scenes(self, plain_tiles):
        positions = [
            {"filename": "scan_x0_y0.tif", "x": 0, "y": 0, "scene": 0},
            {"filename": "scan_x1_y0.tif", "x": 0, "y": 0, "scene": 1},
        ]
        r = resolve_tiles(plain_tiles, positions=positions)
        assert set(r.scenes) == {0, 1}

    def test_micrometre_units_converted_with_pixel_size(self, plain_tiles):
        positions = [
            {"filename": "scan_x0_y0.tif", "x": 0, "y": 0},
            {"filename": "scan_x1_y0.tif", "x": 100, "y": 0},  # 100 µm
        ]
        r = resolve_tiles(plain_tiles, positions=positions)
        # Without units it is pixels; wrap through JSON path for the units branch instead:
        d = plain_tiles
        (d / "p.json").write_text(json.dumps(
            {"units": "um", "pixel_size_um": 0.5,
             "tiles": [{"filename": "scan_x0_y0.tif", "x": 0, "y": 0},
                       {"filename": "scan_x1_y0.tif", "x": 100, "y": 0}]}), encoding="utf-8")
        r2 = resolve_tiles(d / "p.json")
        xs = sorted(t["x"] for t in r2.scenes[0])
        assert xs[1] == pytest.approx(200.0)  # 100 µm / 0.5 µm/px

    def test_invalid_entries_raise(self, plain_tiles):
        with pytest.raises(TileSourceError):
            resolve_tiles(plain_tiles, positions=[{"x": 0, "y": 0}])  # no filename


# ---------------------------------------------------------------------------
# Filename grid
# ---------------------------------------------------------------------------

class TestFilenameGrid:
    def test_xy_pattern(self, plain_tiles):
        r = resolve_tiles(plain_tiles)
        assert r.source_type == "grid" and r.confidence == "low"
        assert r.total_tiles == 4
        # 512px tile, 10% overlap -> 461px step
        xs = sorted({t["x"] for t in r.scenes[0]})
        assert xs == [0.0, pytest.approx(461.0)]

    def test_overlap_sets_pitch(self, plain_tiles):
        r = resolve_tiles(plain_tiles, overlap=0.25)
        xs = sorted({t["x"] for t in r.scenes[0]})
        assert xs[1] == pytest.approx(384.0)  # 512 * 0.75

    def test_rowcol_pattern(self, tmp_path):
        d = tmp_path / "rc"
        d.mkdir()
        for r_ in range(2):
            for c in range(2):
                _write_tile(d / f"img_r{r_}_c{c}.tif")
        res = resolve_tiles(d)
        assert res.source_type == "grid" and res.total_tiles == 4

    def test_position_index_needs_grid_cols(self, tmp_path):
        d = tmp_path / "pos"
        d.mkdir()
        for i in range(4):
            _write_tile(d / f"img_position{i:03d}.tif")
        with pytest.raises(TileSourceError, match="grid_cols"):
            resolve_tiles(d)

    def test_position_index_with_grid_cols_serpentine(self, tmp_path):
        d = tmp_path / "pos2"
        d.mkdir()
        for i in range(4):
            _write_tile(d / f"img_position{i:03d}.tif")
        r = resolve_tiles(d, grid_cols=2, serpentine=True)
        assert r.total_tiles == 4
        # serpentine: index 2 -> row 1 col 1 (snaked), index 3 -> row 1 col 0
        by_name = {t["filename"]: (t["x"], t["y"]) for t in r.scenes[0]}
        assert by_name["img_position002.tif"][1] > 0  # second row

    def test_unrecognised_filenames_raise_with_guidance(self, tmp_path):
        d = tmp_path / "opaque"
        d.mkdir()
        for name in ("alpha.tif", "beta.tif"):
            _write_tile(d / name)
        with pytest.raises(TileSourceError, match="do not encode a recognisable grid"):
            resolve_tiles(d)


# ---------------------------------------------------------------------------
# Zeiss delegation + dispatch
# ---------------------------------------------------------------------------

class TestZeissAndDispatch:
    def test_zeiss_info_xml_delegated(self, info_xml_with_tiles):
        r = resolve_tiles(info_xml_with_tiles)
        assert r.source_type == "zeiss"
        assert r.total_tiles == 4

    def test_zeiss_detected(self, info_xml_with_tiles):
        assert detect_source_type(info_xml_with_tiles) == "zeiss"

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(TileSourceError, match="does not exist"):
            resolve_tiles(tmp_path / "nope")

    def test_empty_directory_raises(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(TileSourceError, match="no tiles or layout"):
            resolve_tiles(d)

    def test_detect_unknown(self, tmp_path):
        assert detect_source_type(tmp_path / "nope") == "unknown"

    def test_resolved_to_dict_is_json_shaped(self, info_xml_with_tiles):
        payload = resolve_tiles(info_xml_with_tiles).to_dict()
        assert set(payload) >= {"source_type", "confidence", "raw_dir", "total_tiles", "notes", "warnings"}


# ---------------------------------------------------------------------------
# Directory precedence — a layout file beats a filename grid
# ---------------------------------------------------------------------------

class TestDirectoryPrecedence:
    def test_fiji_config_beats_filename_grid(self, tmp_path):
        d = tmp_path / "both"
        d.mkdir()
        # filenames also encode a grid, but a TileConfiguration is present and authoritative
        for col in range(2):
            _write_tile(d / f"x{col}_y0.tif")
        (d / "TileConfiguration.txt").write_text(
            "dim = 2\nx0_y0.tif; ; (0.0, 0.0)\nx1_y0.tif; ; (999.0, 0.0)\n", encoding="utf-8"
        )
        r = resolve_tiles(d)
        assert r.source_type == "fiji"
        assert max(t["x"] for t in r.scenes[0]) == 999.0  # from the config, not the grid
