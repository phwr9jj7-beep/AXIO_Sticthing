"""
agent_runner.py — the filesystem side of the agent integration (see
:mod:`axio_stitching.agent_integration` for the pure core and the design rationale).

Responsibilities: detect the target agent apps, apply a plan transactionally, report status,
and uninstall from a recorded manifest. Managed FILES are written into NEW directories the
installer owns; the one exception is a plan's ``config_keys`` — single owned keys inside a
SHARED config file — which are applied and removed only through the surgical contract in
:mod:`axio_stitching.configkey` / :mod:`axio_stitching.configkey_toml`.

The safety rules enforced here:

  - refuse to write into a directory that is not ours (no sidecar, or a foreign one),
  - reject a symlinked target rather than following it,
  - record a per-file manifest and, on uninstall, remove ONLY files whose hash still
    matches — a file the user edited is reported and left in place (never a blind rmtree),
  - roll back everything this run created if any step fails.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Callable

from . import __version__, configkey, configkey_toml
from .agent_integration import (
    AGENT_TARGETS,
    MANAGED_BY,
    MANAGED_SIDECAR,
    MANAGED_SIDECAR_VERSION,
    PLUGIN_DIR_NAME,
    SKILL_DIR_NAME,
    TARGET_LABELS,
    IntegrationCtx,
    IntegrationPlan,
    antigravity_config_dir,
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
    gemini_cli_config_dir,
    gemini_cli_settings_path,
    pathmod_for,
    render_installed_skill,
)

# ---------------------------------------------------------------------------
# Format dispatch — the per-format surgery module
# ---------------------------------------------------------------------------

_SURGERY = {"json": configkey, "toml": configkey_toml}


def _surgery(fmt: str | None):
    """An absent format in an older sidecar means 'json' — the only format that existed then."""
    return _SURGERY.get(fmt or "json", configkey)


# ---------------------------------------------------------------------------
# Context construction
# ---------------------------------------------------------------------------

#: The console-mode sibling executable a packaged build ships for headless work.
MCP_EXECUTABLE_STEMS = ("AXIO_Stitching_MCP",)
#: The windowed desktop executable.
GUI_EXECUTABLE_STEMS = ("AXIO_Stitching_Studio",)


def _sibling_executable(current: Path, stems: tuple[str, ...]) -> Path | None:
    """A sibling build artefact by stem, matching the current executable's suffix."""
    for stem in stems:
        candidate = current.with_name(stem + current.suffix)
        if candidate.exists():
            return candidate
    return None


def _default_exec() -> tuple[str, tuple[str, ...], str | None]:
    """
    ``(command, args, app_path)`` — how to launch the MCP server, and where the desktop app is.

    A source/venv install uses the interpreter running right now, because that is the one
    that actually has the package importable.

    A frozen build has no ``python`` to call, so an executable serves as the launcher via its
    own ``--mcp-serve`` flag. It must be the **console** build: a Windows windowed executable
    is linked without standard handles, so an stdio server started from the GUI binary would
    have nothing to read or write. When the console sibling is present we register that;
    otherwise we fall back to the running executable and accept the risk rather than refuse
    to install.
    """
    if not getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), ("-m", "axio_stitching.mcp_server"), None

    current = Path(sys.executable).resolve()
    server = current if current.stem in MCP_EXECUTABLE_STEMS else (
        _sibling_executable(current, MCP_EXECUTABLE_STEMS) or current
    )
    gui = current if current.stem in GUI_EXECUTABLE_STEMS else (
        _sibling_executable(current, GUI_EXECUTABLE_STEMS) or current
    )
    return str(server), ("--mcp-serve",), str(gui)


def find_app_path() -> str | None:
    """
    Locate the AXIO Stitching Studio desktop executable, or the GUI entry script in a source
    checkout. Returns None when neither is found — the caller must then omit the env var
    rather than bake a guess.
    """
    override = os.environ.get("AXIO_STITCHING_APP")
    if override and override.strip() and Path(override).exists():
        return str(Path(override).resolve())

    if getattr(sys, "frozen", False):
        current = Path(sys.executable).resolve()
        return str(_sibling_executable(current, GUI_EXECUTABLE_STEMS) or current)

    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        # One-dir bundle (the current spec: shared _internal, two executables).
        repo_root / "dist" / "AXIO_Stitching_Studio" / "AXIO_Stitching_Studio.exe",
        # Legacy one-file build.
        repo_root / "dist" / "AXIO_Stitching_Studio.exe",
        # Source checkout.
        repo_root / "scripts" / "axio_launcher.py",
        repo_root / "scripts" / "gui_stitch.py",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def make_ctx(
    *,
    env: dict[str, str] | None = None,
    home: str | None = None,
    platform: str | None = None,
    exec_path: str | None = None,
    exec_args: tuple[str, ...] | None = None,
    app_path: str | None = None,
    default_out_dir: str | None = None,
) -> IntegrationCtx:
    """Build the pure :class:`IntegrationCtx` from the real environment (overridable for tests)."""
    plat = platform or sys.platform
    default_exec_path, default_exec_args, frozen_app = _default_exec()
    return IntegrationCtx(
        platform=plat,
        home=home or str(Path.home()),
        env=dict(env if env is not None else os.environ),
        pathmod=pathmod_for(plat),
        exec_path=exec_path or default_exec_path,
        exec_args=exec_args or default_exec_args,
        app_path=app_path or frozen_app or find_app_path(),
        default_out_dir=default_out_dir,
    )


# ---------------------------------------------------------------------------
# Bundled skill sources
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def source_skill_dir() -> Path:
    """
    Where the shipped source skill lives, across all three shapes this package ships in:

    * a PyInstaller bundle — ``sys._MEIPASS/skills/<name>`` (see the .spec's ``datas``),
    * an installed wheel — ``<package>/_skills/<name>`` (see pyproject's ``force-include``),
    * a source checkout — ``<repo>/skills/<name>``.

    The last candidate is returned even when it does not exist, so the caller's fallback
    (a generated stub) has a path to name in its error.
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(getattr(sys, "_MEIPASS", _repo_root())) / "skills" / SKILL_DIR_NAME)
    candidates.append(Path(__file__).resolve().parent / "_skills" / SKILL_DIR_NAME)
    candidates.append(_repo_root() / "skills" / SKILL_DIR_NAME)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def load_reference_doc() -> str:
    """
    The parameter reference shipped alongside the installed skill. Falls back to a short
    generated stub so an install never fails merely because the repo doc is missing.
    """
    candidate = source_skill_dir() / "references" / "parameters.md"
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return (
            "# AXIO Stitching - parameter reference\n\n"
            "Call the `axio_list_algorithms` MCP tool for the authoritative list of legal\n"
            "values for `correction`, `algorithm`, `alignment_mode` and `z_mode`.\n"
        )


def load_source_skill_frontmatter() -> tuple[str, str]:
    """``(name, description)`` from the shipped source skill, so the installed render inherits it."""
    from .agent_integration import DEFAULT_SKILL_DESCRIPTION, parse_skill_frontmatter

    try:
        raw = (source_skill_dir() / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return SKILL_DIR_NAME, DEFAULT_SKILL_DESCRIPTION
    front = parse_skill_frontmatter(raw)
    return front.get("name") or SKILL_DIR_NAME, front.get("description") or DEFAULT_SKILL_DESCRIPTION


def build_plan(ctx: IntegrationCtx, target: str) -> IntegrationPlan:
    """Render the skill (running its self-check) and build the plan for ``target``."""
    name, description = load_source_skill_frontmatter()
    rendered = render_installed_skill(name, description)
    return build_plan_for_target(target, ctx, rendered, load_reference_doc())


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    target: str
    label: str
    installed: bool
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "label": self.label,
            "detected": self.installed,
            "evidence": self.evidence,
        }


def _exists(path: str) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def detect_target(target: str, ctx: IntegrationCtx) -> Detection:
    """
    Is this agent app present on the machine? Evidence names WHAT was found, because several
    of these roots are shared with other tooling and the caller should be able to see why we
    concluded the app is installed.
    """
    label = TARGET_LABELS.get(target, target)
    evidence: list[str] = []

    if target == "claude-code":
        cfg = claude_config_dir(ctx)
        if _exists(cfg):
            evidence.append(f"config dir: {cfg}")
        legacy = ctx.join(ctx.home, ".claude.json")
        if _exists(legacy):
            evidence.append(f"config file: {legacy}")
    elif target == "codex":
        root = codex_config_dir(ctx)
        if _exists(root):
            evidence.append(f"root: {root}")
        toml_path = codex_config_toml_path(ctx)
        if _exists(toml_path):
            evidence.append(f"config: {toml_path}")
    elif target == "antigravity":
        cfg = antigravity_config_dir(ctx)
        if _exists(cfg):
            evidence.append(f"config dir: {cfg}")
        mcp_path = antigravity_mcp_config_path(ctx)
        if _exists(mcp_path):
            evidence.append(f"mcp registry: {mcp_path}")
    elif target == "claude-desktop":
        cfg_file = claude_desktop_config_path(ctx)
        parent = str(ctx.pathmod(cfg_file).parent)
        if _exists(cfg_file):
            evidence.append(f"config: {cfg_file}")
        elif _exists(parent):
            evidence.append(f"app data dir: {parent}")
    elif target == "gemini-cli":
        root = gemini_cli_config_dir(ctx)
        settings = gemini_cli_settings_path(ctx)
        if _exists(settings):
            evidence.append(f"settings: {settings}")
        elif _exists(root):
            evidence.append(f"root: {root}")
    else:
        raise ValueError(f"unknown agent target: {target!r}")

    return Detection(target=target, label=label, installed=bool(evidence), evidence=evidence)


def detect_all(ctx: IntegrationCtx) -> list[Detection]:
    return [detect_target(t, ctx) for t in AGENT_TARGETS]


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------

def sidecar_path(unit_dir: str) -> Path:
    return Path(unit_dir) / MANAGED_SIDECAR


def read_sidecar(unit_dir: str) -> dict | None:
    """The sidecar we wrote, or None when absent/unreadable/foreign."""
    path = sidecar_path(unit_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("managedBy") != MANAGED_BY:
        return None
    return data


def is_foreign_dir(unit_dir: str) -> bool:
    """True when the directory exists and is NOT one of ours, so we must not overwrite it."""
    path = Path(unit_dir)
    if not path.exists():
        return False
    if path.is_symlink():
        return True
    return read_sidecar(unit_dir) is None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

UnitState = str  # 'absent' | 'installed' | 'drifted' | 'foreign'


@dataclass
class UnitStatus:
    kind: str
    dir: str
    state: UnitState
    detail: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "dir": self.dir, "state": self.state, "detail": self.detail}


@dataclass
class KeyStatus:
    file: str
    key_path: list[str]
    fmt: str
    state: UnitState
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "key": ".".join(self.key_path),
            "format": self.fmt,
            "state": self.state,
            "detail": self.detail,
        }


@dataclass
class StatusReport:
    target: str
    label: str
    detected: bool
    evidence: list[str]
    mcp_name: str
    config_dir: str
    units: list[UnitStatus] = field(default_factory=list)
    keys: list[KeyStatus] = field(default_factory=list)

    @property
    def state(self) -> UnitState:
        """One word for the whole target."""
        states = [u.state for u in self.units] + [k.state for k in self.keys]
        if not states:
            return "absent"
        if "foreign" in states:
            return "foreign"
        if "drifted" in states:
            return "drifted"
        if all(s == "installed" for s in states):
            return "installed"
        if all(s == "absent" for s in states):
            return "absent"
        return "drifted"

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "label": self.label,
            "detected": self.detected,
            "evidence": self.evidence,
            "state": self.state,
            "mcp_name": self.mcp_name,
            "config_dir": self.config_dir,
            "units": [u.to_dict() for u in self.units],
            "keys": [k.to_dict() for k in self.keys],
        }


def status(ctx: IntegrationCtx, target: str) -> StatusReport:
    """Report what is installed for ``target``, and whether the user has since edited it."""
    plan = build_plan(ctx, target)
    detection = detect_target(target, ctx)
    report = StatusReport(
        target=target,
        label=plan.label,
        detected=detection.installed,
        evidence=detection.evidence,
        mcp_name=plan.mcp_name,
        config_dir=plan.config_dir,
    )

    for unit in plan.units:
        directory = Path(unit.dir)
        if not directory.exists():
            report.units.append(UnitStatus(unit.kind, unit.dir, "absent"))
            continue
        if directory.is_symlink():
            report.units.append(
                UnitStatus(unit.kind, unit.dir, "foreign", "path is a symlink; refusing to manage it")
            )
            continue
        sidecar = read_sidecar(unit.dir)
        if sidecar is None:
            report.units.append(
                UnitStatus(unit.kind, unit.dir, "foreign", "directory exists but was not created by AXIO")
            )
            continue

        drift: list[str] = []
        for record in sidecar.get("files", []):
            rel = record.get("rel", "")
            file_path = directory.joinpath(*rel.split("/"))
            if not file_path.exists():
                drift.append(f"{rel}: removed")
                continue
            try:
                actual = configkey.hash_text(file_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                drift.append(f"{rel}: unreadable")
                continue
            if actual != record.get("sha256"):
                drift.append(f"{rel}: edited")
        # A shipped file the current version adds but the recorded install predates.
        recorded = {r.get("rel") for r in sidecar.get("files", [])}
        for planned in unit.files:
            if planned.rel not in recorded:
                drift.append(f"{planned.rel}: missing (older install)")

        if drift:
            report.units.append(UnitStatus(unit.kind, unit.dir, "drifted", "; ".join(drift)))
        else:
            report.units.append(UnitStatus(unit.kind, unit.dir, "installed"))

    for key in plan.config_keys:
        module = _surgery(key.fmt)
        result = module.read_key(Path(key.file), list(key.key_path))
        if not result.ok:
            report.keys.append(
                KeyStatus(key.file, list(key.key_path), key.fmt, "foreign", result.error or "unreadable")
            )
            continue
        if not result.present:
            report.keys.append(KeyStatus(key.file, list(key.key_path), key.fmt, "absent"))
            continue
        if key.fmt == "toml" and not configkey_toml.toml_reader_available():
            report.keys.append(
                KeyStatus(key.file, list(key.key_path), key.fmt, "installed", "present (value not verified: no TOML reader)")
            )
            continue
        if configkey.hash_value(result.value) == configkey.hash_value(key.value):
            report.keys.append(KeyStatus(key.file, list(key.key_path), key.fmt, "installed"))
        else:
            report.keys.append(
                KeyStatus(key.file, list(key.key_path), key.fmt, "drifted", "value differs from what AXIO would write")
            )

    return report


def status_all(ctx: IntegrationCtx) -> list[StatusReport]:
    return [status(ctx, t) for t in AGENT_TARGETS]


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

@dataclass
class InstallResult:
    target: str
    label: str
    ok: bool
    changed: bool = False
    dry_run: bool = False
    mcp_name: str = ""
    written: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "label": self.label,
            "ok": self.ok,
            "changed": self.changed,
            "dry_run": self.dry_run,
            "mcp_name": self.mcp_name,
            "written": self.written,
            "keys": self.keys,
            "backups": self.backups,
            "skipped": self.skipped,
            "notes": self.notes,
            "error": self.error,
        }


class _Journal:
    """
    Pre-run state of every path this run touches, so a failure can be rolled back exactly.

    Absence is proven by ``lstat`` — NEVER inferred from a read error. Journaling a
    present-but-unreadable file (antivirus lock, permissions) as "created" would make
    rollback DELETE a file this run never wrote, so that case aborts the install before
    anything at the path is touched.
    """

    def __init__(self) -> None:
        self._prior: dict[Path, bytes | None] = {}
        self._created_dirs: list[Path] = []

    def record(self, path: Path) -> None:
        if path in self._prior:
            return
        try:
            path.lstat()
        except FileNotFoundError:
            self._prior[path] = None
            return
        except OSError as exc:
            raise OSError(f"cannot inspect {path} before writing it: {exc}") from exc
        self._prior[path] = path.read_bytes()

    def record_dir(self, path: Path) -> None:
        self._created_dirs.append(path)

    def rollback(self) -> None:
        for path, prior in self._prior.items():
            try:
                if prior is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_bytes(prior)
            except OSError:
                pass
        for path in reversed(self._created_dirs):
            try:
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass


def _previously_created_files(ctx: IntegrationCtx, plan: IntegrationPlan) -> set[str]:
    """Shared config files a previous install of this target recorded as having created."""
    records: list[dict] = []
    for unit in plan.units:
        sidecar = read_sidecar(unit.dir)
        if sidecar:
            records.extend(sidecar.get("configKeys", []))
    standalone = _keys_only_sidecar_path(ctx, plan.target)
    if standalone.exists():
        try:
            data = json.loads(standalone.read_text(encoding="utf-8"))
            if data.get("managedBy") == MANAGED_BY:
                records.extend(data.get("configKeys", []))
        except (OSError, json.JSONDecodeError):
            pass
    return {r["file"] for r in records if r.get("fileCreated") and r.get("file")}


def _content_differs(path: Path, expected_sha256: str) -> bool:
    """True when ``path`` is absent, unreadable, or holds something other than what we plan to write."""
    try:
        return configkey.hash_text(path.read_text(encoding="utf-8")) != expected_sha256
    except (OSError, UnicodeDecodeError):
        return True


def _write_sidecar(unit_dir: Path, kind: str, files: list[dict], config_keys: list[dict]) -> Path:
    """
    The marker that the install COMPLETED. It records the file hashes and the shared-config
    keys this run owns (with the hash of the value actually applied), so status can verify
    and uninstall can remove exactly what was written.
    """
    payload = {
        "managedBy": MANAGED_BY,
        "kind": kind,
        "version": MANAGED_SIDECAR_VERSION,
        "appVersion": __version__,
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": files,
        "configKeys": config_keys,
    }
    path = unit_dir / MANAGED_SIDECAR
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def install(ctx: IntegrationCtx, target: str, dry_run: bool = False, force: bool = False) -> InstallResult:
    """
    Apply the plan for ``target`` transactionally.

    ``force`` allows overwriting a directory that exists but carries no sidecar. Without it
    such a directory is refused — it may be a hand-made skill of the user's.
    """
    try:
        plan = build_plan(ctx, target)
    except Exception as exc:
        return InstallResult(target=target, label=TARGET_LABELS.get(target, target), ok=False, error=str(exc))

    result = InstallResult(
        target=target, label=plan.label, ok=True, dry_run=dry_run, mcp_name=plan.mcp_name
    )
    journal = _Journal()

    # "We created this config file" is a fact about the FIRST install and must survive every
    # idempotent re-install, otherwise uninstall would leave an empty husk behind. Read it
    # BEFORE the units loop, which rewrites the sidecar that records it.
    previously_created = _previously_created_files(ctx, plan)

    try:
        # ---- Managed directories (pure file-drop) --------------------------------
        for unit in plan.units:
            unit_dir = Path(unit.dir)
            if is_foreign_dir(unit.dir) and not force:
                reason = (
                    "path is a symlink" if unit_dir.is_symlink()
                    else "directory exists but was not created by AXIO"
                )
                result.ok = False
                result.error = f"refusing to write {unit.dir}: {reason} (use --force to override)"
                journal.rollback()
                return result

            # Carry forward any owned keys this unit's sidecar already records, so a failure
            # between here and the key-application step below cannot lose them.
            prior_keys = (read_sidecar(unit.dir) or {}).get("configKeys", [])
            file_records: list[dict] = []
            for planned in unit.files:
                file_path = unit_dir.joinpath(*planned.rel.split("/"))
                planned_sha = configkey.hash_text(planned.content)
                file_records.append({"rel": planned.rel, "sha256": planned_sha})
                # Only a file whose content actually differs counts as a change, so a
                # re-install of an unchanged version reports `changed=False` truthfully.
                needs_write = _content_differs(file_path, planned_sha)
                if dry_run:
                    if needs_write:
                        result.written.append(str(file_path))
                        result.changed = True
                    else:
                        result.notes.append(f"{file_path} already up to date")
                    continue
                if not needs_write:
                    result.notes.append(f"{file_path} already up to date")
                    continue
                journal.record(file_path)
                if not file_path.parent.exists():
                    for ancestor in reversed(file_path.parent.parents):
                        if not ancestor.exists() and str(ancestor).startswith(str(unit_dir.parent)):
                            journal.record_dir(ancestor)
                    journal.record_dir(file_path.parent)
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(planned.content, encoding="utf-8", newline="\n")
                result.written.append(str(file_path))
                result.changed = True

            if not dry_run:
                sidecar = _write_sidecar(unit_dir, unit.kind, file_records, prior_keys)
                journal.record(sidecar)

        # ---- Owned keys in shared config files -----------------------------------
        applied_keys: list[dict] = []
        for key in plan.config_keys:
            module = _surgery(key.fmt)
            outcome = module.apply_key(Path(key.file), list(key.key_path), key.value, dry_run=dry_run)
            if not outcome.ok:
                result.ok = False
                result.error = outcome.error
                journal.rollback()
                _rollback_keys(applied_keys)
                return result
            record = {
                "file": key.file,
                "keyPath": list(key.key_path),
                "format": key.fmt,
                "valueSha256": outcome.value_sha256,
                "fileCreated": outcome.file_created or key.file in previously_created,
            }
            applied_keys.append(record)
            label = f"{key.file} :: {'.'.join(key.key_path)}"
            result.keys.append(label)
            if outcome.changed:
                result.changed = True
            else:
                result.notes.append(f"{label} already up to date")
            if outcome.backup:
                result.backups.append(outcome.backup)

        # Record owned keys on a unit sidecar when the target has one; otherwise keep a
        # standalone sidecar next to the config dir so uninstall can still find them.
        if applied_keys and not dry_run:
            if plan.units:
                unit_dir = Path(plan.units[0].dir)
                sidecar = read_sidecar(str(unit_dir)) or {}
                sidecar_files = sidecar.get("files", [])
                _write_sidecar(unit_dir, plan.units[0].kind, sidecar_files, applied_keys)
            else:
                _write_keys_only_sidecar(ctx, plan, applied_keys)

    except Exception as exc:  # noqa: BLE001 - any failure must roll back, then be reported
        journal.rollback()
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    return result


def _keys_only_sidecar_path(ctx: IntegrationCtx, target: str) -> Path:
    """Where a keys-only target (Claude Desktop, Gemini CLI) records what it owns."""
    return Path(ctx.home) / ".axio_stitching" / "agents" / f"{target}{MANAGED_SIDECAR}"


def _write_keys_only_sidecar(ctx: IntegrationCtx, plan: IntegrationPlan, keys: list[dict]) -> Path:
    path = _keys_only_sidecar_path(ctx, plan.target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "managedBy": MANAGED_BY,
        "kind": f"{plan.target}-mcp-key",
        "version": MANAGED_SIDECAR_VERSION,
        "appVersion": __version__,
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": [],
        "configKeys": keys,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def _rollback_keys(applied: list[dict]) -> None:
    for record in applied:
        module = _surgery(record.get("format"))
        try:
            module.remove_key(
                Path(record["file"]), list(record["keyPath"]), record.get("valueSha256")
            )
        except Exception:  # noqa: BLE001 - best-effort rollback
            pass


def install_all(
    ctx: IntegrationCtx,
    targets: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    detected_only: bool = True,
) -> list[InstallResult]:
    """
    Install to ``targets`` (default: every target detected on this machine).

    ``detected_only`` skips agents that are not installed, which is what makes a blanket
    "set up everything" safe to run — it never creates a config directory for an app the
    user does not have.
    """
    chosen = list(targets) if targets else list(AGENT_TARGETS)
    results: list[InstallResult] = []
    for target in chosen:
        detection = detect_target(target, ctx)
        if detected_only and not targets and not detection.installed:
            results.append(
                InstallResult(
                    target=target,
                    label=detection.label,
                    ok=True,
                    changed=False,
                    dry_run=dry_run,
                    skipped=["not detected on this machine"],
                )
            )
            continue
        results.append(install(ctx, target, dry_run=dry_run, force=force))
    return results


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

@dataclass
class UninstallResult:
    target: str
    label: str
    ok: bool
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "label": self.label,
            "ok": self.ok,
            "removed": self.removed,
            "kept": self.kept,
            "error": self.error,
        }


def uninstall(ctx: IntegrationCtx, target: str, dry_run: bool = False) -> UninstallResult:
    """
    Remove what we installed, and only that.

    Files are removed while their hash still matches the sidecar; anything the user edited is
    KEPT and reported. Owned keys are removed through the same hash-verified surgery.
    """
    try:
        plan = build_plan(ctx, target)
    except Exception as exc:
        return UninstallResult(target=target, label=TARGET_LABELS.get(target, target), ok=False, error=str(exc))

    result = UninstallResult(target=target, label=plan.label, ok=True)
    key_records: list[dict] = []

    for unit in plan.units:
        unit_dir = Path(unit.dir)
        sidecar = read_sidecar(unit.dir)
        if sidecar is None:
            if unit_dir.exists():
                result.kept.append(f"{unit.dir} (not ours - no AXIO sidecar)")
            continue
        key_records.extend(sidecar.get("configKeys", []))

        for record in sidecar.get("files", []):
            rel = record.get("rel", "")
            file_path = unit_dir.joinpath(*rel.split("/"))
            if not file_path.exists():
                continue
            try:
                actual = configkey.hash_text(file_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                result.kept.append(f"{file_path} (unreadable)")
                continue
            if actual != record.get("sha256"):
                result.kept.append(f"{file_path} (edited since install)")
                continue
            if dry_run:
                result.removed.append(str(file_path))
                continue
            try:
                file_path.unlink()
                result.removed.append(str(file_path))
            except OSError as exc:
                result.kept.append(f"{file_path} ({exc})")

        sidecar_file = sidecar_path(unit.dir)
        if sidecar_file.exists() and not dry_run:
            try:
                sidecar_file.unlink()
                result.removed.append(str(sidecar_file))
            except OSError:
                pass

        if not dry_run:
            _prune_empty(unit_dir)

    # Keys recorded on a keys-only sidecar (targets with no managed directory).
    standalone = _keys_only_sidecar_path(ctx, target)
    if standalone.exists():
        try:
            data = json.loads(standalone.read_text(encoding="utf-8"))
            if data.get("managedBy") == MANAGED_BY:
                key_records.extend(data.get("configKeys", []))
        except (OSError, json.JSONDecodeError):
            pass

    # Fall back to the plan's own keys when no sidecar recorded them (older install).
    if not key_records:
        key_records = [
            {
                "file": k.file,
                "keyPath": list(k.key_path),
                "format": k.fmt,
                "valueSha256": None,
                "fileCreated": False,
            }
            for k in plan.config_keys
        ]

    seen: set[tuple[str, str]] = set()
    for record in key_records:
        identity = (record.get("file", ""), ".".join(record.get("keyPath", [])))
        if identity in seen:
            continue
        seen.add(identity)
        module = _surgery(record.get("format"))
        outcome = module.remove_key(
            Path(record["file"]),
            list(record["keyPath"]),
            record.get("valueSha256"),
            dry_run=dry_run,
            delete_if_empty=bool(record.get("fileCreated")),
        )
        label = f"{record['file']} :: {identity[1]}"
        if not outcome.ok:
            result.ok = False
            result.error = outcome.error
        elif outcome.kept_modified:
            result.kept.append(f"{label} (edited since install)")
        elif outcome.removed:
            result.removed.append(label)

    if standalone.exists() and not dry_run:
        try:
            standalone.unlink()
        except OSError:
            pass

    return result


def _prune_empty(directory: Path) -> None:
    """Post-order removal of the directory and any empty ancestors we created."""
    if not directory.exists():
        return
    for child in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass
    try:
        directory.rmdir()
    except OSError:
        return
    # Prune the container (e.g. an empty `plugins/` we created) but never above it.
    parent = directory.parent
    try:
        if parent.exists() and not any(parent.iterdir()) and parent.name in {"plugins", "skills"}:
            parent.rmdir()
    except OSError:
        pass


def uninstall_all(ctx: IntegrationCtx, targets: list[str] | None = None, dry_run: bool = False) -> list[UninstallResult]:
    return [uninstall(ctx, t, dry_run=dry_run) for t in (targets or list(AGENT_TARGETS))]
