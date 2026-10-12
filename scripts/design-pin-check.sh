#!/usr/bin/env bash
# Vendored into each consumer repository as scripts/design-pin-check.sh (LabOnoM Design Guide, C1).
# Verifies that the pinned copy of the family file, DESIGN.md at the repository root, is exactly the
# released version named in DESIGN_VERSION, whose single line is "<version> <sha256>" taken from
# LabOnoM/design RELEASES.json. The hash is taken over the file with CRLF line endings normalised to
# LF, so a checkout that converts line endings (Windows runners with core.autocrlf) still verifies.
# Needs only coreutils; no network, no access to the private design repository.
set -euo pipefail
cd "$(dirname "$0")/.."
read -r version expected < <(tr -d '\r' < DESIGN_VERSION)
actual=$(tr -d '\r' < DESIGN.md | sha256sum | cut -d' ' -f1)
if [ "$actual" = "$expected" ]; then
  echo "OK    DESIGN.md is LabOnoM Design Guide $version"
else
  echo "FAIL  DESIGN.md does not match LabOnoM Design Guide $version" >&2
  echo "      expected sha256 $expected" >&2
  echo "      actual   sha256 $actual (CRLF normalised to LF)" >&2
  echo "      Copy DESIGN.md from the LabOnoM/design release, or update DESIGN_VERSION when adopting a newer one." >&2
  exit 1
fi
