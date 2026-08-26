# Agent integration

AXIO Stitching Studio exposes its pipeline to AI coding agents through two artefacts:

- an **MCP server** (`axio_stitching.mcp_server`) that publishes the pipeline as 17 typed
  tools over stdio, and
- an **Agent Skill** (`skills/axio-stitching-pipeline/`) that teaches an agent how and when
  to use them.

`axio agent install` puts both in front of whichever agent platforms are on the machine.

```bash
axio agent status                        # what is detected, installed, or drifted
axio agent install --dry-run             # every file and config key that would change
axio agent install                       # every platform detected on this machine
axio agent install --target claude-code  # or a comma-separated subset
axio agent uninstall                     # remove only what AXIO wrote, hash-verified
```

Restart the agent app afterwards; MCP servers are read at start-up.

---

## Supported platforms

| Target | Covers | Skill | MCP registration |
|---|---|---|---|
| `claude-code` | Claude Code CLI **and** the Claude Code desktop app (they share `~/.claude`) | `~/.claude/skills/axio-stitching-pipeline/` | plugin file-drop at `~/.claude/skills/axio-stitching/` |
| `codex` | Codex CLI, the Codex IDE extension, and the **ChatGPT desktop app** (its agent runtime *is* Codex) | `~/.codex/skills/axio-stitching-pipeline/` | owned table in `~/.codex/config.toml` |
| `antigravity` | Google Antigravity IDE | inside the plugin at `~/.gemini/config/plugins/axio-stitching/skills/` | owned key in `~/.gemini/config/mcp_config.json` |
| `claude-desktop` | The classic Claude Desktop app | — (no skills directory) | owned key in `claude_desktop_config.json` |
| `gemini-cli` | Gemini CLI | — | owned key in `~/.gemini/settings.json` |

Every path honours the platform's own override environment variable when set:
`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `ANTIGRAVITY_CONFIG_DIR`, `GEMINI_CONFIG_DIR`,
`CLAUDE_DESKTOP_CONFIG`.

### What the server is called

Claude Code namespaces a plugin-provided server, so it appears as
**`plugin:axio-stitching:axio-stitching`**. Everywhere else it is the bare
**`axio-stitching`**. `axio agent status` prints the right name per target.

### Exactly what gets written

<details>
<summary><code>claude-code</code> — a pure file-drop; no shared file is touched</summary>

```
~/.claude/skills/axio-stitching-pipeline/
    SKILL.md
    references/parameters.md
    .axio-stitching-managed.json
~/.claude/skills/axio-stitching/
    .claude-plugin/plugin.json
    .mcp.json
    .axio-stitching-managed.json
```
</details>

<details>
<summary><code>codex</code> — a file-drop plus ONE owned table</summary>

```
~/.codex/skills/axio-stitching-pipeline/
    SKILL.md
    references/parameters.md
    .axio-stitching-managed.json
```

and in the shared `~/.codex/config.toml`:

```toml
[mcp_servers.axio-stitching]
command = "/abs/path/to/python"
args = ["-m", "axio_stitching.mcp_server"]
startup_timeout_sec = 60

[mcp_servers.axio-stitching.env]
PYTHONUTF8 = "1"
PYTHONIOENCODING = "utf-8"
```
</details>

<details>
<summary><code>antigravity</code> — a plugin file-drop plus ONE owned key</summary>

```
~/.gemini/config/plugins/axio-stitching/
    plugin.json
    skills/axio-stitching-pipeline/SKILL.md
    skills/axio-stitching-pipeline/references/parameters.md
    .axio-stitching-managed.json
```

and `mcpServers.axio-stitching` in the shared `~/.gemini/config/mcp_config.json`.

The MCP server is deliberately registered in the **shared user-level** file rather than a
plugin-scoped `mcp_config.json`: the plugin-scoped form is documented but not honoured by the
shipping build, so a server registered there loads the skill and nothing else.
</details>

---

## The safety contract

The installer writes into directories and files that belong to the user and to other tools.
Everything it does is reversible and auditable.

**Managed directories** — created new, and owned outright. Each carries a sidecar
(`.axio-stitching-managed.json`) recording the sha256 of every file written.

- A directory that already exists **without** our sidecar is **refused**, not overwritten —
  it may be a skill you wrote by hand. `--force` overrides.
- A symlinked target is refused rather than followed.
- If any step fails, everything the run created is rolled back. Absence is proven by `lstat`
  before writing, never inferred from a read error — journalling a locked-but-present file as
  "created" would make rollback delete a file the run never wrote.

**Owned keys in shared config files** — exactly one key per file, edited surgically:

- an unparseable config is **refused**; we never try to repair another tool's file,
- the previous content is backed up once, to `<file>.axio-stitching.bak`,
- the write is atomic (temp file + rename), so a crash cannot truncate the config,
- TOML is a byte-preserving **section splice**, so your comments, ordering and formatting
  survive; the result is re-parsed and verified before it is committed,
- on uninstall the key is removed **only while it still hashes to what we wrote** — a value
  you have since edited is kept and reported as drift,
- a config file the installer itself created is deleted on uninstall; one that pre-existed is
  kept with the rest of its content intact.

`axio agent status` reports each artefact as `absent`, `installed`, `drifted` (you edited it),
or `foreign` (it is not ours).

---

## The MCP tools

| Tool | Purpose |
|---|---|
| `axio_doctor` | Environment diagnosis. Call first. |
| `axio_list_algorithms` | Legal values for every parameter, with guidance. |
| `axio_detect_source` | Cheap classification of an unknown dataset (`zeiss`/`fiji`/`ome`/`explicit`/`grid`). |
| `axio_inspect_dataset` | Scenes, tiles, channels, Z, pixel scale — from ANY supported source. Recognizes split-channel (`_cN_`) and filename-tag Z (`_zNN_`) layouts and emits the exact parameters to pass. |
| `axio_estimate_stitch` | Canvas size, peak RAM, disk, rough time, and a fit verdict. |
| `axio_validate_stitch` | Prerequisites and missing-tile check. |
| `axio_start_stitch` | Start a background run; returns a job id. |
| `axio_job_status` | Poll: state, percent, stage, log tail. |
| `axio_job_result` | The finished `StitchResult`. |
| `axio_list_jobs` | Recent jobs, including from earlier sessions. |
| `axio_cancel_job` | Cooperative cancellation at the next stage boundary. |
| `axio_stitch_sync` | Blocking run — small datasets and tests only. |
| `axio_read_preview` | The preview thumbnail as an **image** the agent can see. |
| `axio_qc_report` | Bounded metrics over the mosaic: empty area, clipping, seams. |
| `axio_list_outputs` | What a previous run produced. |
| `axio_launch_gui` | Open the desktop app for the user. |
| `axio_agent_status` | How AXIO is wired into the agent platforms on this machine. |

Long runs are asynchronous by design: a real scene takes minutes to hours, which no MCP client
will wait for synchronously, so `axio_start_stitch` + `axio_job_status` is the normal path and
`axio_stitch_sync` is the exception.

---

## Environment variables the server understands

| Variable | Purpose |
|---|---|
| `AXIO_STITCHING_APP` | Path to the desktop executable, so `axio_launch_gui` can find it. Baked in at install time when known. |
| `AXIO_STITCHING_OUT_DIR` | A default output directory the GUI pre-fills. |
| `PYTHONUTF8` / `PYTHONIOENCODING` | Always set to UTF-8 by the installer. Zeiss datasets routinely carry non-ASCII path components, and a cp932/cp1252 default codepage turns those into `UnicodeDecodeError` deep inside the pipeline. |

---

## Packaged builds — the fused installer

The Windows release is **one installer** (`AXIO_Stitching_Studio_<version>_Setup.exe`, built
by `scripts/build_installer.py` from `installer/AXIO_Stitching_Setup.iss`) that installs a
shared one-directory bundle:

```
%LOCALAPPDATA%\Programs\AXIO Stitching Studio\
    AXIO_Stitching_Studio.exe    - the GUI (windowed)
    AXIO_Stitching_MCP.exe       - MCP server (--mcp-serve) + axio CLI (--cli ...)
    _internal\                   - the payload BOTH executables share (incl. skills/)
```

- **One payload, two executables.** The previous two one-file EXEs each carried a private
  ~318 MB copy of the same payload — and one-file self-extracts it to temp on *every*
  launch, giving the MCP server a 10–30 s cold start each time an agent host spawned it.
  The one-dir bundle starts in ~1–2 s.
- **The MCP server must be the console build.** A Windows windowed executable is linked
  without standard handles, so an stdio server inside one has nothing to read or write.
  Agent hosts launch it with redirected pipes, so no console window appears.
- **Per-user, no UAC** (`PrivilegesRequired=lowest`). Agent configs live in the user
  profile; an elevated installer would wire the *admin's* `~/.claude` instead of the user's.
- **Agent setup is a checkbox**, checked by default: it simply runs
  `AXIO_Stitching_MCP.exe --cli agent install` — the same audited auto-detecting mechanism
  documented above, which skips platforms that aren't installed. Re-run it any time.
- **Uninstall deregisters first**: the uninstaller runs `--cli agent uninstall`
  (hash-verified; user-edited entries are kept) *before* removing files, so no platform
  config is left pointing at a deleted executable.

From an installed copy, `agent install` registers `AXIO_Stitching_MCP.exe --mcp-serve` as
the server command and bakes `AXIO_Stitching_Studio.exe` as `AXIO_STITCHING_APP`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The tools do not appear after installing | The agent app reads MCP config at start-up. Restart it, then `axio agent status`. |
| `No module named 'mcp.server.fastmcp'` | An MCP SDK 2.0 install. The server binds either generation; `axio doctor` reports which. If it says no usable API, `pip install "mcp[cli]>=1.0"`. |
| `axio agent install` refuses a directory | Something is already there without our sidecar. Inspect it; `--force` overwrites. |
| Status says `drifted` | You (or another tool) edited a managed file or the registered value. Re-run `axio agent install` to restore it, or leave it — uninstall will not touch it. |
| Antigravity loads the skill but shows "No MCP Servers" | The server key belongs in the **shared** `~/.gemini/config/mcp_config.json`, which is where the installer puts it. Check `axio agent status --json`. |
| `axio_launch_gui` cannot find the app | Set `AXIO_STITCHING_APP` to the executable and re-run `axio agent install`. |
| The server starts but every path fails with a decode error | `PYTHONUTF8=1` is missing from the registered entry — re-run `axio agent install` rather than hand-editing. |
