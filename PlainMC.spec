# -*- mode: python ; coding: utf-8 -*-
"""PlainMC 启动器打包配置（PyInstaller）—— 单文件版。

用法（在本文件所在目录执行）：
    pyinstaller PlainMC.spec
生成：dist/我的世界启动器.exe

说明：
- 内部资源 textures 打进 exe（运行时从 sys._MEIPASS 读取）。
- game_data / .plainmc_config.json / personalization 不放进 exe，运行时在 exe 同级目录。
- cubiomespi 的 lib/lib.dll 用 collect_all 收集（它靠 ctypes 按包相对路径加载）。
- PyQt6 原始体积约 890MB，这里做了三项裁剪。否则每次启动都要往 %TEMP% 解压近
  900MB，杀软再逐个扫描，启动会慢到「像没反应」（1~2 分钟）：
    ① collect_all 会把同一个 DLL 同时放进 datas 和 binaries —— 去重（省 ~360MB）
    ② 去掉 *.debug.pak（调试符号资源，运行时用不到）—— 省 ~75MB
    ③ 去掉 qtwebengine_devtools_resources*.pak（仅 F12 开发者工具用）—— 省 ~83MB
- 顶部 CONSOLE 开关：False=无黑框（正式）；True=带黑框（调试，崩溃信息直接显示）。
  两种模式启动时都会在「exe 所在目录」（源码运行时为脚本所在目录）追加运行日志
  _startup_log.txt。
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
    """去重 + 剔除用不到的大资源，显著减小解压体积（直接决定启动速度）。"""
    seen = {os.path.basename(src).lower() for src, _ in binaries}
    kept, dropped = [], []
    for src, dest in datas:
        bn = os.path.basename(src).lower()
        if bn.endswith((".dll", ".exe")) and bn in seen:
            dropped.append(bn + " (重复)")
            continue
        if bn.endswith(".debug.pak"):
            dropped.append(bn + " (调试资源)")
            continue
        if "devtools_resources" in bn and bn.endswith(".pak"):
            dropped.append(bn + " (DevTools)")
            continue
        kept.append((src, dest))
    return kept, dropped


pyqt_datas, _d1 = trim(pyqt_datas, pyqt_bins)
cubi_datas, _d2 = trim(cubi_datas, cubi_bins)
print("[spec] 裁剪掉的冗余文件：", len(_d1) + len(_d2), "个")

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
# spec 层的 datas 过滤拦不住它，只能在这里剔除，否则 158MB 的调试/DevTools
# 资源照样会被解压到 %TEMP%（启动慢的主因之一）。
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
    a.binaries,
    a.datas,
    [],
    name="我的世界启动器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=CONSOLE,  # 见文件顶部 CONSOLE 开关
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
