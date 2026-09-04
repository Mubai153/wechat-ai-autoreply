# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.building.build_main import Analysis, EXE, PYZ, COLLECT
from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_dir = Path(SPEC).resolve().parent
vendor_dir = project_dir / ".vendor" / "wechatauto-replica"
sys.path.insert(0, str(vendor_dir))
hiddenimports = collect_submodules("wechatauto")
datas = collect_data_files("wechatauto") + collect_data_files("uiautomation")

a = Analysis(
    [str(project_dir / "main.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="微信自动回复",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="微信自动回复",
)
