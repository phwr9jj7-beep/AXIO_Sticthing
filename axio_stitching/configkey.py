"""
configkey.py — surgical, reversible edits to a JSON config file we do NOT own.

Some agent apps register MCP servers only in a SHARED config file: Google Antigravity
(`~/.gemini/config/mcp_config.json`), the Gemini CLI (`~/.gemini/settings.json`) and
Claude Desktop (`claude_desktop_config.json`) all work this way. Those files belong to the
user and to other tools, so this module owns exactly ONE key inside one and treats
everything else as untouchable:

  - a parse failure REFUSES (we never try to repair another tool's config),
  - a missing or 0-byte file is treated as ``{}`` (several apps ship it empty),
  - the previous content is backed up once, before the first modification,
  - the write is atomic (tmp + os.replace) so a crash cannot truncate the config,
  - removal happens only while the value still hashes to what we wrote; a value the user
    edited is KEPT and reported as drift.

Deliberately free of any GUI/MCP import so every branch is unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Suffix for the one-time backup of pre-existing content.
CONFIG_BACKUP_SUFFIX = ".axio-stitching.bak"


# ---------------------------------------------------------------------------
# Hashing — stable across re-serialisation
# ---------------------------------------------------------------------------

def canonical(value: Any) -> str:
    """Deterministic JSON encoding of ``value`` (sorted keys, no incidental whitespace)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def hash_value(value: Any) -> str:
    """sha256 of the canonical encoding, so re-serialisation cannot change the hash."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def hash_text(text: str) -> str:
    """sha256 of raw text — used for managed *files* (as opposed to owned keys)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    ok: bool
    #: False when the desired value was already present (idempotent re-install).
    changed: bool = False
    #: sha256 of the value written — record this so uninstall can verify.
    value_sha256: str = ""
    backup: str | None = None
    #: True when this apply CREATED the config file (it did not exist before). Recorded so
    #: uninstall can delete a file we brought into being rather than leave an empty husk.
    file_created: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "value_sha256": self.value_sha256,
            "backup": self.backup,
            "file_created": self.file_created,
            "error": self.error,
        }


@dataclass
class RemoveResult:
    ok: bool
    removed: bool = False
    #: True when the key was left in place because the user had edited it.
    kept_modified: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "removed": self.removed,
            "kept_modified": self.kept_modified,
            "error": self.error,
        }


@dataclass
class ReadResult:
    """Outcome of probing a key without modifying anything."""
    ok: bool
    present: bool = False
    value: Any = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _load(path: Path) -> tuple[bool, dict, str | None]:
    """
    Read a JSON config.

    Returns ``(ok, data, error)``. A missing or whitespace-only file is ``(True, {}, None)``;
    an unparseable file is ``(False, {}, "...")`` — we refuse rather than clobber it.
    """
    if not path.exists():
        return True, {}, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, {}, f"cannot read {path}: {exc}"
    if not raw.strip():
        return True, {}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, {}, f"refusing to edit unparseable JSON at {path}: {exc}"
    if not isinstance(data, dict):
        return False, {}, f"refusing to edit {path}: top level is {type(data).__name__}, not an object"
    return True, data, None


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` via a temp file + replace, so a crash cannot truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    text = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _ensure_backup(path: Path) -> str | None:
    """
    Back up pre-existing content exactly once. Returns the backup path, or None when
    there was nothing to back up (new/empty file) or a backup already exists.
    """
    if not path.exists():
        return None
    backup = path.with_name(path.name + CONFIG_BACKUP_SUFFIX)
    if backup.exists():
        return str(backup)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    backup.write_bytes(raw)
    return str(backup)


def _dig(data: dict, key_path: list[str]) -> tuple[bool, Any]:
    """Walk ``key_path``; return ``(found, value)``."""
    node: Any = data
    for key in key_path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_key(path: Path, key_path: list[str]) -> ReadResult:
    """Probe one owned key without modifying anything."""
    ok, data, error = _load(path)
    if not ok:
        return ReadResult(ok=False, error=error)
    found, value = _dig(data, key_path)
    return ReadResult(ok=True, present=found, value=value)


def apply_key(
    path: Path,
    key_path: list[str],
    value: Any,
    dry_run: bool = False,
) -> ApplyResult:
    """
    Set exactly one nested key in a shared JSON config, leaving every other byte of
    meaning intact.

    Idempotent: when the key already holds the desired value nothing is written and
    ``changed`` is False.
    """
    if not key_path:
        return ApplyResult(ok=False, error="key_path must not be empty")

    ok, data, error = _load(path)
    if not ok:
        return ApplyResult(ok=False, error=error)

    sha = hash_value(value)
    found, existing = _dig(data, key_path)
    if found and hash_value(existing) == sha:
        return ApplyResult(ok=True, changed=False, value_sha256=sha)

    created = not path.exists()
    if dry_run:
        return ApplyResult(ok=True, changed=True, value_sha256=sha, file_created=created)

    backup = _ensure_backup(path)

    node = data
    for key in key_path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[key_path[-1]] = value

    try:
        _atomic_write_json(path, data)
    except OSError as exc:
        return ApplyResult(ok=False, error=f"cannot write {path}: {exc}", backup=backup)

    return ApplyResult(ok=True, changed=True, value_sha256=sha, backup=backup, file_created=created)


def remove_key(
    path: Path,
    key_path: list[str],
    expected_sha256: str | None = None,
    dry_run: bool = False,
    delete_if_empty: bool = False,
) -> RemoveResult:
    """
    Remove one owned key — but only while it still hashes to ``expected_sha256``.

    A value the user has since edited is KEPT and reported via ``kept_modified``; we never
    delete someone else's work. Pass ``expected_sha256=None`` to remove unconditionally
    (used only when the sidecar predates hash recording).

    ``delete_if_empty`` unlinks the whole file when our key was the only thing in it — pass
    it only when the install recorded that it CREATED the file, so uninstalling leaves no
    empty husk where there was nothing before. A pre-existing file is always kept.
    """
    if not key_path:
        return RemoveResult(ok=False, error="key_path must not be empty")
    if not path.exists():
        return RemoveResult(ok=True, removed=False)

    ok, data, error = _load(path)
    if not ok:
        return RemoveResult(ok=False, error=error)

    found, existing = _dig(data, key_path)
    if not found:
        return RemoveResult(ok=True, removed=False)

    if expected_sha256 and hash_value(existing) != expected_sha256:
        return RemoveResult(ok=True, removed=False, kept_modified=True)

    if dry_run:
        return RemoveResult(ok=True, removed=True)

    node = data
    parents: list[tuple[dict, str]] = []
    for key in key_path[:-1]:
        parents.append((node, key))
        node = node[key]
    del node[key_path[-1]]

    # Prune containers we emptied (e.g. an "mcpServers" we were the only entry of) —
    # but only ones that are now empty, never one still holding another tool's server.
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]

    if delete_if_empty and not data:
        try:
            path.unlink()
        except OSError as exc:
            return RemoveResult(ok=False, error=f"cannot remove {path}: {exc}")
        return RemoveResult(ok=True, removed=True)

    try:
        _atomic_write_json(path, data)
    except OSError as exc:
        return RemoveResult(ok=False, error=f"cannot write {path}: {exc}")

    return RemoveResult(ok=True, removed=True)
