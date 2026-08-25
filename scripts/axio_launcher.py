"""
axio_launcher.py — the packaged executable's single entry point.

A frozen (PyInstaller) build is one executable with no ``python`` beside it, so the same
binary has to serve every surface the pipeline exposes. This module dispatches on argv
**before importing anything heavy**, which matters: an MCP server that boots Qt is slow to
start and fails outright on a headless machine, and an agent platform launching the server
would see that as a broken tool provider.

Dispatch:

===================  ==========================================================
``--mcp-serve``      Serve the MCP tools over stdio. This is what
                     ``axio agent install`` registers as the server command for
                     a frozen install.
``--cli [args]``     Run the ``axio`` command-line interface.
``--xml ...``        The legacy headless stitching runner the GUI's worker
                     subprocess invokes.
*(nothing)*          Launch the desktop GUI.
===================  ==========================================================
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _prepare_import_path() -> None:
    """Make ``axio_stitching`` importable from a source checkout and from a frozen bundle."""
    here = Path(__file__).resolve().parent
    for candidate in (here.parent, here):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", here))
        if str(bundle) not in sys.path:
            sys.path.insert(0, str(bundle))


def main() -> None:
    _prepare_import_path()
    argv = sys.argv[1:]

    if "--mcp-serve" in argv:
        # Zeiss datasets routinely carry non-ASCII path components; a cp932/cp1252 default
        # codepage turns those into UnicodeDecodeErrors deep inside the pipeline.
        os.environ.setdefault("PYTHONUTF8", "1")
        from axio_stitching.mcp_server import main as mcp_main

        mcp_main()
        return

    if "--cli" in argv:
        sys.argv = [sys.argv[0]] + [a for a in argv if a != "--cli"]
        from axio_stitching.cli import main as cli_main

        cli_main()
        return

    if "--xml" in argv:
        from gui_runner import main as runner_main

        runner_main()
        return

    from gui_stitch import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
