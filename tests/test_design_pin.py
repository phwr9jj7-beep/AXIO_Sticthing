"""
tests/test_design_pin.py — the pinned LabOnoM Design Guide is the released file
================================================================================
Conformance item C1 of the LabOnoM Design Guide (design workstream W14, step three,
2026-10-12). DESIGN.md at the repository root is a release of the guide's family file;
DESIGN_VERSION names the version and its SHA-256; design/lab-canvas.qss is the Qt palette
generated from the Lab Canvas profile. Editing a copy in place, or letting the pin drift,
fails here before anyone styles the GUI against a file nobody else has. Pure standard
library: runs under pytest or as a script.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_design_md_matches_the_pinned_release():
    version, expected = (ROOT / "DESIGN_VERSION").read_text(encoding="utf-8").split()
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", version), "calendar version"
    # The pin hash normalises CRLF to LF, so a checkout that converts line endings still verifies.
    actual = hashlib.sha256((ROOT / "DESIGN.md").read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    assert actual == expected, (
        f"DESIGN.md does not match LabOnoM Design Guide {version}; "
        "copy the released file from LabOnoM/design or update DESIGN_VERSION"
    )
    assert re.search(rf"^version: {re.escape(version)}$", (ROOT / "DESIGN.md").read_text(encoding="utf-8"), re.M)


def test_qt_palette_is_the_guides_generated_file_for_the_pinned_version():
    version = (ROOT / "DESIGN_VERSION").read_text(encoding="utf-8").split()[0]
    first_line = (ROOT / "design/lab-canvas.qss").read_text(encoding="utf-8").splitlines()[0]
    assert "Lab Canvas" in first_line and "generated" in first_line, first_line
    assert "Do not edit" in first_line


def test_agent_rules_and_readme_name_the_guide():
    for name in ("CLAUDE.md", "README.md"):
        assert "LabOnoM Design Guide" in (ROOT / name).read_text(encoding="utf-8"), f"{name} must point agents at the guide"


if __name__ == "__main__":
    test_design_md_matches_the_pinned_release()
    test_qt_palette_is_the_guides_generated_file_for_the_pinned_version()
    test_agent_rules_and_readme_name_the_guide()
    print("ok")
