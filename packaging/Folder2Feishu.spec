# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


repo_root = Path(SPECPATH).resolve().parent
frontend_dist = repo_root / "frontend" / "dist"
if not (frontend_dist / "index.html").is_file():
    raise SystemExit("frontend/dist 不存在，请先运行 frontend 的 npm run build")

hidden_imports = (
    collect_submodules("uvicorn")
    + collect_submodules("sqlalchemy")
    + [
        "folder2feishu",
        "folder2feishu.cli",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]
)

datas = [
    (str(frontend_dist), "frontend/dist"),
    *collect_data_files("certifi"),
]

a = Analysis(
    [str(repo_root / "packaging" / "launcher.py")],
    pathex=[str(repo_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "mypy"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Folder2Feishu",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(repo_root / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Folder2Feishu",
)
