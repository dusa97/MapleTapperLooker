# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/logo.png', 'assets'), ('assets/reference/combat_power_label.png', 'assets/reference'),
           ('assets/digit_templates.npz', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# opencv-python-headless still bundles its FFmpeg video-decoding backend even though
# this app only ever calls cv2.cvtColor/resize/matchTemplate/minMaxLoc for auto-locate's
# template matching - no video or image-file I/O through cv2 at all - so that ~30MB DLL
# is dead weight here. Cutting it from the collected binaries before building the EXE.
a.binaries = [b for b in a.binaries if 'opencv_videoio_ffmpeg' not in b[0].lower()]

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
