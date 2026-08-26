"""
test_agent_runner.py — the filesystem side of the agent integration.

The runner writes into directories that belong to the user's agent apps, so these tests pin
down the safety contract end to end: detection, transactional install, idempotency, drift
reporting, refusal to overwrite a foreign directory, and an uninstall that removes only what
was written and leaves no husk behind.

Every test runs against a SANDBOX home; nothing here touches the developer's real
``~/.claude`` or ``~/.codex``.
"""

import json
from pathlib import Path

import pytest

from axio_stitching.agent_integration import (
    AGENT_TARGETS,
    MANAGED_BY,
    MANAGED_SIDECAR,
    PLUGIN_DIR_NAME,
    SKILL_DIR_NAME,
)
from axio_stitching.agent_runner import (
    build_plan,
    detect_all,
    detect_target,
    install,
    install_all,
    is_foreign_dir,
    make_ctx,
    read_sidecar,
    status,
    status_all,
    uninstall,
    uninstall_all,
)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A sandbox home with every agent app's root present, so all targets are detected."""
    root = tmp_path / "home"
    for relative in (".claude", ".codex", ".gemini/config", "AppData/Roaming/Claude"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def ctx(home: Path):
    """
    A HOST-NATIVE context: the runner does real filesystem I/O, so its platform must match
    the OS running the tests (backslash-joined plan paths are literal filename characters on
    POSIX). Windows path COMPUTATION is covered cross-OS by test_agent_integration's injected
    pure-path contexts; CLAUDE_DESKTOP_CONFIG pins the desktop config to one sandbox path so
    these behavioural tests are identical on every OS.
    """
    import sys
    return make_ctx(
        home=str(home),
        env={
            "APPDATA": str(home / "AppData" / "Roaming"),
            "CLAUDE_DESKTOP_CONFIG": str(home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"),
        },
        platform=sys.platform,
        exec_path=r"C:\proj\.venv\Scripts\python.exe",
        exec_args=("-m", "axio_stitching.mcp_server"),
        app_path=r"C:\proj\dist\AXIO_Stitching_Studio.exe",
    )


class TestDetection:
    def test_detects_every_platform_when_its_root_exists(self, ctx):
        detections = {d.target: d for d in detect_all(ctx)}
        assert set(detections) == set(AGENT_TARGETS)
        assert all(d.installed for d in detections.values())

    def test_evidence_names_what_was_found(self, ctx, home: Path):
        evidence = detect_target("claude-code", ctx).evidence
        assert any(str(home / ".claude") in e for e in evidence)

    def test_reports_absence_for_a_missing_root(self, tmp_path: Path):
        empty = make_ctx(home=str(tmp_path / "nothing"), env={})
        assert not detect_target("codex", empty).installed

    def test_an_unknown_target_is_rejected(self, ctx):
        with pytest.raises(ValueError):
            detect_target("emacs", ctx)


class TestInstall:
    def test_installs_every_detected_target(self, ctx):
        results = install_all(ctx)
        assert all(r.ok for r in results), [r.error for r in results if not r.ok]
        assert all(r.changed for r in results)

    def test_claude_code_drops_a_skill_and_a_plugin(self, ctx, home: Path):
        assert install(ctx, "claude-code").ok
        skills = home / ".claude" / "skills"
        assert (skills / SKILL_DIR_NAME / "SKILL.md").exists()
        assert (skills / SKILL_DIR_NAME / "references" / "parameters.md").exists()
        assert (skills / PLUGIN_DIR_NAME / ".claude-plugin" / "plugin.json").exists()
        assert (skills / PLUGIN_DIR_NAME / ".mcp.json").exists()

    def test_codex_drops_a_skill_and_owns_one_toml_table(self, ctx, home: Path):
        assert install(ctx, "codex").ok
        assert (home / ".codex" / "skills" / SKILL_DIR_NAME / "SKILL.md").exists()
        assert "[mcp_servers.axio-stitching]" in (home / ".codex" / "config.toml").read_text(encoding="utf-8")

    def test_antigravity_drops_a_plugin_and_owns_one_json_key(self, ctx, home: Path):
        assert install(ctx, "antigravity").ok
        plugin = home / ".gemini" / "config" / "plugins" / PLUGIN_DIR_NAME
        assert (plugin / "plugin.json").exists()
        assert (plugin / "skills" / SKILL_DIR_NAME / "SKILL.md").exists()
        registry = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8"))
        assert "axio-stitching" in registry["mcpServers"]

    def test_claude_desktop_only_touches_its_config(self, ctx, home: Path):
        assert install(ctx, "claude-desktop").ok
        config = home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        assert "axio-stitching" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]

    def test_writes_a_sidecar_with_file_hashes(self, ctx, home: Path):
        install(ctx, "claude-code")
        sidecar = read_sidecar(str(home / ".claude" / "skills" / SKILL_DIR_NAME))
        assert sidecar is not None
        assert sidecar["managedBy"] == MANAGED_BY
        assert {f["rel"] for f in sidecar["files"]} == {"SKILL.md", "references/parameters.md"}
        assert all(len(f["sha256"]) == 64 for f in sidecar["files"])

    def test_is_idempotent(self, ctx):
        install_all(ctx)
        second = install_all(ctx)
        assert all(r.ok and not r.changed for r in second), [
            (r.target, r.changed, r.error) for r in second
        ]

    def test_dry_run_writes_nothing(self, ctx, home: Path):
        results = install_all(ctx, dry_run=True)
        assert all(r.ok and r.changed for r in results)
        assert not (home / ".claude" / "skills").exists()
        assert not (home / ".codex" / "config.toml").exists()

    def test_dry_run_reports_no_change_when_already_installed(self, ctx):
        install_all(ctx)
        assert all(not r.changed for r in install_all(ctx, dry_run=True))

    def test_skips_an_undetected_platform_rather_than_creating_its_config(self, tmp_path: Path):
        home = tmp_path / "bare"
        (home / ".claude").mkdir(parents=True)
        bare = make_ctx(home=str(home), env={})
        results = {r.target: r for r in install_all(bare)}
        assert results["claude-code"].changed
        assert results["codex"].skipped
        assert not (home / ".codex").exists(), "a config dir must not be created for a missing app"

    def test_an_explicit_target_installs_even_when_undetected(self, tmp_path: Path):
        home = tmp_path / "bare"
        home.mkdir()
        bare = make_ctx(home=str(home), env={})
        assert install_all(bare, targets=["codex"])[0].ok
        assert (home / ".codex" / "config.toml").exists()

    def test_refuses_a_foreign_directory(self, ctx, home: Path):
        hand_made = home / ".claude" / "skills" / SKILL_DIR_NAME
        hand_made.mkdir(parents=True)
        (hand_made / "SKILL.md").write_text("my own skill", encoding="utf-8")

        result = install(ctx, "claude-code")
        assert not result.ok and "not created by AXIO" in (result.error or "")
        assert (hand_made / "SKILL.md").read_text(encoding="utf-8") == "my own skill"

    def test_force_overrides_a_foreign_directory(self, ctx, home: Path):
        hand_made = home / ".claude" / "skills" / SKILL_DIR_NAME
        hand_made.mkdir(parents=True)
        (hand_made / "SKILL.md").write_text("my own skill", encoding="utf-8")

        assert install(ctx, "claude-code", force=True).ok
        assert "axio_doctor" in (hand_made / "SKILL.md").read_text(encoding="utf-8")

    def test_an_unknown_target_returns_an_error_rather_than_raising(self, ctx):
        result = install(ctx, "emacs")
        assert not result.ok and "unknown agent target" in (result.error or "")


class TestIsForeignDir:
    def test_absent_is_not_foreign(self, tmp_path: Path):
        assert not is_foreign_dir(str(tmp_path / "nope"))

    def test_a_directory_without_our_sidecar_is_foreign(self, tmp_path: Path):
        (tmp_path / "d").mkdir()
        assert is_foreign_dir(str(tmp_path / "d"))

    def test_a_sidecar_from_another_tool_is_foreign(self, tmp_path: Path):
        target = tmp_path / "d"
        target.mkdir()
        (target / MANAGED_SIDECAR).write_text(json.dumps({"managedBy": "someone-else"}), encoding="utf-8")
        assert is_foreign_dir(str(target))


class TestStatus:
    def test_reports_absent_before_install(self, ctx):
        assert all(r.state == "absent" for r in status_all(ctx))

    def test_reports_installed_after_install(self, ctx):
        install_all(ctx)
        assert all(r.state == "installed" for r in status_all(ctx)), [
            (r.target, r.state) for r in status_all(ctx)
        ]

    def test_detects_an_edited_managed_file(self, ctx, home: Path):
        install(ctx, "claude-code")
        skill = home / ".claude" / "skills" / SKILL_DIR_NAME / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nmy note\n", encoding="utf-8")

        report = status(ctx, "claude-code")
        assert report.state == "drifted"
        assert any("SKILL.md: edited" in u.detail for u in report.units)

    def test_detects_a_deleted_managed_file(self, ctx, home: Path):
        install(ctx, "claude-code")
        (home / ".claude" / "skills" / SKILL_DIR_NAME / "SKILL.md").unlink()
        assert any("removed" in u.detail for u in status(ctx, "claude-code").units)

    def test_detects_an_edited_config_value(self, ctx, home: Path):
        install(ctx, "claude-desktop")
        config = home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        data = json.loads(config.read_text(encoding="utf-8"))
        data["mcpServers"]["axio-stitching"]["env"]["MY_VAR"] = "1"
        config.write_text(json.dumps(data), encoding="utf-8")

        report = status(ctx, "claude-desktop")
        assert report.state == "drifted"
        assert report.keys[0].state == "drifted"

    def test_reports_a_foreign_directory(self, ctx, home: Path):
        hand_made = home / ".claude" / "skills" / SKILL_DIR_NAME
        hand_made.mkdir(parents=True)
        (hand_made / "SKILL.md").write_text("mine", encoding="utf-8")
        assert status(ctx, "claude-code").state == "foreign"


class TestUninstall:
    def test_leaves_no_trace_after_a_clean_cycle(self, ctx, home: Path):
        install_all(ctx)
        results = uninstall_all(ctx)
        assert all(r.ok for r in results), [r.error for r in results if not r.ok]
        assert all(not r.kept for r in results), [r.kept for r in results if r.kept]

        leftovers = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file())
        assert leftovers == [], leftovers

    def test_keeps_a_file_the_user_edited_and_reports_it(self, ctx, home: Path):
        install(ctx, "claude-code")
        skill = home / ".claude" / "skills" / SKILL_DIR_NAME / "SKILL.md"
        skill.write_text("I rewrote this", encoding="utf-8")

        result = uninstall(ctx, "claude-code")
        assert result.ok
        assert any("edited since install" in k for k in result.kept)
        assert skill.read_text(encoding="utf-8") == "I rewrote this"

    def test_keeps_a_config_value_the_user_edited(self, ctx, home: Path):
        install(ctx, "claude-desktop")
        config = home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        data = json.loads(config.read_text(encoding="utf-8"))
        data["mcpServers"]["axio-stitching"]["env"]["MY_VAR"] = "1"
        config.write_text(json.dumps(data), encoding="utf-8")

        result = uninstall(ctx, "claude-desktop")
        assert any("edited since install" in k for k in result.kept)
        assert "axio-stitching" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]

    def test_never_touches_a_directory_that_is_not_ours(self, ctx, home: Path):
        hand_made = home / ".claude" / "skills" / SKILL_DIR_NAME
        hand_made.mkdir(parents=True)
        (hand_made / "SKILL.md").write_text("mine", encoding="utf-8")

        result = uninstall(ctx, "claude-code")
        assert result.ok
        assert any("not ours" in k for k in result.kept)
        assert (hand_made / "SKILL.md").exists()

    def test_preserves_other_tools_entries_in_shared_configs(self, ctx, home: Path):
        codex_config = home / ".codex" / "config.toml"
        codex_config.write_text(
            '# keep me\n[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8"
        )
        registry = home / ".gemini" / "config" / "mcp_config.json"
        registry.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")

        install_all(ctx)
        uninstall_all(ctx)

        assert codex_config.read_text(encoding="utf-8") == '# keep me\n[mcp_servers.other]\ncommand = "x"\n'
        assert json.loads(registry.read_text(encoding="utf-8")) == {"mcpServers": {"other": {"command": "x"}}}

    def test_uninstall_then_install_is_a_working_cycle(self, ctx, home: Path):
        install_all(ctx)
        uninstall_all(ctx)
        second = install_all(ctx, targets=list(AGENT_TARGETS))
        assert all(r.ok and r.changed for r in second), [(r.target, r.error) for r in second]

    def test_dry_run_removes_nothing(self, ctx, home: Path):
        install(ctx, "claude-code")
        result = uninstall(ctx, "claude-code", dry_run=True)
        assert result.ok and result.removed
        assert (home / ".claude" / "skills" / SKILL_DIR_NAME / "SKILL.md").exists()

    def test_uninstalling_nothing_is_not_an_error(self, ctx):
        assert all(r.ok for r in uninstall_all(ctx))


class TestBuildPlan:
    def test_renders_the_skill_and_reads_the_shipped_reference(self, ctx):
        plan = build_plan(ctx, "claude-code")
        skill_file = next(
            f for u in plan.units for f in u.files if f.rel == "SKILL.md"
        )
        assert "axio_doctor" in skill_file.content
        reference = next(
            f for u in plan.units for f in u.files if f.rel.endswith("parameters.md")
        )
        assert len(reference.content) > 200
