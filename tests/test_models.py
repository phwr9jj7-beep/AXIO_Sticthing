"""test_models.py — Pydantic model validation tests."""

import pytest
from pathlib import Path
from pydantic import ValidationError

from axio_stitching.models import (
    StitchConfig, StitchResult, ProgressEvent, SceneInfo, TileInfo,
    CorrectionMethod, StitchAlgorithm, AlignmentMode, ZMode, PipelineStage,
)


class TestStitchConfig:
    def test_required_fields(self, tmp_path):
        xml = tmp_path / "test.xml"
        xml.touch()
        cfg = StitchConfig(xml_path=xml, out_dir=tmp_path)
        assert cfg.correction == CorrectionMethod.BASICPY
        assert cfg.algorithm == StitchAlgorithm.PHASE
        assert cfg.alignment_mode == AlignmentMode.REFERENCE
        assert cfg.z_mode == ZMode.NONE

    def test_missing_xml_raises(self, tmp_path):
        with pytest.raises(ValidationError):
            StitchConfig(xml_path=tmp_path / "nonexistent.xml", out_dir=tmp_path)

    def test_correction_enum_coercion(self, tmp_path):
        xml = tmp_path / "test.xml"
        xml.touch()
        cfg = StitchConfig(xml_path=xml, out_dir=tmp_path, correction="median")
        assert cfg.correction == CorrectionMethod.MEDIAN

    def test_invalid_correction_raises(self, tmp_path):
        xml = tmp_path / "test.xml"
        xml.touch()
        with pytest.raises(ValidationError):
            StitchConfig(xml_path=xml, out_dir=tmp_path, correction="invalid_method")

    def test_negative_ref_channel_raises(self, tmp_path):
        xml = tmp_path / "test.xml"
        xml.touch()
        with pytest.raises(ValidationError):
            StitchConfig(xml_path=xml, out_dir=tmp_path, ref_channel=-1)

    def test_target_tags_list(self, tmp_path):
        xml = tmp_path / "test.xml"
        xml.touch()
        cfg = StitchConfig(xml_path=xml, out_dir=tmp_path, target_tags=["_c2_", "_c3_"])
        assert cfg.target_tags == ["_c2_", "_c3_"]


class TestStitchResult:
    def test_defaults(self):
        r = StitchResult(success=True)
        assert r.output_paths == []
        assert r.duration_seconds == 0.0

    def test_to_dict_paths_as_strings(self, tmp_path):
        r = StitchResult(success=True, output_paths=[tmp_path / "out.tif"])
        d = r.to_dict()
        assert all(isinstance(p, str) for p in d["output_paths"])


class TestProgressEvent:
    def test_to_stdout_format(self):
        e = ProgressEvent(percent=42, status_message="Testing", stage=PipelineStage.CANVAS)
        out = e.to_stdout()
        assert "[STATUS] Testing" in out
        assert "[PROGRESS] 42" in out


class TestSceneInfo:
    def test_total_tiles_computed(self):
        tiles = [TileInfo(filename=f"t{i}.tif", x=float(i * 1020), y=0, w=1020, h=1020) for i in range(5)]
        scene = SceneInfo(scene_id=0, tiles=tiles)
        assert scene.total_tiles == 5
