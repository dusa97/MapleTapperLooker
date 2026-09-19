# -*- mode: python ; coding: utf-8 -*-


from PyInstaller.utils.hooks import collect_all

capture_datas, capture_binaries, capture_imports = collect_all('windows_capture')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=capture_binaries,
    datas=[('assets/logo.png', 'assets'), ('assets/reference/combat_power_label.png', 'assets/reference'),
           ('assets/digit_templates.npz', 'assets'),
           ('assets/version.txt', 'assets')] + capture_datas,
    hiddenimports=capture_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# opencv-python still bundles its FFmpeg video-decoding backend even though
# this app only ever calls cv2.cvtColor/resize/matchTemplate/minMaxLoc for auto-locate's
# template matching - no video or image-file I/O through cv2 at all - so that ~30MB DLL
# is dead weight here. Cutting it from the collected binaries before building the EXE.
a.binaries = [b for b in a.binaries if 'opencv_videoio_ffmpeg' not in b[0].lower()]

# Launch time on a single-file build is dominated by the NUMBER of files it has
# to unpack into %TEMP% on every start (antivirus scans each one as it lands),
# not by their size. Tk ships 609 timezone files and 127 message-catalog
# translations - 736 of the 921 files it contributes - and this app uses
# neither: no tk clock, no localised Tk dialogs. Dropping them cut the unpack
# from 1,058 files to ~320 with no visible change.
_TK_DEAD_WEIGHT = ('_tcl_data\\tzdata', '_tcl_data/tzdata', '_tcl_data\\msgs', '_tcl_data/msgs',
                   '_tk_data\\msgs', '_tk_data/msgs')
a.datas = [d for d in a.datas if not d[0].startswith(_TK_DEAD_WEIGHT)]

# Single-file build: everything bundled into one .exe (no dist/ subfolder of
# loose DLLs to ship alongside it). uac_admin=True embeds a manifest that
# makes Windows show the UAC elevation prompt automatically on launch, so the
# script's own runtime _relaunch_as_admin() fallback never has to fire when
# running from this exe.
# Store the bundled files UNCOMPRESSED inside the exe. By default every DLL and
# data file is zlib-compressed, so each launch must inflate ~150 MB before it
# can write a single byte to %TEMP% - measured on this machine that inflate was
# ~2 s of a ~2.7 s launch, while the actual disk write was under 0.3 s. The
# price is a bigger download (the exe roughly doubles); the win is that launch
# becomes a plain copy. Bigger-but-instant beats smaller-but-slow for a tool
# that gets opened every play session and downloaded once.
_STORE_UNCOMPRESSED = {'EXTENSION': False, 'DATA': False, 'BINARY': False,
                       'EXECUTABLE': False, 'PYSOURCE': False, 'PYMODULE': False,
                       'SPLASH': False, 'PYZ': False, 'SYMLINK': False}

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    cdict=_STORE_UNCOMPRESSED,
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
