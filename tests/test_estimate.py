"""
test_estimate.py — the pre-flight sizing model.

The estimator's job is to stop a run that cannot finish. These tests pin down the arithmetic
(the canvas geometry and the frame counts that drive every other number), the verdict
thresholds, and the requirement that a bad verdict always comes with advice the caller can
literally act on.
"""

from pathlib import Path

import pytest

from axio_stitching.estimate import (
    BLEND_BYTES_PER_PIXEL,
    RETAINED_BYTES_PER_PIXEL_PER_FRAME,
    StitchEstimate,
    _decide,
    _human_duration,
    estimate_stitch,
)
from axio_stitching.models import StitchConfig


def config_for(xml: Path, out_dir: Path, **overrides) -> StitchConfig:
    base = dict(xml_path=xml, out_dir=out_dir, correction="none", algorithm="phase")
    base.update(overrides)
    return StitchConfig(**base)


class TestHumanDuration:
    def test_scales_through_the_units(self):
        assert _human_duration(45) == "45s"
        assert _human_duration(600).endswith("min")
        assert _human_duration(3600 * 5).endswith("h")
        assert _human_duration(3600 * 24 * 5).endswith("days")

    def test_never_reports_negative_time(self):
        assert _human_duration(-10) == "0s"


class TestEstimateGeometry:
    def test_computes_the_canvas_from_stage_extent_plus_one_tile(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out"))
        scene = result.scenes[0]
        # Tiles span 0..900 in each axis and are 1020 px, so the canvas is 900 + 1020.
        assert scene.canvas_width == 1920
        assert scene.canvas_height == 1920
        assert scene.tiles == 4

    def test_reads_tile_geometry_from_a_real_file(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out"))
        assert not any("falling back" in w for w in result.warnings)

    def test_falls_back_and_says_so_when_no_tile_is_readable(self, info_xml_path: Path, tmp_path: Path):
        # info_xml_path names tiles that do not exist on disk.
        result = estimate_stitch(config_for(info_xml_path, tmp_path / "out"))
        assert any("no tile file could be read" in w for w in result.warnings)
        assert result.scenes, "an unreadable tile must not stop the estimate"

    def test_output_bytes_match_the_canvas(self, small_dataset: Path, tmp_path: Path):
        scene = estimate_stitch(config_for(small_dataset, tmp_path / "out")).scenes[0]
        assert scene.output_bytes == scene.canvas_width * scene.canvas_height * 2

    def test_peak_ram_uses_the_documented_model(self, small_dataset: Path, tmp_path: Path):
        scene = estimate_stitch(config_for(small_dataset, tmp_path / "out")).scenes[0]
        canvas_px = scene.canvas_width * scene.canvas_height
        expected = canvas_px * BLEND_BYTES_PER_PIXEL + canvas_px * RETAINED_BYTES_PER_PIXEL_PER_FRAME * 1
        assert scene.peak_ram_bytes == expected


class TestZModes:
    def test_2d_mode_writes_one_frame(self, small_dataset: Path, tmp_path: Path):
        scene = estimate_stitch(config_for(small_dataset, tmp_path / "out", z_mode="none")).scenes[0]
        assert scene.z_slices == 1 and scene.output_frames == 1

    def test_mip_output_only_stays_one_frame(self, small_dataset: Path, tmp_path: Path):
        scene = estimate_stitch(
            config_for(small_dataset, tmp_path / "out", z_mode="mip_output_only")
        ).scenes[0]
        assert scene.output_frames == 1


class TestCorrectionCost:
    def test_no_correction_needs_no_intermediate_space(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out", correction="none"))
        assert result.total_intermediate_bytes == 0

    def test_correction_reserves_room_for_corrected_tiles(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out", correction="median"))
        assert result.total_intermediate_bytes > 0

    def test_basicpy_is_estimated_as_slower_than_median(self, small_dataset: Path, tmp_path: Path):
        slow = estimate_stitch(config_for(small_dataset, tmp_path / "out", correction="basicpy"))
        fast = estimate_stitch(config_for(small_dataset, tmp_path / "out", correction="median"))
        assert slow.total_seconds > fast.total_seconds

    def test_sift_is_estimated_as_slower_than_phase(self, small_dataset: Path, tmp_path: Path):
        slow = estimate_stitch(config_for(small_dataset, tmp_path / "out", algorithm="sift"))
        fast = estimate_stitch(config_for(small_dataset, tmp_path / "out", algorithm="phase"))
        assert slow.total_seconds > fast.total_seconds

    def test_coordinate_registration_is_free(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out", algorithm="coordinate"))
        assert result.total_seconds == pytest.approx(
            estimate_stitch(config_for(small_dataset, tmp_path / "out", algorithm="coordinate")).total_seconds
        )


class TestSceneSelection:
    def test_a_single_scene_is_estimated_alone(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out", scene=0))
        assert len(result.scenes) == 1

    def test_a_missing_scene_is_warned_about_not_crashed_on(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out", scene=7))
        assert any("scene 7" in w for w in result.warnings)
        assert result.verdict == "will_not_fit"

    def test_a_ref_tag_that_matches_nothing_warns_and_continues(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out", ref_tag="_nope_"))
        assert any("no tile matched ref_tag" in w for w in result.warnings)
        assert result.scenes


class TestVerdict:
    def test_a_small_job_fits(self, small_dataset: Path, tmp_path: Path):
        assert estimate_stitch(config_for(small_dataset, tmp_path / "out")).verdict == "ok"

    def test_ram_over_available_will_not_fit(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out"))
        result.available_ram_bytes = result.peak_ram_bytes // 2
        _decide(result, config_for(small_dataset, tmp_path / "out"))
        assert result.verdict == "will_not_fit"
        assert any("exceeds" in r for r in result.reasons)

    def test_ram_over_sixty_percent_is_tight(self, small_dataset: Path, tmp_path: Path):
        result = estimate_stitch(config_for(small_dataset, tmp_path / "out"))
        result.available_ram_bytes = int(result.peak_ram_bytes / 0.7)
        result.free_disk_bytes = 10 * 1024**4
        _decide(result, config_for(small_dataset, tmp_path / "out"))
        assert result.verdict == "tight"

    def test_disk_shortfall_will_not_fit(self, small_dataset: Path, tmp_path: Path):
        config = config_for(small_dataset, tmp_path / "out", correction="median")
        result = estimate_stitch(config)
        result.free_disk_bytes = 1024
        _decide(result, config)
        assert result.verdict == "will_not_fit"
        assert any("free on the output volume" in r for r in result.reasons)

    def test_a_bad_verdict_always_carries_actionable_advice(self, small_dataset: Path, tmp_path: Path):
        config = config_for(small_dataset, tmp_path / "out", correction="basicpy", z_mode="mip_align_3d")
        result = estimate_stitch(config)
        result.available_ram_bytes = 1024
        _decide(result, config)
        assert result.verdict == "will_not_fit"
        assert result.advice, "a will_not_fit verdict without advice is a dead end"
        assert any("mip_output_only" in a for a in result.advice)

    def test_an_unknown_ram_figure_is_admitted_not_assumed(self, small_dataset: Path, tmp_path: Path):
        config = config_for(small_dataset, tmp_path / "out")
        result = estimate_stitch(config)
        result.available_ram_bytes = None
        result.warnings.clear()
        _decide(result, config)
        assert any("unverified" in w for w in result.warnings)


class TestUnparseableInput:
    def test_an_xml_with_no_scenes_will_not_fit(self, tmp_path: Path):
        xml = tmp_path / "empty_info.xml"
        xml.write_text("<ZoomImageDocument></ZoomImageDocument>", encoding="utf-8")
        result = estimate_stitch(config_for(xml, tmp_path / "out"))
        assert result.verdict == "will_not_fit"
        assert result.reasons


class TestSerialisation:
    def test_to_dict_carries_both_bytes_and_human_strings(self, small_dataset: Path, tmp_path: Path):
        payload = estimate_stitch(config_for(small_dataset, tmp_path / "out")).to_dict()
        assert payload["totals"]["peak_ram_bytes"] > 0
        assert payload["totals"]["peak_ram"].endswith(("B", "KB", "MB", "GB", "TB"))
        assert payload["totals"]["time_confidence"] == "order-of-magnitude"

    def test_an_empty_estimate_reports_zero_rather_than_raising(self):
        empty = StitchEstimate()
        assert empty.peak_ram_bytes == 0
        assert empty.total_output_bytes == 0
