"""
test_agent_integration.py — the PURE core of the agent integration.

Every branch here is exercised on BOTH path flavours regardless of the OS running the tests,
because the context injects the path module. That matters: the Windows paths are the ones
that ship, and they must be verifiable on CI runners that are not Windows.
"""

import json
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from axio_stitching.agent_integration import (
    AGENT_TARGETS,
    MCP_NAMESPACED_NAME,
    MCP_SERVER_NAME,
    PLUGIN_DIR_NAME,
    SKILL_DIR_NAME,
    IntegrationCtx,
    antigravity_mcp_config_path,
    antigravity_plugin_dir,
    build_plan_for_target,
    claude_config_dir,
    claude_desktop_config_path,
    claude_plugin_dir,
    claude_skill_dir,
    codex_config_dir,
    codex_config_toml_path,
    codex_skill_dir,
    gemini_cli_settings_path,
    is_bare_interpreter_launcher,
    mcp_server_entry,
    parse_skill_frontmatter,
    pathmod_for,
    render_installed_skill,
)


def win_ctx(**overrides) -> IntegrationCtx:
    base = dict(
        platform="win32",
        home=r"C:\Users\tester",
        env={"APPDATA": r"C:\Users\tester\AppData\Roaming"},
        pathmod=PureWindowsPath,
        exec_path=r"C:\proj\.venv\Scripts\python.exe",
        exec_args=("-m", "axio_stitching.mcp_server"),
    )
    base.update(overrides)
    return IntegrationCtx(**base)


def posix_ctx(**overrides) -> IntegrationCtx:
    base = dict(
        platform="linux",
        home="/home/tester",
        env={},
        pathmod=PurePosixPath,
        exec_path="/home/tester/.venv/bin/python",
        exec_args=("-m", "axio_stitching.mcp_server"),
    )
    base.update(overrides)
    return IntegrationCtx(**base)


SKILL = render_installed_skill()
REFERENCE = "# reference\n"


class TestPathmodFor:
    def test_selects_windows_paths_for_win32(self):
        assert pathmod_for("win32") is PureWindowsPath

    def test_selects_posix_paths_otherwise(self):
        assert pathmod_for("linux") is PurePosixPath
        assert pathmod_for("darwin") is PurePosixPath


class TestClaudePaths:
    def test_defaults_to_dot_claude(self):
        assert claude_config_dir(win_ctx()) == r"C:\Users\tester\.claude"
        assert claude_config_dir(posix_ctx()) == "/home/tester/.claude"

    def test_honours_the_config_dir_override(self):
        ctx = win_ctx(env={"CLAUDE_CONFIG_DIR": r"D:\claude"})
        assert claude_config_dir(ctx) == r"D:\claude"

    def test_ignores_a_blank_override(self):
        ctx = win_ctx(env={"CLAUDE_CONFIG_DIR": "   "})
        assert claude_config_dir(ctx) == r"C:\Users\tester\.claude"

    def test_skill_and_plugin_are_separate_directories(self):
        ctx = win_ctx()
        assert claude_skill_dir(ctx).endswith(SKILL_DIR_NAME)
        assert claude_plugin_dir(ctx).endswith(PLUGIN_DIR_NAME)
        assert claude_skill_dir(ctx) != claude_plugin_dir(ctx)


class TestClaudeDesktopPath:
    def test_windows_uses_appdata(self):
        assert claude_desktop_config_path(win_ctx()) == (
            r"C:\Users\tester\AppData\Roaming\Claude\claude_desktop_config.json"
        )

    def test_windows_falls_back_when_appdata_is_unset(self):
        assert "AppData" in claude_desktop_config_path(win_ctx(env={}))

    def test_macos_uses_application_support(self):
        ctx = posix_ctx(platform="darwin")
        assert claude_desktop_config_path(ctx) == (
            "/home/tester/Library/Application Support/Claude/claude_desktop_config.json"
        )

    def test_linux_uses_dot_config(self):
        assert claude_desktop_config_path(posix_ctx()) == (
            "/home/tester/.config/Claude/claude_desktop_config.json"
        )

    def test_honours_the_explicit_override(self):
        ctx = posix_ctx(env={"CLAUDE_DESKTOP_CONFIG": "/tmp/cd.json"})
        assert claude_desktop_config_path(ctx) == "/tmp/cd.json"


class TestCodexPaths:
    def test_defaults_to_dot_codex(self):
        assert codex_config_dir(posix_ctx()) == "/home/tester/.codex"

    def test_honours_codex_home(self):
        ctx = posix_ctx(env={"CODEX_HOME": "/opt/codex"})
        assert codex_config_dir(ctx) == "/opt/codex"
        assert codex_skill_dir(ctx) == f"/opt/codex/skills/{SKILL_DIR_NAME}"
        assert codex_config_toml_path(ctx) == "/opt/codex/config.toml"


class TestAntigravityPaths:
    def test_defaults_to_gemini_config(self):
        assert antigravity_plugin_dir(posix_ctx()) == f"/home/tester/.gemini/config/plugins/{PLUGIN_DIR_NAME}"
        assert antigravity_mcp_config_path(posix_ctx()) == "/home/tester/.gemini/config/mcp_config.json"

    def test_honours_the_override(self):
        ctx = posix_ctx(env={"ANTIGRAVITY_CONFIG_DIR": "/opt/ag"})
        assert antigravity_plugin_dir(ctx) == f"/opt/ag/plugins/{PLUGIN_DIR_NAME}"


class TestGeminiCliPath:
    def test_defaults_to_dot_gemini(self):
        assert gemini_cli_settings_path(posix_ctx()) == "/home/tester/.gemini/settings.json"


class TestMcpServerEntry:
    def test_includes_the_stdio_discriminator_when_asked(self):
        assert mcp_server_entry(win_ctx(), include_type=True)["type"] == "stdio"

    def test_omits_the_discriminator_for_antigravity_and_codex(self):
        assert "type" not in mcp_server_entry(win_ctx(), include_type=False)

    def test_always_forces_utf8(self):
        env = mcp_server_entry(win_ctx())["env"]
        assert env["PYTHONUTF8"] == "1"
        assert env["PYTHONIOENCODING"] == "utf-8"

    def test_never_bakes_a_python_interpreter_as_the_app(self):
        entry = mcp_server_entry(win_ctx(exec_path=r"C:\Python312\python.exe"))
        assert "AXIO_STITCHING_APP" not in entry["env"]

    def test_bakes_an_explicit_app_path(self):
        entry = mcp_server_entry(win_ctx(app_path=r"C:\apps\AXIO_Stitching_Studio.exe"))
        assert entry["env"]["AXIO_STITCHING_APP"] == r"C:\apps\AXIO_Stitching_Studio.exe"

    def test_falls_back_to_exec_path_when_it_is_a_real_app(self):
        ctx = win_ctx(exec_path=r"C:\apps\AXIO_Stitching_MCP.exe", exec_args=("--mcp-serve",))
        assert mcp_server_entry(ctx)["env"]["AXIO_STITCHING_APP"] == r"C:\apps\AXIO_Stitching_MCP.exe"

    def test_omits_an_unset_out_dir_rather_than_baking_an_empty_string(self):
        assert "AXIO_STITCHING_OUT_DIR" not in mcp_server_entry(win_ctx(default_out_dir="  "))["env"]

    def test_bakes_a_provided_out_dir(self):
        entry = mcp_server_entry(win_ctx(default_out_dir=r"D:\out"))
        assert entry["env"]["AXIO_STITCHING_OUT_DIR"] == r"D:\out"

    def test_carries_the_command_and_args_verbatim(self):
        entry = mcp_server_entry(win_ctx())
        assert entry["command"] == r"C:\proj\.venv\Scripts\python.exe"
        assert entry["args"] == ["-m", "axio_stitching.mcp_server"]


class TestIsBareInterpreterLauncher:
    @pytest.mark.parametrize("exe", ["python.exe", "python", "pythonw.exe", "py.exe"])
    def test_recognises_interpreters(self, exe):
        assert is_bare_interpreter_launcher(win_ctx(exec_path=rf"C:\x\{exe}"))

    def test_does_not_flag_the_app(self):
        assert not is_bare_interpreter_launcher(win_ctx(exec_path=r"C:\x\AXIO_Stitching_MCP.exe"))


class TestParseSkillFrontmatter:
    def test_reads_simple_scalars(self):
        assert parse_skill_frontmatter("---\nname: x\ndescription: y\n---\nbody") == {
            "name": "x",
            "description": "y",
        }

    def test_gathers_a_folded_block(self):
        source = "---\nname: x\ndescription: >-\n  first line\n  second line\n---\n"
        assert parse_skill_frontmatter(source)["description"] == "first line second line"

    def test_returns_empty_without_frontmatter(self):
        assert parse_skill_frontmatter("# just a heading\n") == {}


class TestRenderInstalledSkill:
    def test_has_valid_frontmatter(self):
        front = parse_skill_frontmatter(SKILL)
        assert front["name"] == SKILL_DIR_NAME
        assert len(front["description"]) > 60

    def test_carries_no_dev_repo_recipe(self):
        for repo_ism in ("cd AXIO_Sticthing", "conda env create", "pip install -e .", "gui_runner.py"):
            assert repo_ism not in SKILL

    def test_drives_the_mcp_tools_by_name(self):
        for tool in (
            "axio_doctor",
            "axio_inspect_dataset",
            "axio_estimate_stitch",
            "axio_start_stitch",
            "axio_job_status",
            "axio_qc_report",
            "axio_read_preview",
            "axio_launch_gui",
        ):
            assert tool in SKILL, f"the installed skill must teach {tool}"

    def test_teaches_non_zeiss_sources(self):
        # The headline capability must be discoverable in the skill an agent reads.
        for token in ("Fiji", "TileConfiguration", "OME-TIFF", "vendor-neutral", "axio_detect_source"):
            assert token in SKILL, f"the installed skill must mention {token}"
        # And it must warn not to trust an inferred grid with coordinate mode.
        assert "coordinate" in SKILL and "grid" in SKILL.lower()

    def test_names_no_single_platform_registration_mechanism(self):
        # The same render ships to Claude Code, Codex and Antigravity, so it must stay neutral.
        for platform_ism in (".mcp.json", "claude_desktop_config", "mcp_config.json", "config.toml"):
            assert platform_ism not in SKILL

    def test_accepts_an_inherited_description(self):
        rendered = render_installed_skill("custom-name", "custom description")
        front = parse_skill_frontmatter(rendered)
        assert front["name"] == "custom-name"
        assert front["description"] == "custom description"

    def test_falls_back_when_given_blanks(self):
        front = parse_skill_frontmatter(render_installed_skill("  ", "  "))
        assert front["name"] == SKILL_DIR_NAME
        assert front["description"]


class TestPlans:
    def test_every_known_target_builds(self):
        for target in AGENT_TARGETS:
            plan = build_plan_for_target(target, win_ctx(), SKILL, REFERENCE)
            assert plan.target == target
            assert plan.units or plan.config_keys, f"{target} plan does nothing"

    def test_an_unknown_target_is_rejected(self):
        with pytest.raises(ValueError, match="unknown agent target"):
            build_plan_for_target("emacs", win_ctx(), SKILL, REFERENCE)

    def test_claude_code_is_a_pure_file_drop(self):
        plan = build_plan_for_target("claude-code", win_ctx(), SKILL, REFERENCE)
        assert plan.config_keys == (), "Claude Code must not touch a shared config file"
        assert {u.kind for u in plan.units} == {"claude-skill", "claude-mcp-plugin"}
        assert plan.mcp_name == MCP_NAMESPACED_NAME

    def test_claude_code_plugin_registers_the_stdio_server(self):
        plan = build_plan_for_target("claude-code", win_ctx(), SKILL, REFERENCE)
        plugin = next(u for u in plan.units if u.kind == "claude-mcp-plugin")
        mcp_json = next(f for f in plugin.files if f.rel == ".mcp.json")
        entry = json.loads(mcp_json.content)["mcpServers"][MCP_SERVER_NAME]
        assert entry["type"] == "stdio"
        assert entry["command"] == r"C:\proj\.venv\Scripts\python.exe"

    def test_codex_owns_one_toml_table(self):
        plan = build_plan_for_target("codex", win_ctx(), SKILL, REFERENCE)
        assert len(plan.config_keys) == 1
        key = plan.config_keys[0]
        assert key.fmt == "toml"
        assert key.key_path == ("mcp_servers", MCP_SERVER_NAME)
        assert "type" not in key.value, "Codex MCP tables carry no type discriminator"
        assert key.value["startup_timeout_sec"] == 60

    def test_antigravity_owns_one_json_key_and_drops_a_plugin(self):
        plan = build_plan_for_target("antigravity", win_ctx(), SKILL, REFERENCE)
        assert len(plan.units) == 1 and plan.units[0].kind == "antigravity-plugin"
        assert [f.rel for f in plan.units[0].files][0] == "plugin.json"
        key = plan.config_keys[0]
        assert key.fmt == "json" and key.key_path == ("mcpServers", MCP_SERVER_NAME)
        assert "type" not in key.value

    def test_claude_desktop_is_mcp_only(self):
        plan = build_plan_for_target("claude-desktop", win_ctx(), SKILL, REFERENCE)
        assert plan.units == ()
        assert plan.config_keys[0].value["type"] == "stdio"

    def test_gemini_cli_is_mcp_only(self):
        plan = build_plan_for_target("gemini-cli", win_ctx(), SKILL, REFERENCE)
        assert plan.units == ()
        assert plan.config_keys[0].key_path == ("mcpServers", MCP_SERVER_NAME)

    @pytest.mark.parametrize("target", AGENT_TARGETS)
    def test_every_emitted_json_file_is_pure_ascii(self, target):
        # These manifests are parsed by other vendors' tools whose decoding assumptions we do
        # not control; a non-ASCII byte read under a CJK codepage has broken them before.
        plan = build_plan_for_target(target, win_ctx(), SKILL, REFERENCE)
        for unit in plan.units:
            for planned in unit.files:
                if planned.rel.endswith(".json"):
                    planned.content.encode("ascii")

    @pytest.mark.parametrize("target", AGENT_TARGETS)
    def test_plan_paths_use_the_injected_path_flavour(self, target):
        win = build_plan_for_target(target, win_ctx(), SKILL, REFERENCE)
        posix = build_plan_for_target(target, posix_ctx(), SKILL, REFERENCE)
        win_paths = [u.dir for u in win.units] + [k.file for k in win.config_keys]
        posix_paths = [u.dir for u in posix.units] + [k.file for k in posix.config_keys]
        assert all("\\" in p for p in win_paths), win_paths
        assert all("\\" not in p for p in posix_paths), posix_paths

    @pytest.mark.parametrize("target", AGENT_TARGETS)
    def test_plan_file_rels_are_posix_style(self, target):
        plan = build_plan_for_target(target, win_ctx(), SKILL, REFERENCE)
        for unit in plan.units:
            for planned in unit.files:
                assert "\\" not in planned.rel
