"""
configkey_toml.py — the TOML variant of :mod:`axio_stitching.configkey`.

OpenAI Codex (the Codex CLI, its IDE extension, and the ChatGPT desktop app whose agent
runtime IS Codex) registers MCP servers as ``[mcp_servers.<id>]`` tables inside the SHARED
``$CODEX_HOME/config.toml``. That file belongs to the user, so the same safety contract as
the JSON module applies — expressed as a **byte-preserving section splice** rather than a
parse-and-reserialise, because reserialising TOML would destroy the user's comments,
ordering and formatting:

  - only the lines of OUR table (and its child tables) are ever rewritten,
  - a missing or empty file is treated as empty,
  - the previous content is backed up once, before the first modification,
  - the write is atomic (tmp + os.replace),
  - the result is re-parsed and verified before being reported as applied (when a TOML
    reader is available — ``tomllib`` on 3.11+, ``tomli`` otherwise),
  - removal happens only while the section still matches what we wrote.

Header matching is SEMANTIC, not textual: ``[mcp_servers."axio-stitching"]``,
``[mcp_servers.'axio-stitching']`` and ``[ mcp_servers . "axio-stitching" ]`` are the same
table, and all three are recognised.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .configkey import (  # re-exported result shapes keep the two modules interchangeable
    CONFIG_BACKUP_SUFFIX,
    ApplyResult,
    ReadResult,
    RemoveResult,
    hash_value,
)

# ---------------------------------------------------------------------------
# Optional TOML reader (verification only — the splice itself never needs one)
# ---------------------------------------------------------------------------

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]


def toml_reader_available() -> bool:
    """True when this interpreter can parse TOML for post-write verification."""
    return _toml is not None


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^\s*(\[\[?)([^\]]*)(\]\]?)\s*(?:#.*)?$")


def _split_dotted(raw: str) -> list[str] | None:
    """
    Split a TOML dotted key into its parts, honouring quoting.

    Returns None when the text is not a well-formed dotted key (in which case the caller
    treats the header as an opaque boundary rather than trying to interpret it).
    """
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(raw)
    expecting_sep = False
    while i < n:
        ch = raw[i]
        if ch in " \t":
            i += 1
            continue
        if ch == ".":
            if not expecting_sep and not buf:
                return None
            parts.append("".join(buf))
            buf = []
            expecting_sep = False
            i += 1
            continue
        if expecting_sep:
            return None
        if ch == '"':
            j = i + 1
            out: list[str] = []
            while j < n and raw[j] != '"':
                if raw[j] == "\\" and j + 1 < n:
                    esc = raw[j + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(esc, esc))
                    j += 2
                    continue
                out.append(raw[j])
                j += 1
            if j >= n:
                return None
            buf = out
            i = j + 1
            expecting_sep = True
            continue
        if ch == "'":
            j = raw.find("'", i + 1)
            if j < 0:
                return None
            buf = list(raw[i + 1 : j])
            i = j + 1
            expecting_sep = True
            continue
        m = re.match(r"[A-Za-z0-9_-]+", raw[i:])
        if not m:
            return None
        buf = list(m.group(0))
        i += m.end()
        expecting_sep = True
    if not buf and not parts:
        return None
    parts.append("".join(buf))
    return parts


@dataclass(frozen=True)
class _Header:
    line_no: int
    key_path: list[str] | None  # None => unparseable / array-of-tables
    is_array: bool


def _scan_headers(lines: list[str]) -> list[_Header]:
    """Find every table header in the file, in order."""
    headers: list[_Header] = []
    in_multiline_string = False
    delim = ""
    for idx, line in enumerate(lines):
        # Skip over multi-line basic/literal strings so a "[" inside one is not read as a header.
        if in_multiline_string:
            if delim in line:
                in_multiline_string = False
            continue
        stripped = line.strip()
        for d in ('"""', "'''"):
            if stripped.count(d) % 2 == 1:
                in_multiline_string = True
                delim = d
                break
        if in_multiline_string:
            continue
        m = _HEADER_RE.match(line)
        if not m:
            continue
        open_br, body, close_br = m.group(1), m.group(2), m.group(3)
        if len(open_br) != len(close_br):
            continue
        is_array = open_br == "[["
        key_path = None if is_array else _split_dotted(body)
        headers.append(_Header(line_no=idx, key_path=key_path, is_array=is_array))
    return headers


def _is_descendant(candidate: list[str] | None, ancestor: list[str]) -> bool:
    return (
        candidate is not None
        and len(candidate) > len(ancestor)
        and candidate[: len(ancestor)] == ancestor
    )


def find_section(text: str, key_path: list[str]) -> tuple[int, int] | None:
    """
    Locate our table's line span ``[start, end)`` — the header plus its body plus every
    CHILD table (e.g. ``[mcp_servers."axio-stitching".env]``), stopping at the first
    header that is neither ours nor a descendant of ours.

    Returns None when the table is absent.
    """
    lines = text.splitlines()
    headers = _scan_headers(lines)
    for pos, header in enumerate(headers):
        if header.is_array or header.key_path != key_path:
            continue
        start = header.line_no
        end = len(lines)
        for later in headers[pos + 1 :]:
            if _is_descendant(later.key_path, key_path):
                continue
            end = later.line_no
            break
        return start, end
    return None


# ---------------------------------------------------------------------------
# Value rendering
# ---------------------------------------------------------------------------

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _render_key(key: str) -> str:
    if _BARE_KEY_RE.match(key):
        return key
    return _render_string(key)


def _render_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _render_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_scalar(v) for v in value) + "]"
    raise TypeError(f"unsupported TOML scalar: {type(value).__name__}")


def render_section(key_path: list[str], value: dict) -> str:
    """
    Render ``value`` as a TOML table (plus child tables for nested dicts).

    Emitted deterministically — scalars first, then child tables — so an unchanged install
    re-renders byte-identically and reports ``changed=False``.
    """
    if not isinstance(value, dict):
        raise TypeError("a TOML owned key must be a table (dict)")
    dotted = ".".join(_render_key(k) for k in key_path)
    out = [f"[{dotted}]"]
    children: list[tuple[str, dict]] = []
    for key, val in value.items():
        if isinstance(val, dict):
            children.append((key, val))
            continue
        out.append(f"{_render_key(key)} = {_render_scalar(val)}")
    text = "\n".join(out) + "\n"
    for key, val in children:
        text += "\n" + render_section(key_path + [key], val)
    return text


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> tuple[bool, str, str | None]:
    if not path.exists():
        return True, "", None
    try:
        return True, path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return False, "", f"cannot read {path}: {exc}"
    except UnicodeDecodeError as exc:
        return False, "", f"refusing to edit non-UTF-8 TOML at {path}: {exc}"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _ensure_backup(path: Path) -> str | None:
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


def _parse(text: str) -> tuple[bool, dict, str | None]:
    if _toml is None:  # pragma: no cover - only on 3.10 without tomli
        return True, {}, None
    try:
        return True, _toml.loads(text), None
    except Exception as exc:  # tomllib raises TOMLDecodeError
        return False, {}, str(exc)


def _dig(data: dict, key_path: list[str]) -> tuple[bool, Any]:
    node: Any = data
    for key in key_path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


# ---------------------------------------------------------------------------
# Public API — mirrors configkey.py
# ---------------------------------------------------------------------------

def read_key(path: Path, key_path: list[str]) -> ReadResult:
    """Probe one owned table without modifying anything."""
    ok, text, error = _read_text(path)
    if not ok:
        return ReadResult(ok=False, error=error)
    parsed_ok, data, parse_err = _parse(text)
    if not parsed_ok:
        return ReadResult(ok=False, error=f"refusing to read unparseable TOML at {path}: {parse_err}")
    if _toml is None:  # structural fallback
        span = find_section(text, key_path)
        return ReadResult(ok=True, present=span is not None, value=None)
    found, value = _dig(data, key_path)
    return ReadResult(ok=True, present=found, value=value)


def apply_key(
    path: Path,
    key_path: list[str],
    value: dict,
    dry_run: bool = False,
) -> ApplyResult:
    """
    Splice exactly one ``[a.b]`` table into a shared TOML config, preserving every other
    byte of the file (comments, ordering, formatting included).
    """
    if not key_path:
        return ApplyResult(ok=False, error="key_path must not be empty")

    ok, text, error = _read_text(path)
    if not ok:
        return ApplyResult(ok=False, error=error)

    parsed_ok, _data, parse_err = _parse(text)
    if not parsed_ok:
        return ApplyResult(
            ok=False,
            error=f"refusing to edit unparseable TOML at {path}: {parse_err}",
        )

    try:
        section = render_section(key_path, value)
    except TypeError as exc:
        return ApplyResult(ok=False, error=str(exc))

    sha = hash_value(value)
    lines = text.splitlines(keepends=True)
    span = find_section(text, key_path)

    if span is not None:
        current = "".join(lines[span[0] : span[1]])
        if current.strip() == section.strip():
            return ApplyResult(ok=True, changed=False, value_sha256=sha)

    created = not path.exists()
    if dry_run:
        return ApplyResult(ok=True, changed=True, value_sha256=sha, file_created=created)

    backup = _ensure_backup(path)

    if span is None:
        head = text
        if head and not head.endswith("\n"):
            head += "\n"
        if head.strip():
            head += "\n"
        new_text = head + section
    else:
        start, end = span
        # Keep exactly one blank separator line after the spliced section when more of the
        # user's file follows.
        tail = "".join(lines[end:])
        body = section if section.endswith("\n") else section + "\n"
        if tail.strip():
            body += "\n"
        new_text = "".join(lines[:start]) + body + tail

    verify_ok, verify_data, verify_err = _parse(new_text)
    if not verify_ok:
        return ApplyResult(
            ok=False,
            backup=backup,
            error=f"refusing to write: the spliced TOML would not parse ({verify_err})",
        )
    if _toml is not None:
        found, written = _dig(verify_data, key_path)
        if not found or hash_value(written) != sha:
            return ApplyResult(
                ok=False,
                backup=backup,
                error="refusing to write: the spliced TOML does not read back as the intended value",
            )

    try:
        _atomic_write_text(path, new_text)
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
    Excise our table (and its child tables) — but only while it still hashes to
    ``expected_sha256``. A table the user edited is KEPT and reported as drift.

    ``delete_if_empty`` unlinks the whole file when our table was the only thing in it —
    pass it only when the install recorded that it CREATED the file.
    """
    if not key_path:
        return RemoveResult(ok=False, error="key_path must not be empty")
    if not path.exists():
        return RemoveResult(ok=True, removed=False)

    ok, text, error = _read_text(path)
    if not ok:
        return RemoveResult(ok=False, error=error)

    span = find_section(text, key_path)
    if span is None:
        return RemoveResult(ok=True, removed=False)

    if expected_sha256:
        parsed_ok, data, parse_err = _parse(text)
        if not parsed_ok:
            return RemoveResult(
                ok=False, error=f"refusing to edit unparseable TOML at {path}: {parse_err}"
            )
        if _toml is not None:
            found, current = _dig(data, key_path)
            if not found or hash_value(current) != expected_sha256:
                return RemoveResult(ok=True, removed=False, kept_modified=True)

    if dry_run:
        return RemoveResult(ok=True, removed=True)

    lines = text.splitlines(keepends=True)
    start, end = span
    # Absorb the blank lines immediately before our header so removal does not accumulate
    # blank gaps across install/uninstall cycles.
    while start > 0 and lines[start - 1].strip() == "":
        start -= 1
    new_text = "".join(lines[:start]) + "".join(lines[end:])
    if new_text.strip() == "":
        new_text = ""

    verify_ok, _data, verify_err = _parse(new_text)
    if not verify_ok:
        return RemoveResult(
            ok=False, error=f"refusing to write: the TOML would not parse after removal ({verify_err})"
        )

    if delete_if_empty and not new_text.strip():
        try:
            path.unlink()
        except OSError as exc:
            return RemoveResult(ok=False, error=f"cannot remove {path}: {exc}")
        return RemoveResult(ok=True, removed=True)

    try:
        _atomic_write_text(path, new_text)
    except OSError as exc:
        return RemoveResult(ok=False, error=f"cannot write {path}: {exc}")

    return RemoveResult(ok=True, removed=True)
