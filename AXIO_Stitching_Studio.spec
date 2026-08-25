# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for AXIO Stitching Studio.

Builds TWO one-file executables from one shared analysis:

``AXIO_Stitching_Studio.exe``  (windowed)
    The desktop GUI. ``console=False`` because a GUI must not open a terminal.

``AXIO_Stitching_MCP.exe``  (console)
    The headless surfaces - the MCP server (``--mcp-serve``) and the ``axio`` CLI
    (``--cli ...``). This one MUST be a console build: a Windows windowed executable is
    linked without standard handles, so an stdio MCP server built as ``console=False``
    has nothing to read or write and the agent platform sees a dead tool provider.
    Agent hosts launch it as a subprocess with redirected pipes, so no window appears.

Both share ``scripts/axio_launcher.py``, which dispatches on argv BEFORE importing Qt.

``skills/`` is bundled because ``axio agent install`` renders the installed skill from the
shipped source skill's frontmatter and copies its reference doc.
"""

_HIDDEN = [
    # Reached only through the launcher's lazy dispatch, so PyInstaller's static analysis
    # does not see them.
    'axio_stitching.mcp_server',
    'axio_stitching.cli',
    'axio_stitching.agent_runner',
    'axio_stitching.agent_integration',
    'axio_stitching.configkey',
    'axio_stitching.configkey_toml',
    'axio_stitching.doctor',
    'axio_stitching.estimate',
    'axio_stitching.jobs',
    'axio_stitching.qc',
    'gui_stitch',
    'gui_runner',
    'gui_worker',
]

a = Analysis(
    ['scripts\\axio_launcher.py'],
    pathex=['scripts'],
    binaries=[],
    datas=[('skills', 'skills')],
    hiddenimports=_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AXIO_Stitching_Studio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_mcp = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AXIO_Stitching_MCP',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
