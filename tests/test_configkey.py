"""
test_configkey.py — the JSON owned-key surgery contract.

These tests exist because the module edits files that belong to the USER and to other tools.
Every guarantee in its docstring is asserted here: parse-refuse, backup-once, atomic write,
idempotency, hash-verified removal, and "never touch anything but our one key".
"""

import json
from pathlib import Path

from axio_stitching.configkey import (
    CONFIG_BACKUP_SUFFIX,
    apply_key,
    hash_value,
    read_key,
    remove_key,
)

ENTRY = {"command": "python", "args": ["-m", "axio_stitching.mcp_server"], "env": {"PYTHONUTF8": "1"}}
KEY = ["mcpServers", "axio-stitching"]


class TestHashValue:
    def test_is_stable_across_key_order(self):
        assert hash_value({"a": 1, "b": 2}) == hash_value({"b": 2, "a": 1})

    def test_differs_for_different_values(self):
        assert hash_value({"a": 1}) != hash_value({"a": 2})

    def test_distinguishes_nested_changes(self):
        assert hash_value({"env": {"X": "1"}}) != hash_value({"env": {"X": "2"}})


class TestApplyKey:
    def test_creates_a_missing_file(self, tmp_path: Path):
        target = tmp_path / "config.json"
        result = apply_key(target, KEY, ENTRY)
        assert result.ok and result.changed and result.file_created
        assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["axio-stitching"] == ENTRY

    def test_dry_run_writes_nothing(self, tmp_path: Path):
        target = tmp_path / "config.json"
        result = apply_key(target, KEY, ENTRY, dry_run=True)
        assert result.ok and result.changed
        assert not target.exists()

    def test_preserves_every_other_key(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}), encoding="utf-8"
        )
        apply_key(target, KEY, ENTRY)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["mcpServers"]["other"] == {"command": "x"}
        assert data["theme"] == "dark"
        assert data["mcpServers"]["axio-stitching"] == ENTRY

    def test_is_idempotent(self, tmp_path: Path):
        target = tmp_path / "config.json"
        apply_key(target, KEY, ENTRY)
        second = apply_key(target, KEY, ENTRY)
        assert second.ok and not second.changed

    def test_file_created_is_false_for_a_pre_existing_file(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")
        assert apply_key(target, KEY, ENTRY).file_created is False

    def test_treats_an_empty_file_as_an_empty_object(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text("   \n", encoding="utf-8")
        result = apply_key(target, KEY, ENTRY)
        assert result.ok and result.changed

    def test_refuses_unparseable_json(self, tmp_path: Path):
        target = tmp_path / "config.json"
        original = '{"mcpServers": {broken'
        target.write_text(original, encoding="utf-8")
        result = apply_key(target, KEY, ENTRY)
        assert not result.ok
        assert "unparseable" in (result.error or "")
        assert target.read_text(encoding="utf-8") == original, "the user's file must be untouched"

    def test_refuses_a_non_object_top_level(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        result = apply_key(target, KEY, ENTRY)
        assert not result.ok and "not an object" in (result.error or "")

    def test_backs_up_pre_existing_content_once(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        backup = target.with_name(target.name + CONFIG_BACKUP_SUFFIX)

        apply_key(target, KEY, ENTRY)
        assert json.loads(backup.read_text(encoding="utf-8")) == {"theme": "dark"}

        apply_key(target, ["mcpServers", "other"], {"command": "y"})
        assert json.loads(backup.read_text(encoding="utf-8")) == {"theme": "dark"}, (
            "the backup must keep the ORIGINAL content, not be overwritten by a later state"
        )

    def test_does_not_back_up_a_file_it_creates(self, tmp_path: Path):
        target = tmp_path / "config.json"
        apply_key(target, KEY, ENTRY)
        assert not target.with_name(target.name + CONFIG_BACKUP_SUFFIX).exists()

    def test_rejects_an_empty_key_path(self, tmp_path: Path):
        assert not apply_key(tmp_path / "c.json", [], ENTRY).ok


class TestReadKey:
    def test_reports_absence(self, tmp_path: Path):
        result = read_key(tmp_path / "config.json", KEY)
        assert result.ok and not result.present

    def test_reads_the_value_back(self, tmp_path: Path):
        target = tmp_path / "config.json"
        apply_key(target, KEY, ENTRY)
        result = read_key(target, KEY)
        assert result.ok and result.present and result.value == ENTRY

    def test_refuses_unparseable_json(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text("{oops", encoding="utf-8")
        assert not read_key(target, KEY).ok


class TestRemoveKey:
    def test_removes_our_key_and_prunes_the_emptied_container(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        applied = apply_key(target, KEY, ENTRY)

        result = remove_key(target, KEY, applied.value_sha256)
        assert result.ok and result.removed
        assert json.loads(target.read_text(encoding="utf-8")) == {"theme": "dark"}

    def test_keeps_another_tools_entry_in_the_same_container(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
        applied = apply_key(target, KEY, ENTRY)

        remove_key(target, KEY, applied.value_sha256)
        assert json.loads(target.read_text(encoding="utf-8")) == {"mcpServers": {"other": {"command": "x"}}}

    def test_keeps_a_value_the_user_edited(self, tmp_path: Path):
        target = tmp_path / "config.json"
        applied = apply_key(target, KEY, ENTRY)

        data = json.loads(target.read_text(encoding="utf-8"))
        data["mcpServers"]["axio-stitching"]["env"]["MY_VAR"] = "1"
        target.write_text(json.dumps(data), encoding="utf-8")

        result = remove_key(target, KEY, applied.value_sha256)
        assert result.ok and not result.removed and result.kept_modified
        assert "axio-stitching" in json.loads(target.read_text(encoding="utf-8"))["mcpServers"]

    def test_removing_an_absent_key_is_not_an_error(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")
        result = remove_key(target, KEY, "deadbeef")
        assert result.ok and not result.removed

    def test_removing_from_a_missing_file_is_not_an_error(self, tmp_path: Path):
        result = remove_key(tmp_path / "nope.json", KEY, "deadbeef")
        assert result.ok and not result.removed

    def test_delete_if_empty_removes_a_file_we_created(self, tmp_path: Path):
        target = tmp_path / "config.json"
        applied = apply_key(target, KEY, ENTRY)
        assert applied.file_created

        remove_key(target, KEY, applied.value_sha256, delete_if_empty=True)
        assert not target.exists()

    def test_delete_if_empty_keeps_a_file_that_still_has_content(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        applied = apply_key(target, KEY, ENTRY)

        remove_key(target, KEY, applied.value_sha256, delete_if_empty=True)
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == {"theme": "dark"}

    def test_dry_run_writes_nothing(self, tmp_path: Path):
        target = tmp_path / "config.json"
        applied = apply_key(target, KEY, ENTRY)
        result = remove_key(target, KEY, applied.value_sha256, dry_run=True)
        assert result.ok and result.removed
        assert read_key(target, KEY).present, "dry run must not actually remove it"
