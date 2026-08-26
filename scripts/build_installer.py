"""
build_installer.py — build the fused Windows installer for AXIO Stitching Studio.

One command produces the end-user artefact::

    python scripts/build_installer.py            # PyInstaller bundle + Inno Setup installer
    python scripts/build_installer.py --skip-pyinstaller   # reuse an existing dist/ bundle

Pipeline:

1. **PyInstaller** compiles ``AXIO_Stitching_Studio.spec`` into the shared one-directory
   bundle ``dist/AXIO_Stitching_Studio/`` — two executables (windowed GUI + console
   MCP/CLI) over one ``_internal`` payload.
2. **Inno Setup** (``ISCC``) compiles ``installer/AXIO_Stitching_Setup.iss`` into
   ``dist/AXIO_Stitching_Studio_<version>_Setup.exe``, with the version read from
   ``axio_stitching/__init__.py`` so the installer can never drift from the package.

The script fails loudly at the first broken step and sanity-checks the bundle between the
two stages (both executables present, the bundled skills data included).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "AXIO_Stitching_Studio.spec"
ISS = REPO / "installer" / "AXIO_Stitching_Setup.iss"
BUNDLE = REPO / "dist" / "AXIO_Stitching_Studio"

#: Where the Inno Setup 6 compiler lands for user-scope and machine-scope installs.
ISCC_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
)


def read_version() -> str:
    """The single source of truth: ``__version__`` in the package."""
    text = (REPO / "axio_stitching" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        sys.exit("could not read __version__ from axio_stitching/__init__.py")
    return match.group(1)


def find_iscc() -> Path:
    for candidate in ISCC_CANDIDATES:
        if candidate.is_file():
            return candidate
    sys.exit(
        "Inno Setup 6 (ISCC.exe) not found. Install it with:\n"
        "    winget install JRSoftware.InnoSetup"
    )


def run(cmd: list[str], what: str) -> None:
    print(f"\n=== {what} ===\n    {' '.join(str(c) for c in cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=REPO)
    if result.returncode != 0:
        sys.exit(f"{what} failed with exit code {result.returncode}")


def check_bundle() -> None:
    """The mistakes worth catching between the two stages, not after shipping."""
    problems = []
    for exe in ("AXIO_Stitching_Studio.exe", "AXIO_Stitching_MCP.exe"):
        if not (BUNDLE / exe).is_file():
            problems.append(f"missing executable: {exe}")
    if not (BUNDLE / "_internal" / "skills" / "axio-stitching-pipeline" / "SKILL.md").is_file():
        problems.append("bundled skills/ data missing from _internal (agent install would ship stubs)")
    if problems:
        sys.exit("bundle sanity check failed:\n  " + "\n  ".join(problems))
    size_mb = sum(f.stat().st_size for f in BUNDLE.rglob("*") if f.is_file()) / 1e6
    print(f"    bundle OK: {size_mb:.0f} MB in {BUNDLE}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 2)[1])
    parser.add_argument("--skip-pyinstaller", action="store_true",
                        help="reuse the existing dist/AXIO_Stitching_Studio bundle")
    args = parser.parse_args()

    version = read_version()
    print(f"AXIO Stitching Studio v{version}")

    if not args.skip_pyinstaller:
        run([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm"], "PyInstaller bundle")
    check_bundle()

    iscc = find_iscc()
    run([str(iscc), f"/DAppVersion={version}", str(ISS)], "Inno Setup installer")

    setup = REPO / "dist" / f"AXIO_Stitching_Studio_{version}_Setup.exe"
    if not setup.is_file():
        sys.exit(f"ISCC reported success but {setup.name} is missing")
    print(f"\nDone: {setup}  ({setup.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
