"""
test_mcp_server.py — the MCP tool surface.

Two classes of thing are checked here. First, the *contract*: every tool is registered,
described, and returns JSON (or an image) rather than raising — an MCP tool that throws is a
broken tool provider, not an error message. Second, the *behaviour* of each tool against a
real synthetic dataset.

The SDK renamed its high-level server class between 1.x and 2.0, so the tools are invoked
through their undecorated functions, which both APIs expose the same way.
"""

import asyncio
import json
from pathlib import Path

import pytest

from axio_stitching import mcp_server
from axio_stitching.mcp_server import mcp

#: The tools the skill and the server instructions promise. Losing one silently would break
#: the documented workflow, so the list is pinned.
EXPECTED_TOOLS = {
    "axio_doctor",
    "axio_list_algorithms",
    "axio_inspect_dataset",
    "axio_estimate_stitch",
    "axio_validate_stitch",
    "axio_start_stitch",
    "axio_job_status",
    "axio_job_result",
    "axio_list_jobs",
    "axio_cancel_job",
    "axio_stitch_sync",
    "axio_read_preview",
    "axio_qc_report",
    "axio_list_outputs",
    "axio_launch_gui",
    "axio_agent_status",
}


def call(tool, **kwargs):
    """Invoke a registered tool through its plain function, on either SDK generation."""
    return getattr(tool, "fn", tool)(**kwargs)


def call_json(tool, **kwargs) -> dict:
    return json.loads(call(tool, **kwargs))


@pytest.fixture(scope="module")
def registered_tools():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


class TestRegistration:
    def test_every_documented_tool_is_registered(self, registered_tools):
        assert EXPECTED_TOOLS <= set(registered_tools), EXPECTED_TOOLS - set(registered_tools)

    def test_no_tool_ships_without_a_description(self, registered_tools):
        for name, tool in registered_tools.items():
            assert (tool.description or "").strip(), f"{name} has no description"

    def test_descriptions_say_what_the_tool_returns(self, registered_tools):
        # An agent picks tools from these strings; a description that stops at "does X"
        # forces a speculative call to find out the shape of the answer.
        for name in EXPECTED_TOOLS - {"axio_read_preview"}:
            assert "Returns:" in registered_tools[name].description, name

    def test_the_sdk_binding_was_resolved(self):
        assert mcp_server._SDK_API in {"MCPServer", "FastMCP"}

    def test_server_instructions_teach_the_workflow(self):
        for step in ("axio_doctor", "axio_inspect_dataset", "axio_estimate_stitch", "axio_start_stitch"):
            assert step in mcp_server.INSTRUCTIONS


class TestVocabularyTools:
    def test_list_algorithms_matches_the_models(self):
        from axio_stitching.models import AlignmentMode, CorrectionMethod, StitchAlgorithm, ZMode

        payload = call_json(mcp_server.axio_list_algorithms)
        assert payload["corrections"] == [m.value for m in CorrectionMethod]
        assert payload["algorithms"] == [m.value for m in StitchAlgorithm]
        assert payload["alignment_modes"] == [m.value for m in AlignmentMode]
        assert payload["z_modes"] == [m.value for m in ZMode]

    def test_every_legal_value_carries_guidance(self):
        payload = call_json(mcp_server.axio_list_algorithms)
        for field in ("correction", "algorithm", "alignment_mode", "z_mode"):
            plural = {"correction": "corrections", "algorithm": "algorithms",
                      "alignment_mode": "alignment_modes", "z_mode": "z_modes"}[field]
            assert set(payload["guidance"][field]) == set(payload[plural]), field

    def test_doctor_returns_a_report(self, tmp_path: Path):
        payload = call_json(mcp_server.axio_doctor, out_dir=str(tmp_path))
        assert set(payload) == {"ok", "summary", "checks", "info"}


class TestInspect:
    def test_reports_scenes_tiles_and_real_tile_geometry(self, small_dataset: Path):
        payload = call_json(mcp_server.axio_inspect_dataset, xml_path=str(small_dataset))
        assert payload["total_scenes"] == 1
        assert payload["total_tiles"] == 4
        geometry = payload["tile_geometry"]
        assert geometry["tile_height"] == 1020 and geometry["tile_width"] == 1020
        assert geometry["channels_per_file"] == 1

    def test_says_so_when_the_tiles_are_not_beside_the_xml(self, info_xml_path: Path):
        payload = call_json(mcp_server.axio_inspect_dataset, xml_path=str(info_xml_path))
        assert "error" in payload["tile_geometry"]

    def test_a_missing_file_returns_an_error_not_an_exception(self, tmp_path: Path):
        payload = call_json(mcp_server.axio_inspect_dataset, xml_path=str(tmp_path / "nope.xml"))
        assert payload["ok"] is False and payload["error"]


class TestEstimateAndValidate:
    def test_estimate_returns_a_verdict(self, small_dataset: Path, tmp_path: Path):
        payload = call_json(
            mcp_server.axio_estimate_stitch,
            xml_path=str(small_dataset), out_dir=str(tmp_path / "out"), correction="none",
        )
        assert payload["verdict"] in {"ok", "tight", "will_not_fit"}
        assert payload["totals"]["peak_ram_bytes"] > 0

    def test_estimate_rejects_an_illegal_value_with_a_message(self, small_dataset: Path, tmp_path: Path):
        payload = call_json(
            mcp_server.axio_estimate_stitch,
            xml_path=str(small_dataset), out_dir=str(tmp_path / "out"), correction="magic",
        )
        assert payload["ok"] is False and "magic" in payload["error"]

    def test_validate_passes_a_good_config(self, small_dataset: Path, tmp_path: Path):
        payload = call_json(
            mcp_server.axio_validate_stitch,
            xml_path=str(small_dataset), out_dir=str(tmp_path / "out"),
            correction="none", algorithm="phase",
        )
        assert payload["valid"] is True and payload["errors"] == []

    def test_validate_flags_a_scene_that_does_not_exist(self, small_dataset: Path, tmp_path: Path):
        payload = call_json(
            mcp_server.axio_validate_stitch,
            xml_path=str(small_dataset), out_dir=str(tmp_path / "out"),
            correction="none", algorithm="phase", scene=42,
        )
        assert payload["valid"] is False
        assert any("42" in e for e in payload["errors"])

    def test_validate_warns_about_missing_tiles(self, info_xml_path: Path, tmp_path: Path):
        payload = call_json(
            mcp_server.axio_validate_stitch,
            xml_path=str(info_xml_path), out_dir=str(tmp_path / "out"),
            correction="none", algorithm="phase",
        )
        assert any("missing" in w for w in payload["warnings"])


class TestStitchAndInspectResult:
    @pytest.fixture()
    def stitched(self, small_dataset: Path, tmp_path: Path):
        """Run one real stitch; the tools that consume its output are tested against it."""
        payload = call_json(
            mcp_server.axio_stitch_sync,
            xml_path=str(small_dataset), out_dir=str(tmp_path / "synced"),
            correction="none", algorithm="coordinate", scene=0,
        )
        assert payload["success"], payload.get("error_message")
        return payload

    def test_sync_stitch_produces_output_and_a_preview(self, stitched):
        assert stitched["output_paths"] and Path(stitched["output_paths"][0]).exists()
        assert stitched["preview_paths"] and Path(stitched["preview_paths"][0]).exists()
        assert stitched["scenes_processed"] == 1
        assert stitched["tiles_processed"] == 4

    def test_qc_measures_the_result(self, stitched):
        payload = call_json(mcp_server.axio_qc_report, path=stitched["output_paths"][0])
        assert payload["ok"] is True
        assert payload["metrics"]["empty_fraction"] < 0.5

    def test_list_outputs_finds_the_result(self, stitched):
        directory = str(Path(stitched["output_paths"][0]).parent)
        payload = call_json(mcp_server.axio_list_outputs, directory=directory)
        assert [entry["name"] for entry in payload["outputs"]] == [
            Path(stitched["output_paths"][0]).name
        ]

    def test_read_preview_returns_an_image(self, stitched):
        result = call(mcp_server.axio_read_preview, path=stitched["output_paths"][0])
        assert not isinstance(result, str), "a real preview must come back as an image"
        assert hasattr(result, "to_image_content")

    def test_read_preview_accepts_the_png_directly(self, stitched):
        result = call(mcp_server.axio_read_preview, path=stitched["preview_paths"][0])
        assert hasattr(result, "to_image_content")

    def test_read_preview_explains_a_missing_preview(self, tmp_path: Path):
        payload = json.loads(call(mcp_server.axio_read_preview, path=str(tmp_path / "nope.tif")))
        assert payload["ok"] is False and "no preview image" in payload["error"]
        assert "hint" in payload

    def test_sync_stitch_reports_a_bad_config_as_a_failed_result(self, tmp_path: Path):
        payload = call_json(
            mcp_server.axio_stitch_sync,
            xml_path=str(tmp_path / "nope.xml"), out_dir=str(tmp_path / "out"),
        )
        assert payload["success"] is False and payload["error_message"]


class TestJobTools:
    def test_start_poll_and_collect(self, small_dataset: Path, tmp_path: Path):
        started = call_json(
            mcp_server.axio_start_stitch,
            xml_path=str(small_dataset), out_dir=str(tmp_path / "bg"),
            correction="none", algorithm="coordinate", scene=0,
        )
        job_id = started["job_id"]
        assert "poll axio_job_status" in started["next"]

        from tests.test_jobs import wait_for

        assert wait_for(
            lambda: call_json(mcp_server.axio_job_status, job_id=job_id)["state"] != "running"
        )
        status = call_json(mcp_server.axio_job_status, job_id=job_id)
        assert status["state"] == "succeeded", status.get("error")

        result = call_json(mcp_server.axio_job_result, job_id=job_id)
        assert result["result"]["success"] is True

        listing = call_json(mcp_server.axio_list_jobs)
        assert any(j["job_id"] == job_id for j in listing["jobs"])

    def test_status_of_an_unknown_job_is_reported_not_raised(self):
        assert call_json(mcp_server.axio_job_status, job_id="nope")["state"] == "unknown"

    def test_cancelling_an_unknown_job_is_reported_not_raised(self):
        assert call_json(mcp_server.axio_cancel_job, job_id="nope")["cancelled"] is False

    def test_start_rejects_an_illegal_value(self, small_dataset: Path, tmp_path: Path):
        payload = call_json(
            mcp_server.axio_start_stitch,
            xml_path=str(small_dataset), out_dir=str(tmp_path / "bg"), algorithm="telepathy",
        )
        assert payload["ok"] is False


class TestAgentTools:
    def test_agent_status_reports_every_target(self):
        from axio_stitching.agent_integration import AGENT_TARGETS

        payload = call_json(mcp_server.axio_agent_status)
        assert {t["target"] for t in payload["targets"]} == set(AGENT_TARGETS)

    def test_launch_gui_explains_itself_when_the_app_is_missing(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("AXIO_STITCHING_APP", raising=False)
        monkeypatch.setattr("axio_stitching.agent_runner.find_app_path", lambda: None)
        payload = call_json(mcp_server.axio_launch_gui, out_dir=str(tmp_path))
        assert payload["launched"] is False
        assert "AXIO_STITCHING_APP" in payload["fix"]


class TestStdoutIsReservedForTheProtocol:
    """
    When the pipeline runs as an MCP stdio server, stdout IS the JSON-RPC transport. Any
    library `print()` that reaches it corrupts the frame stream - the client logs "Failed to
    parse JSONRPC message from server" and, depending on the client, drops the response.

    This has already happened once, so it is pinned here: running a real stitch must leave
    stdout completely silent.
    """

    def test_a_full_stitch_writes_nothing_to_stdout(self, small_dataset: Path, tmp_path: Path, capsys):
        payload = call_json(
            mcp_server.axio_stitch_sync,
            xml_path=str(small_dataset), out_dir=str(tmp_path / "quiet"),
            correction="none", algorithm="coordinate", scene=0,
        )
        assert payload["success"], payload.get("error_message")
        captured = capsys.readouterr()
        assert captured.out == "", f"stdout must stay clean; got: {captured.out[:300]!r}"

    def test_the_engines_default_progress_goes_to_stderr(self, capsys):
        from axio_stitching.engine import _default_progress
        from axio_stitching.models import PipelineStage, ProgressEvent

        _default_progress(ProgressEvent(percent=42, status_message="halfway", stage=PipelineStage.CANVAS))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "[PROGRESS] 42" in captured.err

    def test_canvas_and_correction_diagnostics_go_to_stderr(self, capsys):
        from axio_stitching import canvas, corrections, stitchers

        for module in (canvas, corrections, stitchers):
            module._log("diagnostic line")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.count("diagnostic line") == 3
