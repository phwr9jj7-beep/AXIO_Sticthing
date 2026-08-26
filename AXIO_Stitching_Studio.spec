# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for AXIO Stitching Studio.

Builds a SHARED one-directory bundle holding TWO executables:

``dist/AXIO_Stitching_Studio/``
    ``AXIO_Stitching_Studio.exe``  (windowed)  - the desktop GUI.
    ``AXIO_Stitching_MCP.exe``     (console)   - the MCP server (``--mcp-serve``) and the
                                                 ``axio`` CLI (``--cli ...``).
    ``_internal/``                             - the payload BOTH executables share.

Why this shape (and not two one-file EXEs, which this spec previously produced):

* The two builds share ~99% of their payload; one-file duplicated ~318 MB into each EXE.
* One-file self-extracts the whole payload to temp ON EVERY LAUNCH. Agent hosts spawn the
  MCP server per session, so every conversation paid a 10-30 s cold start before the server
  could answer ``initialize`` - flirting with host startup timeouts. One-dir starts in ~1-2 s.
* The MCP executable MUST be a console build: a Windows windowed executable is linked
  without standard handles, so an stdio server inside one has nothing to read or write.
  Agent hosts launch it with redirected pipes, so no console window appears in practice.

The end-user artefact is the Inno Setup installer built from this directory - see
``installer/AXIO_Stitching_Setup.iss`` and ``scripts/build_installer.py``.

``skills/`` is bundled (into ``_internal/skills``) because ``axio agent install`` renders the
installed skill from the shipped source skill's frontmatter and copies its reference doc.
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
    'axio_stitching.tile_sources',
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

exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AXIO_Stitching_Studio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
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
    [],
    exclude_binaries=True,
    name='AXIO_Stitching_MCP',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe_gui,
    exe_mcp,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AXIO_Stitching_Studio',
)
