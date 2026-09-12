# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/logo.png', 'assets'), ('assets/reference/combat_power_label.png', 'assets/reference')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Single-file build: everything bundled into one .exe (no dist/ subfolder of
# loose DLLs to ship alongside it). uac_admin=True embeds a manifest that
# makes Windows show the UAC elevation prompt automatically on launch, so the
# script's own runtime _relaunch_as_admin() fallback never has to fire when
# running from this exe.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MapleTapperLooker',
    icon='assets/logo.ico',
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
    uac_admin=True,
)
