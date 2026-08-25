"""
test_configkey_toml.py — the TOML owned-key surgery contract.

The TOML path is a byte-preserving SPLICE, not a parse-and-reserialise, because
reserialising would destroy the user's comments, ordering and formatting. These tests pin
that property down alongside the same safety guarantees the JSON module gives.
"""

from pathlib import Path

import pytest

from axio_stitching.configkey_toml import (
    CONFIG_BACKUP_SUFFIX,
    apply_key,
    find_section,
    read_key,
    remove_key,
    render_section,
    toml_reader_available,
)

ENTRY = {
    "command": "python",
    "args": ["-m", "axio_stitching.mcp_server"],
    "startup_timeout_sec": 60,
    "env": {"PYTHONUTF8": "1"},
}
KEY = ["mcp_servers", "axio-stitching"]

EXISTING = """\
# my codex config -- keep this comment
model = "gpt-5"

[mcp_servers.other-tool]
command = "othercmd"
args = ["--serve"]

[tui]
theme = "dark"
"""


class TestRenderSection:
    def test_leaves_a_bare_key_unquoted(self):
        # TOML bare keys allow hyphens, so `axio-stitching` needs no quoting.
        assert render_section(KEY, {"command": "x"}).startswith("[mcp_servers.axio-stitching]")

    def test_quotes_a_key_that_is_not_bare(self):
        assert render_section(["a", "needs quoting"], {"c": 1}).startswith('[a."needs quoting"]')
        assert render_section(["a", "dotted.name"], {"c": 1}).startswith('[a."dotted.name"]')

    def test_emits_nested_dicts_as_child_tables(self):
        text = render_section(KEY, ENTRY)
        assert "[mcp_servers.axio-stitching.env]" in text
        assert 'PYTHONUTF8 = "1"' in text

    def test_renders_scalars_by_type(self):
        text = render_section(["t"], {"s": "x", "i": 3, "b": True, "a": ["p", "q"]})
        assert 's = "x"' in text and "i = 3" in text and "b = true" in text
        assert 'a = ["p", "q"]' in text

    def test_escapes_a_windows_path(self):
        text = render_section(["t"], {"command": r"C:\Program Files\x.exe"})
        assert r'"C:\\Program Files\\x.exe"' in text

    def test_rejects_a_non_table_value(self):
        with pytest.raises(TypeError):
            render_section(["t"], "not a table")  # type: ignore[arg-type]


class TestFindSection:
    def test_finds_a_quoted_header(self):
        text = 'a = 1\n[mcp_servers."axio-stitching"]\ncommand = "x"\n'
        assert find_section(text, KEY) == (1, 3)

    def test_matches_a_single_quoted_header_semantically(self):
        text = "[mcp_servers.'axio-stitching']\ncommand = \"x\"\n"
        assert find_section(text, KEY) == (0, 2)

    def test_matches_a_spaced_header_semantically(self):
        text = '[ mcp_servers . "axio-stitching" ]\ncommand = "x"\n'
        assert find_section(text, KEY) == (0, 2)

    def test_includes_child_tables_in_the_span(self):
        text = (
            '[mcp_servers."axio-stitching"]\ncommand = "x"\n\n'
            '[mcp_servers."axio-stitching".env]\nA = "1"\n\n[other]\nb = 2\n'
        )
        start, end = find_section(text, KEY)
        assert start == 0
        assert "".join(text.splitlines(keepends=True)[start:end]).count("[other]") == 0
        assert 'A = "1"' in "".join(text.splitlines(keepends=True)[start:end])

    def test_returns_none_when_absent(self):
        assert find_section("[other]\nb = 2\n", KEY) is None

    def test_does_not_match_a_different_table(self):
        assert find_section('[mcp_servers."other"]\nc = 1\n', KEY) is None


class TestApplyKey:
    def test_creates_a_missing_file(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        result = apply_key(target, KEY, ENTRY)
        assert result.ok and result.changed and result.file_created
        assert "[mcp_servers.axio-stitching]" in target.read_text(encoding="utf-8")

    def test_preserves_comments_and_other_tables(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text(EXISTING, encoding="utf-8")
        assert apply_key(target, KEY, ENTRY).ok

        text = target.read_text(encoding="utf-8")
        assert "# my codex config -- keep this comment" in text
        assert '[mcp_servers.other-tool]' in text
        assert 'command = "othercmd"' in text
        assert '[tui]' in text and 'theme = "dark"' in text

    def test_is_idempotent(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text(EXISTING, encoding="utf-8")
        apply_key(target, KEY, ENTRY)
        before = target.read_text(encoding="utf-8")

        second = apply_key(target, KEY, ENTRY)
        assert second.ok and not second.changed
        assert target.read_text(encoding="utf-8") == before

    def test_replaces_an_existing_section_without_duplicating_it(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        apply_key(target, KEY, ENTRY)
        apply_key(target, KEY, {**ENTRY, "startup_timeout_sec": 120})

        text = target.read_text(encoding="utf-8")
        assert text.count("[mcp_servers.axio-stitching]") == 1
        assert "startup_timeout_sec = 120" in text
        assert "startup_timeout_sec = 60" not in text

    def test_dry_run_writes_nothing(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        result = apply_key(target, KEY, ENTRY, dry_run=True)
        assert result.ok and result.changed
        assert not target.exists()

    @pytest.mark.skipif(not toml_reader_available(), reason="needs a TOML reader to detect the error")
    def test_refuses_unparseable_toml(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        original = "this is = = not toml\n"
        target.write_text(original, encoding="utf-8")
        result = apply_key(target, KEY, ENTRY)
        assert not result.ok and "unparseable" in (result.error or "")
        assert target.read_text(encoding="utf-8") == original

    def test_backs_up_pre_existing_content(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text(EXISTING, encoding="utf-8")
        apply_key(target, KEY, ENTRY)
        assert target.with_name(target.name + CONFIG_BACKUP_SUFFIX).read_text(encoding="utf-8") == EXISTING

    @pytest.mark.skipif(not toml_reader_available(), reason="needs a TOML reader")
    def test_written_value_reads_back_identically(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text(EXISTING, encoding="utf-8")
        apply_key(target, KEY, ENTRY)
        result = read_key(target, KEY)
        assert result.ok and result.present and result.value == ENTRY


class TestRemoveKey:
    def test_restores_the_file_byte_for_byte(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text(EXISTING, encoding="utf-8")
        applied = apply_key(target, KEY, ENTRY)

        result = remove_key(target, KEY, applied.value_sha256)
        assert result.ok and result.removed
        assert target.read_text(encoding="utf-8") == EXISTING

    @pytest.mark.skipif(not toml_reader_available(), reason="drift detection needs a TOML reader")
    def test_keeps_a_section_the_user_edited(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        applied = apply_key(target, KEY, ENTRY)
        target.write_text(
            target.read_text(encoding="utf-8").replace("startup_timeout_sec = 60", "startup_timeout_sec = 999"),
            encoding="utf-8",
        )

        result = remove_key(target, KEY, applied.value_sha256)
        assert result.ok and not result.removed and result.kept_modified
        assert "startup_timeout_sec = 999" in target.read_text(encoding="utf-8")

    def test_removes_child_tables_too(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text(EXISTING, encoding="utf-8")
        applied = apply_key(target, KEY, ENTRY)

        remove_key(target, KEY, applied.value_sha256)
        assert "PYTHONUTF8" not in target.read_text(encoding="utf-8")

    def test_delete_if_empty_removes_a_file_we_created(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        applied = apply_key(target, KEY, ENTRY)
        remove_key(target, KEY, applied.value_sha256, delete_if_empty=True)
        assert not target.exists()

    def test_delete_if_empty_keeps_a_file_with_other_content(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text(EXISTING, encoding="utf-8")
        applied = apply_key(target, KEY, ENTRY)
        remove_key(target, KEY, applied.value_sha256, delete_if_empty=True)
        assert target.exists() and target.read_text(encoding="utf-8") == EXISTING

    def test_removing_an_absent_section_is_not_an_error(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text(EXISTING, encoding="utf-8")
        result = remove_key(target, KEY, "deadbeef")
        assert result.ok and not result.removed
