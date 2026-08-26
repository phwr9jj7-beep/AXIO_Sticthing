# Changelog

All notable changes to AXIO Stitching Studio.

## 1.1.0 — 2026-08-26

One engine, three surfaces: this release turns the desktop-only Zeiss stitcher into a
vendor-neutral pipeline that is equally drivable by humans (GUI), scripts (`axio` CLI), and
AI agents (MCP server + Agent Skill), shipped by a single Windows installer.

### AI agent integration (PR #1, #4, #5)

- **MCP server** (`axio_stitching.mcp_server`, stdio): 17 typed tools covering environment
  diagnosis (`axio_doctor`), dataset classification and inspection (`axio_detect_source`,
  `axio_inspect_dataset`), pre-flight sizing with an `ok / tight / will_not_fit` verdict
  (`axio_estimate_stitch`), validation, background stitching with job polling and
  cooperative cancellation (`axio_start_stitch` / `axio_job_status` / `axio_job_result` /
  `axio_list_jobs` / `axio_cancel_job`), a synchronous path for tiny jobs
  (`axio_stitch_sync`), result inspection (`axio_read_preview` — an actual image —
  `axio_qc_report`, `axio_list_outputs`), and handoff (`axio_launch_gui`,
  `axio_agent_status`). Binds either MCP SDK generation (1.x `FastMCP` / 2.0 `MCPServer`);
  stdout is kept protocol-clean (all pipeline diagnostics go to stderr).
- **Agent Skill** (`axio-stitching-pipeline`): teaches the inspect → estimate → validate →
  stitch → QC → handoff loop. The installed copy is rendered to drive the MCP tools; the
  repo copy drives the CLI.
- **Cross-platform agent installer** (`axio agent install/uninstall/status`): auto-detects
  and wires Claude Code (CLI + desktop), OpenAI Codex / ChatGPT desktop, Google Antigravity,
  Claude Desktop, and Gemini CLI. Shared config files are edited surgically — one owned key,
  backup-once, atomic writes, TOML byte-preserving splice, hash-verified removal; managed
  directories carry sidecars, and uninstall keeps anything the user edited.
- **Channel/Z recognition**: `axio_inspect_dataset` recognizes multi-color and Z-stack
  datasets in both representations — inside the tile file (multi-page) or as separate files
  (`_cN_` / `_zNN_` filename tags) — and emits `recommendations` with the exact parameters
  to pass (`ref_tag`/`target_tags`, `ref_channel`, `z_mode`).
- **GUI handoff**: `axio_launch_gui` opens the desktop app pre-loaded with the agent's
  dataset, parameters, and the newest stitched preview (via `AXIO_STITCHING_*` environment
  variables), and survives a stale baked app path.

### Vendor-neutral inputs (PR #2)

- New `tile_sources` layer auto-detects and resolves five source types into one tile
  structure: Zeiss `_info.xml`/`_meta.xml`, Fiji/ImageJ `TileConfiguration.txt` (preferring
  the `.registered.txt` refinement), OME-TIFF `Plane PositionX/PositionY` (unit-converted
  via `PhysicalSizeX`), explicit position lists (inline or `.json`), and bare tile folders
  with grid-encoded filenames (`x00_y01`, `r0c1`, `Position012` + `--grid-cols`).
- Grid layouts are flagged `confidence: low` — a starting guess to refine with
  `phase`/`sift`, matching MIST / m2stitch / ASHLAR practice.
- `StitchConfig` gains `source` (file OR directory; `xml_path` retained as an alias) plus
  `overlap`, `grid_cols`, `serpentine`, `pixel_size_um`, `tile_size`.

### CLI & supporting subsystems (PR #1, #2)

- `axio` CLI: `doctor`, `inspect`, `estimate`, `validate`, `stitch`, `qc`, `outputs`,
  `serve`, `version`, and the `agent` sub-app; `--source` on all dataset commands; `--json`
  everywhere.
- `doctor`: environment diagnosis with concrete fixes (packages that gate algorithms, RAM,
  disk, MCP SDK flavour, desktop-app location).
- `estimate`: models the pipeline's real allocations to predict canvas size, peak RAM, disk
  and time before a run.
- `jobs`: background execution with journalling to `~/.axio_stitching/jobs/` and orphan
  detection across server restarts.
- `qc`: memory-bounded mosaic metrics (empty fraction, saturation, percentiles, seam
  prominence) streamed strip-by-strip at gigapixel scale.

### Packaging (PR #3)

- **One fused Windows installer** (`AXIO_Stitching_Studio_<version>_Setup.exe`, Inno Setup,
  per-user, no UAC): desktop GUI + MCP/CLI console executable + shared `_internal` payload
  + an agent-integration checkbox that delegates to `axio agent install`. Uninstall
  deregisters the agent integration before removing files.
- Shared one-directory PyInstaller bundle replaces two one-file EXEs: distributed size
  roughly halved (269 MB vs 2 × 318 MB) and MCP cold start cut from 10–30 s to ~2–3 s.
- `scripts/build_installer.py`: one command from source to installer, with bundle sanity
  checks and the version read from the package.

### Testing

- 353 tests: engine, all five input formats, MCP tool contract and behaviour, installer
  transactional safety (foreign-dir refusal, rollback, drift, hash-verified uninstall),
  channel/Z recognition, QC streaming equivalence, and CLI integration.

## 1.0.0 — 2026-05-25

Initial public release: PySide6 desktop application for Zeiss Axio tile scans with BaSiCPy /
median / spatial shading correction, phase-correlation / SIFT / coordinate registration,
multi-channel, split-channel, and 3D Z-stack support, packaged as a standalone Windows
executable.
