# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()

hiddenimports = (
    collect_submodules("core")
    + collect_submodules("agents")
    + collect_submodules("schemas")
    + collect_submodules("scripts")
    + collect_submodules("pydantic")
    + [
        "companion_server",
        "dashboard_server",
        "dashboard_i18n",
        "director",
        "yaml",
        "click",
        "psutil",
        "tkinter",
        "tkinter.filedialog",
    ]
)

datas = [
    (str(ROOT / "console"), "console"),
    (str(ROOT / "config.yaml"), "."),
    (str(ROOT / "companion.json"), "."),
]

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "_pytest"],
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
    name="AI-Dev-Director",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
    version=str(ROOT / "file_version_info.txt"),
)
