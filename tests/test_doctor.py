"""
test_doctor.py — environment diagnostics.

The doctor is the first thing an agent calls, so its own failure modes matter: it must never
raise, must produce an actionable `fix` for anything it flags, and must be honest about a
value it could not determine rather than inventing one.
"""

from pathlib import Path

from axio_stitching.doctor import (
    FAIL,
    OK,
    OPTIONAL_PACKAGES,
    REQUIRED_PACKAGES,
    WARN,
    detect_mcp_flavour,
    disk_free_bytes,
    human_bytes,
    memory_bytes,
    run_doctor,
)


class TestHumanBytes:
    def test_formats_each_magnitude(self):
        assert human_bytes(512) == "512 B"
        assert human_bytes(1536).endswith("KB")
        assert human_bytes(5 * 1024**3).endswith("GB")

    def test_says_unknown_rather_than_guessing(self):
        assert human_bytes(None) == "unknown"


class TestMemoryBytes:
    def test_returns_a_plausible_total(self):
        total, available = memory_bytes()
        assert total is None or total > 256 * 1024**2
        assert available is None or available <= (total or available)


class TestDiskFreeBytes:
    def test_reads_an_existing_directory(self, tmp_path: Path):
        assert (disk_free_bytes(tmp_path) or 0) > 0

    def test_walks_up_to_the_nearest_existing_parent(self, tmp_path: Path):
        assert (disk_free_bytes(tmp_path / "not" / "yet" / "created") or 0) > 0


class TestDetectMcpFlavour:
    def test_reports_a_usable_api_or_says_so(self):
        flavour = detect_mcp_flavour()
        assert set(flavour) == {"available", "api", "version"}
        if flavour["available"]:
            assert "MCPServer" in flavour["api"] or "FastMCP" in flavour["api"]


class TestRunDoctor:
    def test_covers_every_declared_package(self, tmp_path: Path):
        names = {c.name for c in run_doctor(tmp_path).checks}
        for _import_name, pip_name, _why in REQUIRED_PACKAGES:
            assert f"package:{pip_name}" in names
        for _import_name, pip_name, _why in OPTIONAL_PACKAGES:
            assert f"optional:{pip_name}" in names

    def test_every_check_has_a_known_status(self, tmp_path: Path):
        assert all(c.status in {OK, WARN, FAIL} for c in run_doctor(tmp_path).checks)

    def test_anything_flagged_carries_an_actionable_fix(self, tmp_path: Path):
        for check in run_doctor(tmp_path).checks:
            if check.status == FAIL:
                assert check.fix, f"{check.name} failed without telling the user what to do"

    def test_ok_is_false_only_when_something_failed(self, tmp_path: Path):
        report = run_doctor(tmp_path)
        assert report.ok == (not report.failures)

    def test_checks_the_output_directory_that_was_asked_about(self, tmp_path: Path):
        target = tmp_path / "chosen-out"
        report = run_doctor(target)
        assert report.info["out_dir"] == str(target)
        assert any(c.name == "output-writable" and c.status == OK for c in report.checks)

    def test_creates_the_output_directory_it_probes(self, tmp_path: Path):
        target = tmp_path / "made-by-doctor"
        run_doctor(target)
        assert target.exists()

    def test_reports_an_unwritable_output_directory_as_a_failure(self, tmp_path: Path):
        # A path whose parent is a FILE cannot be turned into a directory on any platform.
        blocker = tmp_path / "a-file"
        blocker.write_text("x", encoding="utf-8")
        report = run_doctor(blocker / "child")
        writable = next(c for c in report.checks if c.name == "output-writable")
        assert writable.status == FAIL and writable.fix
        assert not report.ok

    def test_summary_is_a_sentence_not_a_dump(self, tmp_path: Path):
        assert len(run_doctor(tmp_path).summary()) < 80

    def test_to_dict_is_json_shaped(self, tmp_path: Path):
        payload = run_doctor(tmp_path).to_dict()
        assert set(payload) == {"ok", "summary", "checks", "info"}
        assert all(set(c) == {"name", "status", "detail", "fix"} for c in payload["checks"])
