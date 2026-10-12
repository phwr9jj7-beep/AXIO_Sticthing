"""
Ghost (double-image) QC, the positions sidecar, the run manifest, bounded inspect output and
source-aware defaults.

The ghost fixture reproduces the Keyence failure in miniature: tiles cut from one textured
plane at the TRUE step, stitched once at that step and once at a step 4 % too short. The
short-step mosaic has no seam a gradient metric can find, only a blend of two shifted copies
in every x-overlap zone.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import tifffile
from scipy import ndimage as ndi

from axio_stitching import mcp_server
from axio_stitching.engine import StitchingEngine, summarize_inspect
from axio_stitching.models import StitchConfig
from axio_stitching.qc import qc_report

TW, TH = 512, 384
SX, SY = 360, 270
ROWS, COLS = 3, 4


def _tiles(root: Path) -> dict[tuple[int, int], str]:
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    h, w = (ROWS - 1) * SY + TH + 4, (COLS - 1) * SX + TW + 4
    plane = ndi.gaussian_filter(rng.random((h, w)), 1.5) * 30000 + 8000
    names = {}
    for r in range(ROWS):
        for c in range(COLS):
            y, x = r * SY, c * SX
            tile = plane[y:y + TH, x:x + TW] + rng.normal(0, 150, (TH, TW))   # camera noise
            fn = f"t_r{r}_c{c}.tif"
            tifffile.imwrite(str(root / fn), np.clip(tile, 0, 65535).astype(np.uint16))
            names[(r, c)] = fn
    return names


def _stitch(tmp: Path, names: dict, step_x: float, label: str) -> Path:
    js = tmp / f"positions_{label}.json"
    js.write_text(json.dumps({"tiles": [
        {"filename": fn, "x": c * step_x, "y": r * SY} for (r, c), fn in names.items()
    ]}), encoding="utf-8")
    cfg = StitchConfig(source=js, out_dir=tmp / f"out_{label}", correction="none", algorithm="coordinate")
    result = StitchingEngine(cfg).run()
    assert result.success, result.error_message
    return result.output_paths[0]


@pytest.fixture(scope="module")
def mosaics(tmp_path_factory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("ghost")
    names = _tiles(tmp)
    return {
        "good": _stitch(tmp, names, SX, "good"),
        "short": _stitch(tmp, names, SX * 0.96, "short"),   # 14.4 px ghost per overlap
    }


class TestGhost:
    def test_a_correct_mosaic_has_no_ghost(self, mosaics):
        m = qc_report(mosaics["good"]).metrics
        assert m["ghost_excess_x"] is not None
        assert m["ghost_excess_x"] < 0.05
        assert not any("double image" in f for f in qc_report(mosaics["good"]).findings)

    def test_a_short_step_is_detected_at_its_offset(self, mosaics):
        report = qc_report(mosaics["short"])
        m = report.metrics
        assert m["ghost_excess_x"] >= 0.15
        assert abs(m["ghost_lag_x"] - 14) <= 2
        assert any("double image in the x-overlap zones" in f for f in report.findings)

    def test_the_seam_metric_alone_does_not_see_it(self, mosaics):
        # Why the ghost test exists: blending hides the edge.
        assert qc_report(mosaics["short"]).metrics["seam_prominence_x"] < 6

    def test_without_a_layout_the_ghost_metrics_are_absent_and_explained(self, mosaics, tmp_path):
        copy = tmp_path / "stitched_scene0_coordinate.tif"
        copy.write_bytes(mosaics["short"].read_bytes())
        m = qc_report(copy).metrics
        assert m["ghost_excess_x"] is None
        assert "no tile layout" in m["ghost_note"]

    def test_an_explicit_layout_is_honoured(self, mosaics, tmp_path):
        copy = tmp_path / "old_mosaic.tif"
        copy.write_bytes(mosaics["short"].read_bytes())
        sidecar = mosaics["short"].with_name(mosaics["short"].stem + "_positions.json")
        m = qc_report(copy, positions=sidecar).metrics
        assert m["ghost_excess_x"] >= 0.15

    def test_mcp_tool_passes_positions(self, mosaics):
        payload = json.loads(mcp_server.axio_qc_report(str(mosaics["short"])))
        assert payload["metrics"]["ghost_excess_x"] >= 0.15


class TestProvenance:
    def test_sidecar_matches_the_canvas(self, mosaics):
        sidecar = json.loads(mosaics["good"].with_name(mosaics["good"].stem + "_positions.json")
                             .read_text(encoding="utf-8"))
        with tifffile.TiffFile(str(mosaics["good"])) as tif:
            h, w = tif.pages[0].shape[-2:]
        assert (sidecar["canvas"]["width"], sidecar["canvas"]["height"]) == (w, h)
        xs = sorted({t["x"] for t in sidecar["tiles"]})
        assert xs == [0, SX, 2 * SX, 3 * SX]

    def test_run_manifest_records_config_source_timings_and_outputs(self, mosaics):
        manifest = json.loads((mosaics["good"].parent / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["success"] is True
        assert manifest["tool"]["name"] == "axio-stitching"
        assert manifest["config"]["algorithm"] == "coordinate"
        assert manifest["source"]["source_type"] == "explicit"
        assert manifest["source_sha256"] and len(manifest["source_sha256"]) == 64
        assert set(manifest["stage_seconds"]) >= {"resolve", "alignment", "canvas"}
        assert manifest["outputs"][0]["positions_sidecar"].endswith("_positions.json")

    def test_a_failed_run_still_leaves_a_manifest(self, tmp_path):
        js = tmp_path / "p.json"
        js.write_text(json.dumps([{"filename": "missing.tif", "x": 0, "y": 0}]), encoding="utf-8")
        cfg = StitchConfig(source=js, out_dir=tmp_path / "out", correction="none", algorithm="coordinate",
                           tile_width=64, tile_height=64)
        result = StitchingEngine(cfg).run()
        manifest_path = tmp_path / "out" / "run_manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["success"] is result.success


class TestInspectSummary:
    def _payload(self, n: int) -> dict:
        return {"scenes": [{"scene_id": 0, "tiles": [
            {"filename": f"f{i}.tif", "x": (i % 50) * 100.0, "y": (i // 50) * 80.0, "w": 120, "h": 100}
            for i in range(n)]}]}

    def test_large_scenes_are_summarised(self):
        p = summarize_inspect(self._payload(3000))
        scene = p["scenes"][0]
        assert scene["tiles_truncated"] is True and scene["tiles_total"] == 3000
        assert len(scene["tiles"]) == 8
        assert scene["grid_estimate"] == {"columns": 50, "rows": 60}
        assert p["tiles_listing"] == "summary"
        assert len(json.dumps(p)) < 20_000

    def test_small_scenes_are_untouched(self):
        p = summarize_inspect(self._payload(20))
        assert len(p["scenes"][0]["tiles"]) == 20 and p["tiles_listing"] == "full"

    def test_mcp_inspect_summarises_and_can_return_everything(self, tmp_path):
        js = tmp_path / "big.json"
        js.write_text(json.dumps([{"filename": f"t{i}.tif", "x": i * 10, "y": 0} for i in range(500)]),
                      encoding="utf-8")
        small = json.loads(mcp_server.axio_inspect_dataset(str(js)))
        assert small["tiles_listing"] == "summary"
        full = json.loads(mcp_server.axio_inspect_dataset(str(js), summary_only=False))
        assert full["tiles_listing"] == "full" and len(full["scenes"][0]["tiles"]) == 500


class TestAutoDefaults:
    def test_keyence_defaults_to_no_correction_and_coordinates(self, tmp_path):
        from tests.test_stage_model import make_keyence_dataset

        bcf = make_keyence_dataset(tmp_path / "k")
        cfg = StitchConfig(source=bcf, out_dir=tmp_path / "o")
        assert cfg.correction.value == "none" and cfg.algorithm.value == "coordinate"
        assert "keyence" in cfg.resolved_defaults["algorithm"]

    def test_other_sources_keep_the_historical_defaults(self, info_xml_with_tiles, tmp_path):
        cfg = StitchConfig(source=info_xml_with_tiles, out_dir=tmp_path / "o")
        assert cfg.correction.value == "basicpy" and cfg.algorithm.value == "phase"

    def test_explicit_values_win(self, tmp_path):
        from tests.test_stage_model import make_keyence_dataset

        bcf = make_keyence_dataset(tmp_path / "k2")
        cfg = StitchConfig(source=bcf, out_dir=tmp_path / "o", correction="median", algorithm="phase")
        assert cfg.correction.value == "median" and cfg.algorithm.value == "phase"
        assert cfg.resolved_defaults == {}

    def test_auto_is_accepted_explicitly(self, info_xml_with_tiles, tmp_path):
        cfg = StitchConfig(source=info_xml_with_tiles, out_dir=tmp_path / "o",
                           correction="auto", algorithm="auto")
        assert cfg.correction.value == "basicpy"

    def test_cli_passes_keyence_step(self, tmp_path):
        from typer.testing import CliRunner

        from axio_stitching.cli import app
        from tests.test_stage_model import EDGE_SX, make_keyence_dataset

        bcf = make_keyence_dataset(tmp_path / "k3")
        res = CliRunner().invoke(app, ["inspect", "--source", str(bcf), "--keyence-step", "edgepoints", "--json"])
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["stage_model"]["method"] == "edgepoints"
        assert payload["stage_model"]["step_x_px"] == pytest.approx(EDGE_SX, abs=0.01)
