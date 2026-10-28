# -*- mode: python ; coding: utf-8 -*-
"""PlainMC 启动器打包配置（PyInstaller）—— 文件夹版（--onedir）。

用法（在本文件所在目录执行）：
    pyinstaller PlainMC_onedir.spec
生成：dist/我的世界启动器/我的世界启动器.exe  (+ 一堆 _internal 依赖文件)

为什么需要这个版本：
  单文件版（PlainMC.spec）每次启动都要把整个 PyQt6 + WebEngine 压缩包解压到
  %TEMP%，体积近 1GB，杀软还会逐个扫描，在部分机器上要 1~2 分钟才出窗口，
  表现为「双击后完全没反应」。本版本不压缩、不解压，双击**瞬间**出窗口。

用法提示：整个 dist/我的世界启动器/ 文件夹要一起拷贝，不能只拷 exe。
"""
import os
from PyInstaller.utils.hooks import collect_all

# ---------------------------------------------------------------------------
# 打包选项
# ---------------------------------------------------------------------------
CONSOLE = False   # True = 带黑框（调试用）

# ---- PyQt6（含 QtWebEngine 的 DLL / resources / pak）----
pyqt_datas, pyqt_bins, pyqt_hidden = collect_all("PyQt6")
# ---- cubiomespi（含 ctypes 加载的 lib/lib.dll）----
cubi_datas, cubi_bins, cubi_hidden = collect_all("cubiomespi")


def trim(datas, binaries):
    """去重 + 剔除用不到的大资源（与单文件版保持一致）。"""
    seen = {os.path.basename(src).lower() for src, _ in binaries}
    kept = []
    for src, dest in datas:
        bn = os.path.basename(src).lower()
        if bn.endswith((".dll", ".exe")) and bn in seen:
            continue
        if bn.endswith(".debug.pak"):
            continue
        if "devtools_resources" in bn and bn.endswith(".pak"):
            continue
        kept.append((src, dest))
    return kept


pyqt_datas = trim(pyqt_datas, pyqt_bins)
cubi_datas = trim(cubi_datas, cubi_bins)

a = Analysis(
    ["PlainMC.py"],
    pathex=[],
    binaries=pyqt_bins + cubi_bins,
    datas=[("textures", "textures")] + pyqt_datas + cubi_datas,
    hiddenimports=[
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebChannel",
        "PyQt6.QtMultimedia",
        "cubiomespi",
    ] + pyqt_hidden
    + cubi_hidden,
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ---------------------------------------------------------------------------
# Analysis 之后再过滤一次
# ---------------------------------------------------------------------------
# PyInstaller 内置的 hook-PyQt6.QtWebEngineCore 会自行把 .pak 资源塞进 a.datas，
# spec 层的 datas 过滤拦不住它，只能在这里剔除。
def _filter_a_datas(a):
    keep = []
    for entry in a.datas:
        src = entry[0]          # PyInstaller 6.x: (src, dest, typecode)
        bn = os.path.basename(src).lower()
        if bn.endswith(".debug.pak"):
            continue
        if "devtools_resources" in bn and bn.endswith(".pak"):
            continue
        keep.append(entry)
    removed = len(a.datas) - len(keep)
    a.datas = keep
    print("[spec] Analysis 后剔除调试/DevTools 资源：", removed, "个")


_filter_a_datas(a)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="我的世界启动器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="我的世界启动器",
)
