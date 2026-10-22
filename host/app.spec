# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the BindDeckLite Windows host.

Build on Windows from ``host/``:

    pyinstaller --noconfirm --clean app.spec

Output: ``host/dist/BindDeckLite.exe`` (one file, no console window).
The exe starts as a native pywebview window (``desktop.run_webview``): Flask
serves in the background while pywebview renders the existing Web UI in a real
window, with a tray icon for minimize-to-tray.  See ``desktop.py``.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

here = SPECPATH
assets = os.path.join(here, "assets")

datas = [
    (os.path.join(here, "templates"), "templates"),
    (os.path.join(here, "static"), "static"),
]
if os.path.isdir(assets):
    datas.append((assets, "assets"))

hiddenimports = [
    "tkinter",
    "tkinter.filedialog",
    "pystray._win32",
    "PIL.ImageDraw",
    # pywebview renders the Web UI in a native window.  The platform backend
    # (winforms / edgechromium / mshtml) is picked lazily at runtime by
    # guilib.initialize(), so static analysis misses it — list them explicitly.
    # pythonnet (clr / clr_loader) is pywebview's Windows backend.
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.mshtml",
    "pythonnet",
    "clr_loader",
    # bleak's WinRT backend is imported lazily.
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.storage.streams",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.radios",
    "winrt.windows.storage",
]
for package in ("bleak", "pystray", "PIL", "webview", "pythonnet"):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

icon = os.path.join(assets, "app.ico")
if not os.path.exists(icon):
    icon = None

a = Analysis(
    ["app.py"],
    pathex=[here],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas", "matplotlib", "pytest"],
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
    name="BindDeckLite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
