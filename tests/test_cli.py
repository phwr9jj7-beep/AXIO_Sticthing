"""test_cli.py — CLI integration tests using Typer's CliRunner."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from axio_stitching.cli import app

runner = CliRunner()


class TestVersionCommand:
    def test_version_exits_zero(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0

    def test_version_json_output(self):
        result = runner.invoke(app, ["version", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "axio_stitching" in data
        assert "python" in data
        assert "dependencies" in data

    def test_version_contains_package_version(self):
        from axio_stitching import __version__
        result = runner.invoke(app, ["version", "--json"])
        data = json.loads(result.output)
        assert data["axio_stitching"] == __version__


class TestInspectCommand:
    def test_inspect_valid_xml_json(self, info_xml_path):
        result = runner.invoke(app, ["inspect", "--xml", str(info_xml_path), "--json"])
        assert result.exit_code == 0, result.output
        # strict parsing on purpose: --json must be byte-exact for another program to
        # consume, so a stray control character from a wrapping renderer is a real defect.
        data = json.loads(result.output)
        assert "scenes" in data
        assert data["total_scenes"] == 2

    def test_inspect_nonexistent_xml(self, tmp_path):
        result = runner.invoke(app, ["inspect", "--xml", str(tmp_path / "no.xml"), "--json"])
        assert result.exit_code != 0

    def test_inspect_rich_output(self, info_xml_path):
        """Rich mode should not crash."""
        result = runner.invoke(app, ["inspect", "--xml", str(info_xml_path)])
        assert result.exit_code == 0, result.output


class TestValidateCommand:
    def test_validate_valid_config(self, info_xml_path, tmp_path):
        result = runner.invoke(app, [
            "validate", "--xml", str(info_xml_path),
            "--out-dir", str(tmp_path / "output"),
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "valid" in data
        assert "errors" in data

    def test_validate_missing_xml_reports_error(self, tmp_path):
        result = runner.invoke(app, [
            "validate", "--xml", str(tmp_path / "missing.xml"),
            "--out-dir", str(tmp_path), "--json",
        ])
        # A missing XML is rejected by the config model before the engine sees it, so the
        # command reports valid=False and exits non-zero.
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["valid"] is False
        assert data["errors"]


class TestStitchHelp:
    def test_stitch_help_exits_zero(self):
        result = runner.invoke(app, ["stitch", "--help"])
        assert result.exit_code == 0
        assert "--xml" in result.output
        assert "--algorithm" in result.output


class TestDoctorCommand:
    def test_json_report_is_well_shaped(self, tmp_path):
        result = runner.invoke(app, ["doctor", "--out-dir", str(tmp_path), "--json"])
        data = json.loads(result.output)
        assert set(data) == {"ok", "summary", "checks", "info"}
        assert result.exit_code == (0 if data["ok"] else 1)

    def test_rich_output_does_not_crash(self, tmp_path):
        result = runner.invoke(app, ["doctor", "--out-dir", str(tmp_path)])
        assert result.exit_code in (0, 1), result.output
        assert "environment" in result.output


class TestEstimateCommand:
    def test_reports_a_verdict(self, small_dataset, tmp_path):
        result = runner.invoke(app, [
            "estimate", "--xml", str(small_dataset), "--out-dir", str(tmp_path / "out"),
            "--correction", "none", "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["verdict"] in {"ok", "tight", "will_not_fit"}
        assert data["scenes"][0]["canvas_width"] == 1920

    def test_rich_output_does_not_crash(self, small_dataset, tmp_path):
        result = runner.invoke(app, [
            "estimate", "--xml", str(small_dataset), "--out-dir", str(tmp_path / "out"),
            "--correction", "none",
        ])
        assert result.exit_code == 0, result.output
        assert "Verdict" in result.output

    def test_exits_non_zero_when_the_job_will_not_fit(self, tmp_path):
        empty = tmp_path / "broken_info.xml"
        empty.write_text("<ZoomImageDocument></ZoomImageDocument>", encoding="utf-8")
        result = runner.invoke(app, [
            "estimate", "--xml", str(empty), "--out-dir", str(tmp_path / "out"), "--json",
        ])
        assert result.exit_code == 1


class TestQcCommand:
    def test_measures_a_mosaic(self, stitched_tiff):
        result = runner.invoke(app, ["qc", str(stitched_tiff), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True and data["metrics"]["mean"] > 0

    def test_reports_a_broken_file_and_exits_non_zero(self, tmp_path):
        junk = tmp_path / "junk.tif"
        junk.write_bytes(b"nope")
        assert runner.invoke(app, ["qc", str(junk), "--json"]).exit_code == 1

    def test_rich_output_lists_findings(self, seamed_tiff):
        result = runner.invoke(app, ["qc", str(seamed_tiff)])
        assert result.exit_code == 0, result.output
        assert "Findings" in result.output


class TestOutputsCommand:
    def test_lists_stitched_files(self, stitched_tiff):
        result = runner.invoke(app, ["outputs", str(stitched_tiff.parent), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert [entry["name"] for entry in data["outputs"]] == [stitched_tiff.name]

    def test_says_so_when_there_is_nothing(self, tmp_path):
        result = runner.invoke(app, ["outputs", str(tmp_path)])
        assert result.exit_code == 0
        assert "No stitched" in result.output


class TestAgentCommands:
    def test_status_reports_every_target(self):
        from axio_stitching.agent_integration import AGENT_TARGETS

        result = runner.invoke(app, ["agent", "status", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert {t["target"] for t in data["targets"]} == set(AGENT_TARGETS)

    def test_status_rich_output_does_not_crash(self):
        result = runner.invoke(app, ["agent", "status"])
        assert result.exit_code == 0, result.output

    def test_an_unknown_target_is_rejected_before_anything_is_written(self):
        result = runner.invoke(app, ["agent", "install", "--target", "emacs"])
        assert result.exit_code == 2
        assert "Unknown target" in result.output

    def test_install_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        result = runner.invoke(app, ["agent", "install", "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert any(r["changed"] for r in data["results"])
        assert not (home / ".claude" / "skills").exists()

    def test_install_then_uninstall_is_a_clean_cycle(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        installed = runner.invoke(app, ["agent", "install", "--target", "claude-code", "--json"])
        assert installed.exit_code == 0, installed.output
        assert (home / ".claude" / "skills" / "axio-stitching-pipeline" / "SKILL.md").exists()

        removed = runner.invoke(app, ["agent", "uninstall", "--target", "claude-code", "--json"])
        assert removed.exit_code == 0, removed.output
        assert not (home / ".claude" / "skills" / "axio-stitching-pipeline").exists()
