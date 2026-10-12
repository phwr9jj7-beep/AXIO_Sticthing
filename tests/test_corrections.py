"""
Shading-correction output paths must never resolve onto the raw tiles (issue #13).

A positions JSON with ABSOLUTE tile names used to make ``out_dir / name`` collapse onto the
raw tile (``pathlib`` drops the left operand for an absolute right operand): the "already
corrected" check then saw the raw file and silently skipped the correction, and only a
per-tile existence check stood between the writer and the raw data.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import tifffile

from axio_stitching import tile_sources
from axio_stitching.corrections import (
    CorrectionPathError,
    corrected_tile_path,
    run_correction,
)
from axio_stitching.engine import StitchingEngine
from axio_stitching.models import StitchConfig
from axio_stitching.tile_sources import resolve_tiles


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def tiles_and_abs_json(tmp_path: Path) -> tuple[Path, Path]:
    """Four textured tiles in ``tiles/`` and a positions JSON elsewhere naming them ABSOLUTELY."""
    tiles = tmp_path / "raw" / "tiles"
    tiles.mkdir(parents=True)
    rng = np.random.default_rng(7)
    base = (rng.random((300, 300)) * 20000 + 1000).astype(np.uint16)
    entries = []
    for i, (x, y) in enumerate([(0, 0), (100, 0), (0, 100), (100, 100)], start=1):
        name = f"t{i:02d}.tif"
        tifffile.imwrite(str(tiles / name), np.ascontiguousarray(base[y:y + 160, x:x + 160]))
        entries.append({"filename": str((tiles / name).resolve()), "x": x, "y": y})
    meta = tmp_path / "meta"
    meta.mkdir()
    js = meta / "positions.json"
    js.write_text(json.dumps({"tiles": entries}), encoding="utf-8")
    return tiles, js


class TestRebase:
    def test_absolute_names_are_made_relative_to_their_directory(self, tiles_and_abs_json):
        tiles, js = tiles_and_abs_json
        r = resolve_tiles(js)
        assert Path(r.raw_dir).resolve() == tiles.resolve()
        names = [t["filename"] for t in r.scenes[0]]
        assert names == ["t01.tif", "t02.tif", "t03.tif", "t04.tif"]
        assert r.external_paths is False
        assert any("tile directory" in n for n in r.notes)

    def test_relative_names_are_left_alone(self, tmp_path):
        js = tmp_path / "p.json"
        js.write_text(json.dumps([{"filename": "a.tif", "x": 0, "y": 0}]), encoding="utf-8")
        r = resolve_tiles(js)
        assert r.raw_dir == tmp_path
        assert r.scenes[0][0]["filename"] == "a.tif"
        assert r.external_paths is False

    def test_dotdot_names_are_normalised(self, tiles_and_abs_json, tmp_path):
        tiles, _ = tiles_and_abs_json
        js = tmp_path / "meta" / "rel.json"
        js.write_text(json.dumps([{"filename": "../raw/tiles/t01.tif", "x": 0, "y": 0}]),
                      encoding="utf-8")
        r = resolve_tiles(js)
        assert Path(r.raw_dir).resolve() == tiles.resolve()
        assert r.scenes[0][0]["filename"] == "t01.tif"

    def test_paths_on_several_drives_are_flagged_external(self, tiles_and_abs_json, monkeypatch):
        _, js = tiles_and_abs_json

        def no_common(_paths):
            raise ValueError("Paths don't have the same drive")

        monkeypatch.setattr(tile_sources.os.path, "commonpath", no_common)
        r = resolve_tiles(js)
        assert r.external_paths is True
        assert any("several drives" in w for w in r.warnings)
        assert Path(r.scenes[0][0]["filename"]).is_absolute()


class TestCorrectedTilePath:
    def test_relative_name_lands_inside_out_dir(self, tmp_path):
        out = corrected_tile_path(tmp_path / "out", tmp_path / "raw", "sub/a.tif")
        assert out == (tmp_path / "out" / "sub" / "a.tif").resolve() or \
            str(out).endswith(str(Path("out/sub/a.tif")))

    def test_absolute_name_is_refused(self, tmp_path):
        raw = tmp_path / "raw"
        with pytest.raises(CorrectionPathError, match="absolute tile path"):
            corrected_tile_path(tmp_path / "out", raw, str((raw / "a.tif").resolve()))

    def test_escaping_name_is_refused(self, tmp_path):
        with pytest.raises(CorrectionPathError, match="outside"):
            corrected_tile_path(tmp_path / "out", tmp_path / "raw", "../raw/a.tif")

    def test_output_onto_source_is_refused(self, tmp_path):
        with pytest.raises(CorrectionPathError, match="overwrite the source"):
            corrected_tile_path(tmp_path / "raw", tmp_path / "raw", "a.tif")


class TestRunCorrection:
    def _tiles(self, tiles: Path) -> list[dict]:
        return [{"filename": p.name} for p in sorted(tiles.glob("*.tif"))]

    def test_median_writes_only_under_out_dir_and_never_touches_raw(self, tiles_and_abs_json, tmp_path):
        tiles, _ = tiles_and_abs_json
        before = {p.name: _sha(p) for p in tiles.glob("*.tif")}
        out = tmp_path / "out" / "intermediate" / "scene0" / "median_corrected"
        result = run_correction(tiles, self._tiles(tiles), out, method="median")
        assert result == out
        assert sorted(p.name for p in out.glob("*.tif")) == sorted(before)
        assert {p.name: _sha(p) for p in tiles.glob("*.tif")} == before

    def test_out_dir_equal_to_tile_dir_is_refused_before_anything_is_written(self, tiles_and_abs_json):
        tiles, _ = tiles_and_abs_json
        before = {p.name: _sha(p) for p in tiles.glob("*.tif")}
        with pytest.raises(CorrectionPathError):
            run_correction(tiles, self._tiles(tiles), tiles, method="median")
        assert {p.name: _sha(p) for p in tiles.glob("*.tif")} == before

    def test_absolute_names_are_refused_before_anything_is_written(self, tiles_and_abs_json, tmp_path):
        tiles, _ = tiles_and_abs_json
        absolute = [{"filename": str(p.resolve())} for p in sorted(tiles.glob("*.tif"))]
        out = tmp_path / "out_abs"
        with pytest.raises(CorrectionPathError, match="absolute tile path"):
            run_correction(tiles, absolute, out, method="median")
        assert not out.exists()

    def test_reusing_existing_outputs_is_announced_as_a_warning(self, tiles_and_abs_json, tmp_path):
        tiles, _ = tiles_and_abs_json
        out = tmp_path / "out_reuse"
        run_correction(tiles, self._tiles(tiles), out, method="median")
        messages: list[str] = []
        run_correction(tiles, self._tiles(tiles), out, method="median",
                       progress_callback=lambda e: messages.append(e.status_message))
        reuse = [m for m in messages if "already exist" in m]
        assert reuse and reuse[0].startswith("[warning]") and str(out) in reuse[0]

    def test_median_does_not_need_basicpy(self, tiles_and_abs_json, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_basicpy(name, *args, **kwargs):
            if name == "basicpy":
                raise ImportError("basicpy is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_basicpy)
        tiles, _ = tiles_and_abs_json
        out = tmp_path / "out_nobasic"
        run_correction(tiles, self._tiles(tiles), out, method="median")
        assert len(list(out.glob("*.tif"))) == 4


class TestEngine:
    def test_stitch_with_absolute_json_and_median_corrects_and_keeps_raw_intact(
        self, tiles_and_abs_json, tmp_path,
    ):
        tiles, js = tiles_and_abs_json
        before = {p.name: _sha(p) for p in tiles.glob("*.tif")}
        cfg = StitchConfig(source=js, out_dir=tmp_path / "stitch", correction="median",
                           algorithm="coordinate")
        result = StitchingEngine(cfg).run()
        assert result.success, result.error_message
        corrected = tmp_path / "stitch" / "intermediate" / "scene0" / "median_corrected"
        assert len(list(corrected.glob("*.tif"))) == 4
        assert {p.name: _sha(p) for p in tiles.glob("*.tif")} == before

    def test_validate_refuses_a_correction_for_external_paths(self, tiles_and_abs_json, tmp_path, monkeypatch):
        _, js = tiles_and_abs_json
        monkeypatch.setattr(tile_sources.os.path, "commonpath",
                            lambda _p: (_ for _ in ()).throw(ValueError("drives")))
        bad = StitchingEngine(StitchConfig(source=js, out_dir=tmp_path / "v1", correction="median",
                                           algorithm="coordinate")).validate_config()
        assert bad["valid"] is False
        assert any("cannot run on this source" in e for e in bad["errors"])
        ok = StitchingEngine(StitchConfig(source=js, out_dir=tmp_path / "v2", correction="none",
                                          algorithm="coordinate")).validate_config()
        assert not any("cannot run on this source" in e for e in ok["errors"])

    def test_run_refuses_a_correction_for_external_paths(self, tiles_and_abs_json, tmp_path, monkeypatch):
        tiles, js = tiles_and_abs_json
        before = {p.name: _sha(p) for p in tiles.glob("*.tif")}
        monkeypatch.setattr(tile_sources.os.path, "commonpath",
                            lambda _p: (_ for _ in ()).throw(ValueError("drives")))
        result = StitchingEngine(StitchConfig(source=js, out_dir=tmp_path / "r", correction="median",
                                              algorithm="coordinate")).run()
        assert result.success is False
        assert "cannot run on this source" in (result.error_message or "")
        assert {p.name: _sha(p) for p in tiles.glob("*.tif")} == before
