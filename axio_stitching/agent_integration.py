"""
agent_integration.py — connect AXIO's agent surfaces (the skill + the MCP server) into a
user's AI coding agent. This module is the PURE core: it computes target paths and file
contents from an injected context and does NO I/O, so every branch — including the Windows
path logic — is unit-testable on any OS. The filesystem side lives in
:mod:`axio_stitching.agent_runner`.

Targets (see :data:`AGENT_TARGETS`):

``claude-code``
    Claude Code (the CLI and the Claude Code desktop app share ``~/.claude``). A pure
    FILE-DROP: two NEW directories under ``<cfg>/skills/`` — the rendered skill, and an MCP
    plugin (``.claude-plugin/plugin.json`` + ``.mcp.json``) that registers the stdio server.
    Nothing merges into a shared config file. Claude Code namespaces the server as
    ``plugin:axio-stitching:axio-stitching``.

``codex``
    OpenAI Codex — the Codex CLI, its IDE extension, and the **ChatGPT desktop app**, whose
    agent runtime IS Codex. A skill file-drop under ``$CODEX_HOME/skills/`` plus exactly ONE
    owned key ``mcp_servers.axio-stitching`` in the SHARED ``config.toml``.

``antigravity``
    Google Antigravity IDE. A plugin file-drop under ``<geminiConfig>/plugins/`` (plugins
    there are auto-discovered — no manifest to merge into) plus ONE owned key
    ``mcpServers.axio-stitching`` in the SHARED ``mcp_config.json``.

``claude-desktop``
    The classic Claude Desktop app. MCP only — it has no skills directory — via ONE owned
    key in ``claude_desktop_config.json``.

``gemini-cli``
    The Gemini CLI. MCP only, via ONE owned key in ``~/.gemini/settings.json``.

Every edit to a file we do not own goes through the surgical contract in
:mod:`axio_stitching.configkey` / :mod:`axio_stitching.configkey_toml` — never a wholesale
rewrite.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from . import __version__

# ---------------------------------------------------------------------------
# Identity constants
# ---------------------------------------------------------------------------

#: Sidecar filename dropped in each managed directory so uninstall knows exactly what it
#: wrote (and can refuse to touch anything it did not).
MANAGED_SIDECAR = ".axio-stitching-managed.json"

#: Stable marker identifying our sidecars.
MANAGED_BY = "axio-stitching-studio"

#: Sidecar schema version, bumped when the recorded shape changes.
MANAGED_SIDECAR_VERSION = 1

#: Skill directory name (invoked as ``/axio-stitching-pipeline``) and plugin directory name.
SKILL_DIR_NAME = "axio-stitching-pipeline"
PLUGIN_DIR_NAME = "axio-stitching"

#: The MCP server's bare name, and the name Claude Code exposes it under (namespaced by the
#: providing plugin). Probe/remove for Claude Code must use the namespaced form.
MCP_SERVER_NAME = "axio-stitching"
MCP_NAMESPACED_NAME = f"plugin:{PLUGIN_DIR_NAME}:{MCP_SERVER_NAME}"

#: Version stamped into the emitted plugin manifests.
PLUGIN_VERSION = __version__

AgentTarget = Literal["claude-code", "codex", "antigravity", "claude-desktop", "gemini-cli"]

#: Every target this installer understands, in the order a "install everywhere" run uses.
AGENT_TARGETS: tuple[AgentTarget, ...] = (
    "claude-code",
    "codex",
    "antigravity",
    "claude-desktop",
    "gemini-cli",
)

#: Human labels for reporting.
TARGET_LABELS: dict[str, str] = {
    "claude-code": "Claude Code (CLI + desktop app)",
    "codex": "OpenAI Codex / ChatGPT desktop app",
    "antigravity": "Google Antigravity IDE",
    "claude-desktop": "Claude Desktop",
    "gemini-cli": "Gemini CLI",
}

#: Kept deliberately ASCII-only: these manifests are parsed by other vendors' tools whose
#: decoding assumptions we do not control, and a non-ASCII byte in a file read under a CJK
#: codepage is a well-known way to break them.
PLUGIN_DESCRIPTION = (
    "Drive AXIO Stitching Studio from your agent: inspect Zeiss tile-scan metadata, "
    "estimate canvas size and memory before committing, run shading correction and tile "
    "registration, and assemble multi-channel / Z-stack mosaics. Contains no LLM code - "
    "it is a tool provider."
)


# ---------------------------------------------------------------------------
# Injected context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntegrationCtx:
    """
    Everything the pure core needs from the outside world.

    ``pathmod`` is :class:`PureWindowsPath` or :class:`PurePosixPath`, chosen by the caller
    from ``platform``, so path joins are deterministic and testable regardless of the OS
    running the tests.
    """

    #: ``sys.platform`` — selects path semantics and the config-dir defaults.
    platform: str
    #: ``Path.home()`` as a string.
    home: str
    #: ``os.environ`` (or a fixture). Only read, never mutated.
    env: dict[str, str] = field(default_factory=dict)
    #: ``PureWindowsPath`` or ``PurePosixPath``.
    pathmod: type[PurePath] = PurePosixPath
    #: The executable that launches the MCP server: the venv/system Python, or the frozen
    #: AXIO Stitching Studio executable.
    exec_path: str = "python"
    #: Argument vector after ``exec_path``. ``["-m", "axio_stitching.mcp_server"]`` for a
    #: Python launcher; ``["--mcp-serve"]`` for the frozen desktop executable.
    exec_args: tuple[str, ...] = ("-m", "axio_stitching.mcp_server")
    #: Absolute path to the AXIO Stitching Studio GUI executable or entry script, baked into
    #: the server env so the ``axio_launch_gui`` tool can reach the app. Omitted when unknown
    #: rather than baked as an empty string.
    app_path: str | None = None
    #: Optional default output directory, baked so a fresh agent has somewhere sensible to write.
    default_out_dir: str | None = None

    def join(self, *parts: str) -> str:
        return str(self.pathmod(*parts))


def pathmod_for(platform: str) -> type[PurePath]:
    """The pure-path flavour matching ``platform`` (``sys.platform`` semantics)."""
    return PureWindowsPath if platform.startswith("win") else PurePosixPath


# ---------------------------------------------------------------------------
# Path computation (pure)
# ---------------------------------------------------------------------------

def _env(ctx: IntegrationCtx, key: str) -> str | None:
    value = ctx.env.get(key)
    return value if value and value.strip() else None


def claude_config_dir(ctx: IntegrationCtx) -> str:
    """Claude Code's config directory: ``$CLAUDE_CONFIG_DIR`` if set, else ``<home>/.claude``."""
    return _env(ctx, "CLAUDE_CONFIG_DIR") or ctx.join(ctx.home, ".claude")


def claude_skills_dir(ctx: IntegrationCtx) -> str:
    return ctx.join(claude_config_dir(ctx), "skills")


def claude_skill_dir(ctx: IntegrationCtx) -> str:
    return ctx.join(claude_skills_dir(ctx), SKILL_DIR_NAME)


def claude_plugin_dir(ctx: IntegrationCtx) -> str:
    return ctx.join(claude_skills_dir(ctx), PLUGIN_DIR_NAME)


def claude_desktop_config_path(ctx: IntegrationCtx) -> str:
    """
    Claude Desktop's shared MCP config. ``$CLAUDE_DESKTOP_CONFIG`` overrides; otherwise the
    per-OS location the app itself uses.
    """
    override = _env(ctx, "CLAUDE_DESKTOP_CONFIG")
    if override:
        return override
    if ctx.platform.startswith("win"):
        appdata = _env(ctx, "APPDATA") or ctx.join(ctx.home, "AppData", "Roaming")
        return ctx.join(appdata, "Claude", "claude_desktop_config.json")
    if ctx.platform == "darwin":
        return ctx.join(ctx.home, "Library", "Application Support", "Claude", "claude_desktop_config.json")
    return ctx.join(ctx.home, ".config", "Claude", "claude_desktop_config.json")


def antigravity_config_dir(ctx: IntegrationCtx) -> str:
    """
    Google Antigravity's config directory: ``$ANTIGRAVITY_CONFIG_DIR`` if set, else
    ``<home>/.gemini/config`` (shared by the Antigravity IDE and the standalone app).
    """
    return _env(ctx, "ANTIGRAVITY_CONFIG_DIR") or ctx.join(ctx.home, ".gemini", "config")


def antigravity_plugin_dir(ctx: IntegrationCtx) -> str:
    """
    Our Antigravity plugin directory. Plugins under ``<cfg>/plugins/`` are AUTO-DISCOVERED —
    there is no manifest to merge into — so this part of the install is a pure file-drop into
    a NEW directory. The MCP server is registered separately, as an owned key in the shared
    ``mcp_config.json``.
    """
    return ctx.join(antigravity_config_dir(ctx), "plugins", PLUGIN_DIR_NAME)


def antigravity_mcp_config_path(ctx: IntegrationCtx) -> str:
    """
    Antigravity's SHARED user-level MCP registry — the only file that makes a server appear
    in Settings -> Customizations. Because it is shared, we touch exactly one key inside it.
    """
    return ctx.join(antigravity_config_dir(ctx), "mcp_config.json")


def codex_config_dir(ctx: IntegrationCtx) -> str:
    """
    OpenAI Codex's root: ``$CODEX_HOME`` if set, else ``<home>/.codex`` — shared by the
    Codex CLI, the IDE extension and the ChatGPT desktop app.
    """
    return _env(ctx, "CODEX_HOME") or ctx.join(ctx.home, ".codex")


def codex_skills_dir(ctx: IntegrationCtx) -> str:
    return ctx.join(codex_config_dir(ctx), "skills")


def codex_skill_dir(ctx: IntegrationCtx) -> str:
    return ctx.join(codex_skills_dir(ctx), SKILL_DIR_NAME)


def codex_config_toml_path(ctx: IntegrationCtx) -> str:
    """Codex's SHARED ``config.toml`` — we own exactly one table inside it."""
    return ctx.join(codex_config_dir(ctx), "config.toml")


def gemini_cli_config_dir(ctx: IntegrationCtx) -> str:
    """The Gemini CLI's root: ``$GEMINI_CONFIG_DIR`` if set, else ``<home>/.gemini``."""
    return _env(ctx, "GEMINI_CONFIG_DIR") or ctx.join(ctx.home, ".gemini")


def gemini_cli_settings_path(ctx: IntegrationCtx) -> str:
    return ctx.join(gemini_cli_config_dir(ctx), "settings.json")


# ---------------------------------------------------------------------------
# MCP server entry (pure)
# ---------------------------------------------------------------------------

#: Executable basenames that must never be baked as ``AXIO_STITCHING_APP`` — baking the
#: interpreter as "the desktop app" makes ``axio_launch_gui`` try to launch Python.
_NON_APP_BASENAMES = {"python", "python.exe", "pythonw", "pythonw.exe", "py", "py.exe"}


def is_bare_interpreter_launcher(ctx: IntegrationCtx) -> bool:
    """True when ``exec_path`` is a plain Python interpreter rather than the frozen app."""
    return ctx.pathmod(ctx.exec_path).name.lower() in _NON_APP_BASENAMES


def mcp_server_entry(ctx: IntegrationCtx, include_type: bool = True) -> dict[str, Any]:
    """
    The stdio MCP server registration.

    ``PYTHONUTF8=1`` is mandatory on Windows: Zeiss datasets routinely carry non-ASCII path
    components, and a cp932/cp1252 default codepage turns those into UnicodeDecodeErrors
    inside the server. App path and default output dir are baked only when known, never as
    empty strings. ``include_type`` adds the ``"type": "stdio"`` discriminator that Claude
    expects and that Antigravity/Codex do not use.
    """
    env: dict[str, str] = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    app_path = ctx.app_path if (ctx.app_path and ctx.app_path.strip()) else None
    if app_path is None and not is_bare_interpreter_launcher(ctx):
        app_path = ctx.exec_path
    if app_path:
        env["AXIO_STITCHING_APP"] = app_path
    if ctx.default_out_dir and ctx.default_out_dir.strip():
        env["AXIO_STITCHING_OUT_DIR"] = ctx.default_out_dir

    entry: dict[str, Any] = {}
    if include_type:
        entry["type"] = "stdio"
    entry["command"] = ctx.exec_path
    entry["args"] = list(ctx.exec_args)
    entry["env"] = env
    return entry


# ---------------------------------------------------------------------------
# Skill rendering (pure)
# ---------------------------------------------------------------------------

def parse_skill_frontmatter(source: str) -> dict[str, str]:
    """
    Pull ``name`` and ``description`` out of a SKILL.md YAML frontmatter block.

    Deliberately tiny (no YAML dependency): the frontmatter of an Agent Skill is a flat
    scalar map. ``description`` may be a folded block (``>-``), so continuation lines are
    gathered.
    """
    match = re.match(r"^---\r?\n(.*?)\r?\n---", source, re.DOTALL)
    if not match:
        return {}
    lines = match.group(1).split("\n")
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not kv:
            continue
        key, value = kv.group(1), kv.group(2).strip()
        if value in {">", ">-", "|", "|-", ""}:
            base_indent = len(line) - len(line.lstrip())
            gathered: list[str] = []
            for later in lines[i + 1 :]:
                if not later.strip():
                    continue
                if len(later) - len(later.lstrip()) <= base_indent:
                    break
                gathered.append(later.strip())
            if gathered:
                value = " ".join(gathered).strip()
        if key in ("name", "description"):
            out[key] = value
    return out


#: Patterns that must NOT survive into a rendered, installed skill — they are dev-repo
#: recipes that fail immediately in an installed context. The self-check raises on any match.
_REPO_ISM = re.compile(
    r"cd AXIO_Sticthing|conda env create|pip install -e \.|python -m axio_stitching\.cli|scripts/gui_runner\.py"
)

DEFAULT_SKILL_DESCRIPTION = (
    "Stitch Zeiss Axio tile-scan microscopy datasets with AXIO Stitching Studio via its MCP "
    "tools: inspect _info.xml / _meta.xml metadata, estimate canvas size and peak memory "
    "before committing, apply BaSiCPy / median / spatial shading correction, register tiles "
    "by phase correlation, SIFT or stage coordinates, and assemble multi-channel, "
    "split-channel and 3D Z-stack mosaics as ImageJ-compatible TIFFs. Use it for ANY request "
    "to stitch, mosaic, assemble or flatfield-correct microscope tiles, to read tile-scan "
    "metadata, or to QC an already-stitched mosaic - never hand-roll a stitching script for that."
)


def render_installed_skill(name: str = SKILL_DIR_NAME, description: str = "") -> str:
    """
    Produce the installed SKILL.md.

    It teaches the same inspect -> estimate -> validate -> stitch -> QC loop as the shipped
    repo skill, but expressed against the MCP tools the installer registers, so there is no
    shell-command or cwd assumption to break. Target-neutral by design: the same render ships
    to Claude Code, Codex/ChatGPT and Antigravity, so it must not name a registration
    mechanism only one target has.

    Raises if the result still contains a repo-ism or lost its frontmatter — a guard against
    a future edit silently shipping a broken skill.
    """
    safe_name = name.strip() or SKILL_DIR_NAME
    safe_desc = description.strip() or DEFAULT_SKILL_DESCRIPTION

    md = f"""---
name: {safe_name}
description: >-
  {safe_desc}
---

# AXIO tile-scan stitching (via MCP)

You drive **AXIO Stitching Studio**'s own pipeline through the `axio_*` MCP tools installed
alongside this skill — never a reimplementation. The tools carry **no LLM**; the intelligence
(reading the dataset, choosing correction and registration, judging the result) is yours.

**Three failure modes to avoid, all seen in the field:**

1. **Do not write your own stitching script.** No `stitch.py`, no ad-hoc
   `skimage.registration.phase_cross_correlation` loop. A hand-rolled script reproduces none
   of the pipeline's blending, channel handling, or Z-stack semantics, and its output is not
   what the desktop app shows the user. Every step below is a tool call.
2. **Do not start a stitch you have not sized.** These are gigapixel canvases: a 5,000-tile
   scene at 1020x1020 with 3 channels and 40 Z-slices is terabytes of intermediate. Call
   `axio_estimate_stitch` FIRST and read its verdict — an OOM 40 minutes into a run costs the
   user the whole run.
3. **Do not block on a long stitch.** Use `axio_start_stitch` + `axio_job_status`. A real
   scene takes minutes to hours; a synchronous call will simply time out and you will lose
   the handle to a job that is still running.

## 0. Confirm the environment

Call **`axio_doctor`** first and resolve anything it reports. It checks the interpreter, the
required and optional packages (**BaSiCPy** for `basicpy` correction, **OpenCV** for `sift`),
free RAM and free disk on the output volume, and prints exactly what is missing and how to
install it. A correction or algorithm whose package is absent fails at run time, not at
config time — so read this before choosing either.

## 1. Read the dataset

**`axio_inspect_dataset`** `{{ xml_path }}` — parse a Zeiss `_info.xml` or `_meta.xml` and get
back the scenes, the tiles per scene with their stage coordinates and sizes, the tile pixel
dimensions, the channel and Z-slice counts, and the pixel scale in um when the metadata
carries it.

Read this BEFORE proposing anything: it tells you whether the dataset is **multi-page**
(channels inside each tile TIFF — use `ref_channel`) or **split-channel** (one file per
channel, distinguished by a filename tag — use `ref_tag` + `target_tags`), how many scenes
there are, and whether there is a Z dimension at all. Those three facts decide every
parameter below. **`axio_list_algorithms`** gives you the exact vocabulary of legal values.

## 2. Size the job before committing to it

**`axio_estimate_stitch`** `{{ xml_path, scene, correction, algorithm, z_mode, ... }}` — returns
the canvas dimensions in pixels, the output file size, the estimated peak RAM, the
intermediate footprint of the correction step, a rough wall-clock estimate, and a `verdict`
of `ok` / `tight` / `will_not_fit` with the reason.

Act on the verdict rather than reporting it:
- **`will_not_fit`** — do not start. Narrow the job: a single `scene` instead of all scenes,
  `z_mode="mip_output_only"` instead of a full 3D volume, or fewer `target_tags` per run.
  Stitching scene-by-scene and channel-by-channel is the standard way to fit a large dataset
  into a small machine, and the outputs are identical.
- **`tight`** — say so, name the headroom, and let the user decide before you spend an hour.
- **`ok`** — proceed.

## 3. Validate, then start the job

1. **`axio_validate_stitch`** `{{ xml_path, out_dir, correction, algorithm, ... }}` — checks the
   XML parses and yields scenes, that the tile files named by the metadata actually exist next
   to it, that `out_dir` is writable, and that the packages your chosen `correction` and
   `algorithm` need are importable. Fix every **error**; read every **warning** (missing tiles
   are the common one — a partially-copied dataset stitches to a canvas full of holes).
2. **`axio_start_stitch`** `{{ ...same config... }}` — starts the run in the background and
   returns a `job_id` immediately.
3. **`axio_job_status`** `{{ job_id }}` — poll it: `state` (`running` / `succeeded` / `failed` /
   `cancelled`), `percent`, `stage`, elapsed time, and the tail of the log. Poll at a human
   cadence (tens of seconds), and tell the user what stage it is in rather than going silent.
4. **`axio_job_result`** `{{ job_id }}` — the final `StitchResult`: `output_paths`,
   `preview_paths`, `scenes_processed`, `tiles_processed`, `duration_seconds`.
5. **`axio_cancel_job`** `{{ job_id }}` — stop a run the user no longer wants. Cancellation is
   cooperative: it takes effect at the next stage boundary, and any output already written stays.

`axio_stitch_sync` exists for genuinely small datasets (a handful of tiles, one channel, no Z)
and for tests. Reach for it only when `axio_estimate_stitch` says the job is seconds long.

## 4. Look at the result before you report success

A stitch that "succeeded" can still be wrong — a bad registration produces a canvas with
duplicated or torn tissue, and an over-aggressive correction flattens real signal.

- **`axio_read_preview`** `{{ path }}` — returns the preview thumbnail as an IMAGE you can
  actually see. Look at it. Report what you see, not just the exit status.
- **`axio_qc_report`** `{{ path }}` — bounded metrics over the stitched canvas: dynamic range,
  saturated fraction, empty (never-written) fraction, and seam-discontinuity scores sampled at
  tile boundaries. A high empty fraction means tiles were missing or the registration flung a
  tile off-canvas; a high seam score means the registration did not converge.
- **`axio_list_outputs`** `{{ directory }}` — what a previous run left behind, so you can pick up
  where the user left off instead of re-running an hour of work.

If registration looks wrong, change ONE thing and re-run: `phase` -> `sift` for low-contrast
fluorescence or visible stage drift; `sift` -> `coordinate` when the stage is trustworthy and
feature matching is finding nothing; `alignment_mode="max_projection"` when no single channel
carries enough structure to align on.

## 5. Hand the work back to the user

**`axio_launch_gui`** `{{ out_dir }}` — open AXIO Stitching Studio so the human can view the
mosaic at full resolution and re-run with adjusted parameters. Do this when you have an output
worth reviewing; a path in a chat log is not a delivered result. If the app cannot be located
the tool says so — relay that instead of guessing at a path.

## Choosing parameters

**Correction** (`correction`)
| Value | When |
|---|---|
| `basicpy` | Best quality; visible illumination gradient or vignetting. Slow, needs the `basicpy` package. |
| `median` | Good approximation at a fraction of the cost. The right default for large datasets. |
| `spatial` | Rolling-ball background subtraction — uneven *background*, not uneven *illumination*. |
| `none` | Tiles are already flat-fielded, or you are iterating on registration and want speed. |

**Registration** (`algorithm`)
| Value | When |
|---|---|
| `phase` | Default. Fast and robust when the stage is repeatable and tiles have texture. |
| `sift` | Low contrast, sparse fluorescence, or real stage drift. Needs OpenCV. Slower. |
| `coordinate` | No registration at all — trust the stage coordinates. Fastest; correct when the stage is accurate and the sample is featureless. |

**Channels**
- Multi-page tiles (channels inside one TIFF): set `ref_channel` to the channel with the most
  structure — usually a nuclear or autofluorescence channel, rarely a sparse marker.
- Split-channel tiles (one file per channel): set `ref_tag` to the reference channel's filename
  tag (e.g. `"_c1_"`) and `target_tags` to the others (e.g. `"_c2_,_c3_"`). Registration is
  computed once on the reference and APPLIED to every target, so the channels stay in register.
- `alignment_mode`: `reference` (align on `ref_channel` alone), `average` or `max_projection`
  (fuse channels first) when no single channel has enough structure.

**Z-stacks** (`z_mode`)
| Value | Result |
|---|---|
| `none` | 2D only — stitch the first slice. |
| `mip_output_only` | Align in 2D, output a maximum-intensity projection. Cheapest way to see a Z dataset. |
| `mip_align_3d` | Align on the MIP, then apply that transform to every slice — full 3D volume out. |
| `ref_slice_3d` | Align on slice `ref_z_slice`, apply to every slice. Use when one slice is in focus and the MIP is not. |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `BaSiCPy not installed` | `pip install basicpy`, or switch to `correction="median"`. |
| `OpenCV not installed for SIFT` | `pip install opencv-python`, or switch to `algorithm="phase"`. |
| `No tiles matched reference tag` | `ref_tag` does not occur in the filenames — re-read `axio_inspect_dataset` output and copy a tag from an actual filename. |
| `N tile files missing` (validate warning) | The raw tile TIFFs must sit beside the XML. A partial copy stitches to a canvas with holes. |
| Job fails with a memory error | `axio_estimate_stitch` said `tight` or `will_not_fit`. Split by scene, or use `z_mode="mip_output_only"`. |
| Output has torn or duplicated tissue | Registration diverged. Try `sift`; if that also fails, `coordinate` gives a geometrically honest (if seam-visible) mosaic. |
| Output is mostly empty | Tiles missing, or the wrong scene index. Re-check `axio_inspect_dataset`. |

## Scope

Use this skill for Zeiss Axio tile scans and the mosaics it produces. It is **not** for
registering non-tiled images, for non-Zeiss acquisition formats (use Bio-Formats), or for
downstream segmentation and analysis of an already-stitched image.
"""

    if _REPO_ISM.search(md):
        raise ValueError(
            "render_installed_skill produced a dev-repo command; refusing to install a broken skill"
        )
    frontmatter = parse_skill_frontmatter(md)
    if not frontmatter.get("name") or not frontmatter.get("description"):
        raise ValueError(
            "render_installed_skill produced invalid frontmatter (missing name or description)"
        )
    return md


# ---------------------------------------------------------------------------
# Plan (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanFile:
    """One file inside a managed directory. ``rel`` is always POSIX-style in the manifest."""
    rel: str
    content: str


@dataclass(frozen=True)
class ManagedUnit:
    """A NEW directory we own outright, plus the files inside it."""
    kind: str  # 'claude-skill' | 'claude-mcp-plugin' | 'antigravity-plugin' | 'codex-skill'
    dir: str
    files: tuple[PlanFile, ...]


@dataclass(frozen=True)
class PlanConfigKey:
    """
    A single key we own inside a config file belonging to someone else. Applied and removed
    only through :mod:`axio_stitching.configkey` (JSON) or
    :mod:`axio_stitching.configkey_toml` (TOML) — never a wholesale rewrite.
    """
    file: str
    key_path: tuple[str, ...]
    value: Any
    fmt: str = "json"  # 'json' | 'toml'


@dataclass(frozen=True)
class IntegrationPlan:
    target: str
    label: str
    config_dir: str
    mcp_name: str
    units: tuple[ManagedUnit, ...] = ()
    config_keys: tuple[PlanConfigKey, ...] = ()


def _json_text(value: Any) -> str:
    """ASCII-only, stable JSON with a trailing newline — see PLUGIN_DESCRIPTION's note."""
    return json.dumps(value, indent=2, ensure_ascii=True) + "\n"


def _plugin_manifest() -> str:
    return _json_text(
        {
            "name": PLUGIN_DIR_NAME,
            "description": "AXIO Stitching Studio - drive Zeiss tile-scan stitching from your agent.",
            "version": PLUGIN_VERSION,
        }
    )


def build_claude_code_plan(ctx: IntegrationCtx, rendered_skill: str, reference_doc: str) -> IntegrationPlan:
    """
    Claude Code — a pure file-drop into two NEW directories under ``<cfg>/skills/``:
    the rendered skill, and an MCP plugin that registers the stdio server. Nothing merges
    into a shared config file, which is what makes this the safest target.
    """
    entry = mcp_server_entry(ctx, include_type=True)
    return IntegrationPlan(
        target="claude-code",
        label=TARGET_LABELS["claude-code"],
        config_dir=claude_config_dir(ctx),
        mcp_name=MCP_NAMESPACED_NAME,
        units=(
            ManagedUnit(
                kind="claude-skill",
                dir=claude_skill_dir(ctx),
                files=(
                    PlanFile("SKILL.md", rendered_skill),
                    PlanFile("references/parameters.md", reference_doc),
                ),
            ),
            ManagedUnit(
                kind="claude-mcp-plugin",
                dir=claude_plugin_dir(ctx),
                files=(
                    PlanFile(".claude-plugin/plugin.json", _plugin_manifest()),
                    PlanFile(".mcp.json", _json_text({"mcpServers": {MCP_SERVER_NAME: entry}})),
                ),
            ),
        ),
    )


def build_codex_plan(ctx: IntegrationCtx, rendered_skill: str, reference_doc: str) -> IntegrationPlan:
    """
    OpenAI Codex (Codex CLI, IDE extension, ChatGPT desktop app) — a skill file-drop under
    ``$CODEX_HOME/skills/`` plus ONE owned table ``mcp_servers.axio-stitching`` in the shared
    ``config.toml``. Codex reads the same SKILL.md convention Claude Code does, so the render
    is shared. Its MCP tables carry no ``type`` discriminator.

    Deliberately NO ``codex mcp add`` shell-out even when the binary is on PATH: the owned-key
    surgery gives backup, atomic write, hash-verified removal and drift reporting, which
    delegating the edit would forfeit — and one audited mechanism beats two.
    """
    entry = mcp_server_entry(ctx, include_type=False)
    entry = dict(entry)
    entry["startup_timeout_sec"] = 60
    return IntegrationPlan(
        target="codex",
        label=TARGET_LABELS["codex"],
        config_dir=codex_config_dir(ctx),
        mcp_name=MCP_SERVER_NAME,
        units=(
            ManagedUnit(
                kind="codex-skill",
                dir=codex_skill_dir(ctx),
                files=(
                    PlanFile("SKILL.md", rendered_skill),
                    PlanFile("references/parameters.md", reference_doc),
                ),
            ),
        ),
        config_keys=(
            PlanConfigKey(
                file=codex_config_toml_path(ctx),
                key_path=("mcp_servers", MCP_SERVER_NAME),
                value=entry,
                fmt="toml",
            ),
        ),
    )


def build_antigravity_plan(ctx: IntegrationCtx, rendered_skill: str, reference_doc: str) -> IntegrationPlan:
    """
    Google Antigravity — ONE new plugin directory holding ``plugin.json`` (its presence is
    what declares the directory a plugin) and ``skills/<skill>/``, plus ONE owned key
    ``mcpServers.axio-stitching`` in the SHARED ``mcp_config.json``.

    Deliberately NO plugin-scoped ``mcp_config.json``: it is documented by the vendor but not
    honoured by the shipping build, so the server must be registered in the shared
    user-level file to appear in the MCP panel at all.
    """
    entry = mcp_server_entry(ctx, include_type=False)
    plugin_json = _json_text(
        {
            "name": PLUGIN_DIR_NAME,
            "version": PLUGIN_VERSION,
            "description": PLUGIN_DESCRIPTION,
            "author": {"name": "AXIO Stitching Studio"},
            "keywords": ["microscopy", "stitching", "zeiss", "imaging", "spatial", "mcp"],
        }
    )
    return IntegrationPlan(
        target="antigravity",
        label=TARGET_LABELS["antigravity"],
        config_dir=antigravity_config_dir(ctx),
        mcp_name=MCP_SERVER_NAME,
        units=(
            ManagedUnit(
                kind="antigravity-plugin",
                dir=antigravity_plugin_dir(ctx),
                files=(
                    PlanFile("plugin.json", plugin_json),
                    PlanFile(f"skills/{SKILL_DIR_NAME}/SKILL.md", rendered_skill),
                    PlanFile(f"skills/{SKILL_DIR_NAME}/references/parameters.md", reference_doc),
                ),
            ),
        ),
        config_keys=(
            PlanConfigKey(
                file=antigravity_mcp_config_path(ctx),
                key_path=("mcpServers", MCP_SERVER_NAME),
                value=entry,
                fmt="json",
            ),
        ),
    )


def build_claude_desktop_plan(ctx: IntegrationCtx, rendered_skill: str, reference_doc: str) -> IntegrationPlan:
    """
    Claude Desktop — MCP only. The app has no skills directory, so there is nothing to
    file-drop; we own exactly one key in ``claude_desktop_config.json``.
    """
    entry = mcp_server_entry(ctx, include_type=True)
    return IntegrationPlan(
        target="claude-desktop",
        label=TARGET_LABELS["claude-desktop"],
        config_dir=str(ctx.pathmod(claude_desktop_config_path(ctx)).parent),
        mcp_name=MCP_SERVER_NAME,
        config_keys=(
            PlanConfigKey(
                file=claude_desktop_config_path(ctx),
                key_path=("mcpServers", MCP_SERVER_NAME),
                value=entry,
                fmt="json",
            ),
        ),
    )


def build_gemini_cli_plan(ctx: IntegrationCtx, rendered_skill: str, reference_doc: str) -> IntegrationPlan:
    """Gemini CLI — MCP only, via one owned key in ``~/.gemini/settings.json``."""
    entry = mcp_server_entry(ctx, include_type=False)
    return IntegrationPlan(
        target="gemini-cli",
        label=TARGET_LABELS["gemini-cli"],
        config_dir=gemini_cli_config_dir(ctx),
        mcp_name=MCP_SERVER_NAME,
        config_keys=(
            PlanConfigKey(
                file=gemini_cli_settings_path(ctx),
                key_path=("mcpServers", MCP_SERVER_NAME),
                value=entry,
                fmt="json",
            ),
        ),
    )


_BUILDERS = {
    "claude-code": build_claude_code_plan,
    "codex": build_codex_plan,
    "antigravity": build_antigravity_plan,
    "claude-desktop": build_claude_desktop_plan,
    "gemini-cli": build_gemini_cli_plan,
}


def build_plan_for_target(
    target: str,
    ctx: IntegrationCtx,
    rendered_skill: str,
    reference_doc: str,
) -> IntegrationPlan:
    """Dispatch to the per-target planner."""
    builder = _BUILDERS.get(target)
    if builder is None:
        raise ValueError(f"unknown agent target: {target!r} (known: {', '.join(AGENT_TARGETS)})")
    return builder(ctx, rendered_skill, reference_doc)
