"""
test_qc.py — bounded quality control over a stitched mosaic.

Two properties matter most and are tested directly: the streaming path must produce the SAME
numbers as the whole-frame path (otherwise QC on a real gigapixel mosaic would be measuring
something else), and the findings must actually fire on a mosaic that is visibly broken.
"""

from pathlib import Path

import numpy as np
import pytest
import tifffile

from axio_stitching import qc
from axio_stitching.qc import list_outputs, qc_report


class TestQcReportBasics:
    def test_reads_a_normal_mosaic(self, stitched_tiff: Path):
        report = qc_report(stitched_tiff)
        assert report.ok
        assert (report.width, report.height) == (800, 600)
        assert report.dtype == "uint16"
        assert report.total_pixels == 480000

    def test_reports_a_missing_file_rather_than_raising(self, tmp_path: Path):
        report = qc_report(tmp_path / "nope.tif")
        assert not report.ok and "does not exist" in (report.error or "")

    def test_reports_a_non_tiff_rather_than_raising(self, tmp_path: Path):
        junk = tmp_path / "junk.tif"
        junk.write_bytes(b"not a tiff at all")
        report = qc_report(junk)
        assert not report.ok and report.error

    def test_rejects_an_out_of_range_frame(self, stitched_tiff: Path):
        report = qc_report(stitched_tiff, frame=99)
        assert not report.ok and "out of range" in (report.error or "")

    def test_to_dict_is_json_shaped(self, stitched_tiff: Path):
        payload = qc_report(stitched_tiff).to_dict()
        assert payload["ok"] is True
        assert set(payload["metrics"]) >= {
            "mean", "std", "empty_fraction", "saturated_fraction",
            "percentiles", "seam_prominence_x", "seam_prominence_y",
        }


class TestMetrics:
    def test_an_empty_canvas_is_reported_as_empty(self, tmp_path: Path):
        blank = tmp_path / "stitched_blank.tif"
        tifffile.imwrite(str(blank), np.zeros((200, 200), dtype=np.uint16), compression="deflate")
        report = qc_report(blank)
        assert report.metrics["empty_fraction"] == 1.0
        assert any("no image" in f or "empty" in f for f in report.findings)

    def test_saturation_is_measured(self, tmp_path: Path):
        canvas = np.full((200, 200), 65535, dtype=np.uint16)
        path = tmp_path / "stitched_hot.tif"
        tifffile.imwrite(str(path), canvas, compression="deflate")
        report = qc_report(path)
        assert report.metrics["saturated_fraction"] == 1.0
        assert any("clipped" in f for f in report.findings)

    def test_percentiles_bracket_the_data(self, stitched_tiff: Path):
        percentiles = qc_report(stitched_tiff).metrics["percentiles"]
        assert percentiles["p1"] < percentiles["p50"] < percentiles["p99"] <= percentiles["p99_9"]

    def test_mean_matches_numpy(self, stitched_tiff: Path):
        expected = float(tifffile.imread(str(stitched_tiff)).mean())
        assert qc_report(stitched_tiff).metrics["mean"] == pytest.approx(expected, rel=1e-6)

    def test_a_clean_mosaic_has_low_seam_prominence(self, stitched_tiff: Path):
        metrics = qc_report(stitched_tiff).metrics
        assert metrics["seam_prominence_x"] < 3
        assert metrics["seam_prominence_y"] < 3

    def test_a_hard_edge_raises_seam_prominence_and_is_reported(self, seamed_tiff: Path):
        report = qc_report(seamed_tiff)
        assert report.metrics["seam_prominence_x"] >= 6
        assert report.metrics["seam_ridges_x"][0] in range(395, 405)
        assert any("hard edge runs across the x axis" in f for f in report.findings)

    def test_a_half_empty_canvas_is_reported(self, seamed_tiff: Path):
        report = qc_report(seamed_tiff)
        assert report.metrics["empty_fraction"] == pytest.approx(0.5, abs=0.01)
        assert any("empty" in f for f in report.findings)


class TestStreamingEquivalence:
    def test_streamed_and_full_reads_agree(self, stitched_tiff: Path, monkeypatch):
        """
        The whole point of the streaming path is that it is the same measurement at lower
        cost. Force it on a small file and compare against the whole-frame read.
        """
        full = qc_report(stitched_tiff)
        assert full.method == "full"

        monkeypatch.setattr(qc, "STREAM_THRESHOLD_PIXELS", 0)
        streamed = qc_report(stitched_tiff)
        assert streamed.method == "streamed"

        for key in ("mean", "std", "min", "max", "empty_fraction", "saturated_fraction"):
            assert streamed.metrics[key] == pytest.approx(full.metrics[key], rel=1e-6), key
        assert streamed.metrics["percentiles"] == full.metrics["percentiles"]

    def test_streamed_seam_detection_still_finds_the_edge(self, seamed_tiff: Path, monkeypatch):
        monkeypatch.setattr(qc, "STREAM_THRESHOLD_PIXELS", 0)
        report = qc_report(seamed_tiff)
        assert report.method == "streamed"
        assert report.metrics["seam_prominence_x"] >= 6


class TestMultiFrame:
    def test_defaults_to_the_middle_frame_of_a_stack(self, tmp_path: Path):
        volume = np.zeros((5, 100, 100), dtype=np.uint16)
        volume[2] = 12345  # only the middle slice carries signal
        path = tmp_path / "stitched_stack.tif"
        tifffile.imwrite(
            str(path), volume, imagej=True, photometric="minisblack",
            compression="deflate", metadata={"axes": "ZYX"},
        )
        report = qc_report(path)
        assert report.frame_index == 2
        assert report.metrics["mean"] == pytest.approx(12345, rel=1e-6)

    def test_an_explicit_frame_is_honoured(self, tmp_path: Path):
        volume = np.zeros((5, 100, 100), dtype=np.uint16)
        volume[2] = 12345
        path = tmp_path / "stitched_stack.tif"
        tifffile.imwrite(
            str(path), volume, imagej=True, photometric="minisblack",
            compression="deflate", metadata={"axes": "ZYX"},
        )
        report = qc_report(path, frame=0)
        assert report.frame_index == 0
        assert report.metrics["empty_fraction"] == 1.0


class TestListOutputs:
    def test_lists_stitched_files_with_their_previews(self, stitched_tiff: Path):
        preview = stitched_tiff.with_name(stitched_tiff.stem + "_preview.png")
        preview.write_bytes(b"\x89PNG\r\n\x1a\n")

        found = list_outputs(stitched_tiff.parent)
        assert len(found) == 1
        assert found[0]["name"] == stitched_tiff.name
        assert found[0]["preview_path"] == str(preview)
        assert found[0]["axes"] == "YX"
        assert found[0]["shape"] == [600, 800]

    def test_reports_a_missing_preview_as_none(self, stitched_tiff: Path):
        assert list_outputs(stitched_tiff.parent)[0]["preview_path"] is None

    def test_ignores_files_that_are_not_stitched_output(self, stitched_tiff: Path):
        (stitched_tiff.parent / "notes.txt").write_text("hi", encoding="utf-8")
        (stitched_tiff.parent / "some_other.tif").write_bytes(b"x")
        assert [entry["name"] for entry in list_outputs(stitched_tiff.parent)] == [stitched_tiff.name]

    def test_a_missing_directory_is_empty_not_an_error(self, tmp_path: Path):
        assert list_outputs(tmp_path / "nope") == []

    def test_one_unreadable_file_does_not_sink_the_listing(self, stitched_tiff: Path):
        (stitched_tiff.parent / "stitched_broken.tif").write_bytes(b"not a tiff")
        names = {entry["name"] for entry in list_outputs(stitched_tiff.parent)}
        assert names == {stitched_tiff.name, "stitched_broken.tif"}
