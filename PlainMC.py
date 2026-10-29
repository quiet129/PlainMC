# -*- coding: utf-8 -*-
"""PlainMC —— 仿 PCL2 美术风格的《我的世界》Python 启动器（单文件版）。

把原本分散在 mc_launcher 包内的所有模块整合进本文件，零包结构依赖，
直接 `python PlainMC.py` 即可运行。

依赖：仅 PyQt6（网络下载使用标准库 urllib，无需 requests）。
    pip install PyQt6

功能：
  - 从官方源 / 国内镜像 (BMCLAPI) 下载《我的世界》各版本（客户端、依赖库、
    原生库、资源文件），镜像缺文件时自动回退官方源并校验 SHA1。
  - 离线账号启动（填个名字即可玩单机 / 局域网）。
  - PCL2 风格界面：无边框渐变标题栏 + 左侧导航 + 圆角卡片 + 浅/深双主题。
"""

import datetime
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile

# ===== 诊断：exe 运行时把 stdout/stderr 追加到日志文件，便于排查启动崩溃 =====
# 说明：追加模式（不清空），每次运行都保留历史，便于对比多次启动。
# 日志写到 exe 所在目录 —— 无论从哪启动，都一定能找到（冻结/源码两种模式通用）。
_LOG_PATHS = []
try:
    import datetime as _dt
    import time as _time
    # 候选 1：exe 所在目录（打包后）或本文件所在目录（源码运行）
    try:
        if getattr(sys, "frozen", False):
            _base_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            _base_dir = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        _base_dir = os.getcwd()
    _cands = [os.path.join(_base_dir, "_startup_log.txt")]
    # 候选 2：exe 目录（源码运行时 __file__ 与 exe 可能不同）
    try:
        _cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                   "_startup_log.txt"))
    except Exception:
        pass
    _logf = None
    for _p in _cands:
        try:
            _f = open(_p, "a", encoding="utf-8", buffering=1)
            if _logf is None:
                _logf = _f
            else:
                _f.close()
            _LOG_PATHS.append(_p)
        except Exception:
            continue
    if _logf is not None:
        _hdr = (f"\n===== RUN {_dt.datetime.now():%Y-%m-%d %H:%M:%S} "
                f"pid={_time.time():.0f} frozen={getattr(sys, 'frozen', False)} "
                f"name={__name__} exe={sys.executable} =====\n")
        _logf.write(_hdr)
        _logf.flush()
        if getattr(sys, "frozen", False):
            # 冻结模式：重定向，崩溃信息（traceback）也会写进日志
            sys.stdout = _logf
            sys.stderr = _logf

            def _log_excepthook(et, ev, tb):
                import traceback as _tb
                _tb.print_exception(et, ev, tb, file=_logf)
                _logf.flush()
            sys.excepthook = _log_excepthook
except Exception:
    _LOG_PATHS = []
# ===== 诊断结束 =====

import platform
import tarfile
import ssl

# --------------------------------------------------------------------------
# SSL 证书信任修复
# 现象：部分 Windows + Python 3.14 环境下，urllib 默认使用系统根证书存储会
# 报 CERTIFICATE_VERIFY_FAILED（unable to get local issuer certificate），
# 导致下载页「获取版本」、下载游戏/Java、正版登录等全部 HTTPS 请求失败。
# 修复：优先用 certifi 自带的完整 CA 包作为信任锚；若 certifi 缺失则退回
# 「关闭证书校验」的兜底上下文（仅本启动器内部使用，不污染系统其它程序）。
# 通过覆盖全局默认 HTTPS 上下文，所有 urllib.request.urlopen 调用自动受益。
# --------------------------------------------------------------------------
def _build_ssl_ctx():
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        # 没有 certifi 时，退而求其次：关掉主机名/证书校验，保证下载可用
        try:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        except Exception:
            pass
    return ctx


# 缓存单个上下文，避免每次请求重建
_SSL_CTX = None


def _plainmc_ssl_ctx():
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _build_ssl_ctx()
    return _SSL_CTX


# 覆盖全局默认 HTTPS 上下文（urllib 在无显式 context 时调用它）
try:
    ssl._create_default_https_context = _plainmc_ssl_ctx
except Exception:
    pass


# 种子地图引擎（确定性生物群系生成 + MC 1.18+ 结构定位），与本文件同目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import urllib.error
import urllib.parse
import urllib.request

# 网页地图（内嵌 Minecraft Search，又快又清晰，自带种子搜索器）。
# 若环境缺少 PyQt6-WebEngine，则仅保留离线（cubiomes）地图，不影响启动器其余功能。
#
# 【按需加载】Qt6WebEngineCore.dll 有 190MB+，打包成 exe 后它是刚解压到磁盘的
# 「冷文件」，加载它要几十秒，表现为「双击 exe 后一两分钟完全没反应」。
# 所以这里不在模块导入阶段加载，而是等用户真的打开「网页地图」时才加载。
QWebEngineView = None
QWebEngineSettings = None
_HAVE_WEBENGINE = False
_WEBENGINE_TRIED = False


def _load_webengine():
    """按需加载 QtWebEngine，返回是否可用。幂等：只尝试一次。"""
    global QWebEngineView, QWebEngineSettings, _HAVE_WEBENGINE, _WEBENGINE_TRIED
    if _WEBENGINE_TRIED:
        return _HAVE_WEBENGINE
    _WEBENGINE_TRIED = True
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView as _V
        from PyQt6.QtWebEngineCore import QWebEngineSettings as _S
        QWebEngineView = _V
        QWebEngineSettings = _S
        _HAVE_WEBENGINE = True
        print("[DBG] WebEngine 加载完成", flush=True)
    except Exception as e:
        print("[DBG] WebEngine 加载失败:", repr(e), flush=True)
        _HAVE_WEBENGINE = False
    return _HAVE_WEBENGINE

from PyQt6.QtCore import (Qt, QPoint, pyqtSignal, QThread, QProcess,
                          QTimer, QObject)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QApplication, QLineEdit, QComboBox, QListWidget, QListWidgetItem,
    QFrame, QSpinBox, QTextEdit, QFileDialog, QMessageBox, QSizePolicy,
    QButtonGroup, QGridLayout, QSlider, QCheckBox,
    QScrollArea, QLayout, QProgressBar, QDialog, QColorDialog, QMenu,
)
from PyQt6.QtGui import (QPixmap, QPainter, QColor, QPainterPath,
                          QBrush, QImage, QLinearGradient, QPen)
# QtMultimedia 依赖额外的媒体后端 DLL，打包环境下有可能加载失败；
# 失败时仅关闭「背景音乐」功能，绝不影响启动器启动。
try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    _HAVE_MULTIMEDIA = True
except Exception as _e_mm:
    QMediaPlayer = None
    QAudioOutput = None
    _HAVE_MULTIMEDIA = False
    print("[DBG] QtMultimedia 加载失败（背景音乐不可用）:", repr(_e_mm), flush=True)
from PyQt6.QtCore import QUrl


# ==========================================================================
# 常量
# ==========================================================================
APP_NAME = "PlainMC"
APP_VERSION = "1.2.0"

USER_AGENT = "PlainMC/1.0 (+https://github.com/plainmc)"
# 我们不支持任何可选特性（演示模式 / 自定义分辨率 / quickPlay 等）
SUPPORTED_FEATURES = set()

# --------------------------------------------------------------------------
# 后台线程登记
# QThread 若在运行中被 Python 回收，Qt 会阻塞 UI 线程等待/终止该线程
# （表现为界面卡死并打印 "QThread: Destroyed while thread is still running"）。
# 所有后台 worker 必须通过 _track_worker 登记，保证运行期间持有引用。
# --------------------------------------------------------------------------
_ACTIVE_THREADS = set()


def _track_worker(w):
    _ACTIVE_THREADS.add(w)
    try:
        w.finished.connect(lambda *_: _ACTIVE_THREADS.discard(w))
        w.error.connect(lambda *_: _ACTIVE_THREADS.discard(w))
    except Exception:
        pass
    return w


LIB_BASE = "https://libraries.minecraft.net/"
ASSET_BASE = "https://resources.download.minecraft.net/"

# --------------------------------------------------------------------------
# 下载镜像源
# 每个镜像提供：
#   name     —— 界面展示名
#   manifest —— 版本清单地址
#   rewrite  —— 官方 URL 前缀 → 镜像 URL 前缀 的映射（None 表示不重写）
# BMCLAPI 实测：launchermeta/launcher/piston-meta/piston-data 仅替换主机、
# 路径保持不变；resources→/assets/、libraries→/maven/ 为带前缀映射。
# --------------------------------------------------------------------------
MIRRORS = {
    "official": {
        "name": "官方源 (Mojang)",
        "manifest": "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json",
        "rewrite": None,
    },
    "bmclapi": {
        "name": "国内镜像 (BMCLAPI)",
        "manifest": "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json",
        "rewrite": {
            "https://launchermeta.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "http://launchermeta.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "https://launcher.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "http://launcher.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "https://piston-meta.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "http://piston-meta.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "https://piston-data.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "http://piston-data.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "https://resources.download.minecraft.net/": "https://bmclapi2.bangbang93.com/assets/",
            "http://resources.download.minecraft.net/": "https://bmclapi2.bangbang93.com/assets/",
            "https://libraries.minecraft.net/": "https://bmclapi2.bangbang93.com/maven/",
            "http://libraries.minecraft.net/": "https://bmclapi2.bangbang93.com/maven/",
            "https://textures.minecraft.net/": "https://bmclapi2.bangbang93.com/textures/",
            "http://textures.minecraft.net/": "https://bmclapi2.bangbang93.com/textures/",
        },
    },
}
DEFAULT_MIRROR = "bmclapi"


def _frozen():
    """是否处于 PyInstaller 打包后的运行环境。"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _resource_dir():
    """打包进 exe 的内部只读资源目录（如 textures 贴图）。
    PyInstaller --onefile 下指向临时解压目录 sys._MEIPASS，否则为脚本所在目录。"""
    if _frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _app_dir():
    """用户数据根目录（配置 / game_data / personalization）。
    始终与可执行文件或脚本同级，便于打包后随 exe 一起放置。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


PROJECT_ROOT = _app_dir()
CONFIG_PATH = os.path.join(PROJECT_ROOT, ".plainmc_config.json")


# ==========================================================================
# Java 探测与自动下载
# ==========================================================================
# Adoptium（原 AdoptOpenJDK / Eclipse Temurin）二进制接口：返回 JRE 压缩包
# 的重定向地址（最终落在 GitHub Releases）。下方 download_java 会自动跟随
# 重定向，并在国内网络环境下尝试若干 GitHub 代理镜像作为兜底。
ADOPTIUM_API = (
    "https://api.adoptium.net/v3/binary/latest/{feature}/ga/"
    "windows/x64/jre/hotspot/normal/eclipse"
)
# GitHub 下载代理（用于国内网络兜底，按需启用，可能不稳定）
_GITHUB_PROXIES = (
    "https://ghproxy.net/",
    "https://mirror.ghproxy.com/",
)


# Windows：GUI 程序启动子进程时，Windows 会为每个子进程弹出一个黑色控制台
# 窗口（打包成 exe 后尤其明显 —— 每查一次 java 版本就闪一个 CMD）。
# CREATE_NO_WINDOW 让子进程静默运行；非 Windows 上该属性不存在，取 0 即可。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# java 探测结果缓存：path -> (版本字符串, 主版本号)
# 避免每次刷新都重新启动 java.exe（既慢又会弹一堆窗口）
_JAVA_VER_CACHE = {}


def _java_info(path):
    """一次性取得 (版本字符串, 主版本号) 并缓存。"""
    hit = _JAVA_VER_CACHE.get(path)
    if hit is not None:
        return hit
    ver = None
    try:
        proc = subprocess.run(
            [path, "-version"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
        s = (proc.stdout + proc.stderr).strip()
        m = re.search(r'version "([^"]+)"', s)
        if m:
            ver = m.group(1)
        else:
            first = s.split("\n")[0].strip()
            ver = first or None
    except Exception:
        ver = None
    major = None
    if ver:
        # "1.8.0_401" -> 8；"17.0.10" -> 17；"21.0.3" -> 21
        if ver.startswith("1."):
            tail = ver[2:].split(".")[0]
            major = int(tail) if tail.isdigit() else None
        else:
            m = re.match(r"(\d+)", ver)
            major = int(m.group(1)) if m else None
    _JAVA_VER_CACHE[path] = (ver, major)
    return ver, major


def java_version(path):
    """返回 java -version 解析出的版本字符串，失败返回 None。"""
    return _java_info(path)[0]


def java_major_version(path):
    """返回 Java 主版本号（如 8/11/17/21），无法解析返回 None。"""
    return _java_info(path)[1]


def _java_scan_roots(game_root=None):
    roots = [
        r"C:/Program Files/Java",
        r"C:/Program Files (x86)/Java",
        r"C:/Program Files/AdoptOpenJDK",
        r"C:/Program Files/Eclipse Adoptium",
        r"C:/Program Files/Zulu",
        r"C:/Program Files/BellSoft",
    ]
    if game_root:
        # 本启动器自动下载的 Java 存放于此，纳入扫描以便下次复用
        roots.append(os.path.join(game_root, ".java"))
    return roots


def find_java_detailed(game_root=None):
    """返回 [{"name","path","version","major"}, ...]，按常见顺序排列。"""
    found = []
    try:
        p = shutil.which("java")
        if p:
            found.append(("PATH", p))
    except Exception:
        pass

    for r in _java_scan_roots(game_root):
        if not os.path.isdir(r):
            continue
        for top, dirs, files in os.walk(r):
            # 限制遍历深度，避免误入无关目录
            depth = top[len(r):].count(os.sep)
            if depth > 3:
                dirs[:] = []
                continue
            cand = os.path.join(top, "bin", "java.exe")
            if os.path.isfile(cand):
                name = os.path.basename(os.path.dirname(top))
                found.append((name, cand))

    seen = set()
    out = []
    for name, path in found:
        if path not in seen:
            seen.add(path)
            out.append({
                "name": name,
                "path": path,
                "version": java_version(path),
                "major": java_major_version(path),
            })
    return out


def find_java(game_root=None):
    """兼容旧调用：返回 [(显示名, java可执行文件路径), ...]。"""
    return [(d["name"], d["path"]) for d in find_java_detailed(game_root)]


def find_java_for_major(major, game_root=None):
    """在当前系统中寻找主版本号匹配的 Java，返回其 java.exe 路径或 None。"""
    for d in find_java_detailed(game_root):
        if d["major"] == major:
            return d["path"]
    return None


def required_java_for_version(vid):
    """根据 MC 版本号推断所需 Java 主版本（兜底启发式）。
    alpha/beta/远古版与 1.16.x 及更早 -> 8；1.17~1.20.4 -> 17；1.20.5 及以后 -> 21。
    快照（如 21w44a）按年份段近似映射。
    若版本 JSON 自带 javaVersion.majorVersion，以它为准（见 core.get_required_java）。
    """
    v = vid.lower()
    # 快照：两位年份 + w + 序号，如 20w14a / 24w09a
    m = re.match(r"^(\d{2})w\d+[a-z]?$", v)
    if m:
        year = int(m.group(1))
        if year >= 24:
            return 21
        if year >= 21:
            return 17
        return 8
    # alpha(a1.x)/beta(b1.x)/classic(c0.x)/rd-/inf- 等远古版本
    if re.match(r"^[a-z]", v):
        return 8
    m = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", vid)
    if not m:
        return 8
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else 0
    c = int(m.group(3)) if m.group(3) else 0
    if a < 1 or (a == 1 and b < 17):
        return 8
    if a == 1 and b == 20 and c >= 5:
        return 21
    if a == 1 and b <= 20:
        return 17
    return 21


def _adoptium_redirect_location(feature):
    """请求 Adoptium 二进制接口但不跟随重定向，返回最终文件 URL。"""
    url = ADOPTIUM_API.format(feature=feature)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        urllib.request.urlopen(req, timeout=30)
        return None  # 极少数情况下直接 200（不太可能）
    except urllib.error.HTTPError as e:
        return e.headers.get("Location")
    except Exception:
        return None


def download_java(major, dest_dir, cb=None):
    """下载并解压 Adoptium JRE 到 dest_dir，返回 java.exe 路径。

    优先使用 Adoptium 接口真实重定向得到的 GitHub 地址，失败则依次尝试
    GitHub 代理镜像；若连接口都不可达，则直接请求接口（自动跟随重定向）。
    """
    os.makedirs(dest_dir, exist_ok=True)
    location = _adoptium_redirect_location(major)

    urls = []
    if location:
        urls.append(location)
        if location.startswith("https://github.com/") or location.startswith("http://github.com/"):
            for proxy in _GITHUB_PROXIES:
                urls.append(proxy + location)
    else:
        urls.append(ADOPTIUM_API.format(feature=major))

    last_err = None
    for url in urls:
        try:
            if cb:
                cb({"type": "status", "msg": f"下载 Java {major} …"})
            tmp = os.path.join(dest_dir, f"jre-{major}.zip")
            # 避免上次失败残留的半成品压缩包导致跳过下载
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            _download_file(url, tmp, cb, name=f"Java {major}")
            if cb:
                cb({"type": "status", "msg": f"解压 Java {major} …"})
            with zipfile.ZipFile(tmp) as z:
                z.extractall(dest_dir)
            os.remove(tmp)
            for top, dirs, files in os.walk(dest_dir):
                cand = os.path.join(top, "bin", "java.exe")
                if os.path.isfile(cand):
                    return cand
            raise RuntimeError("解压后未找到 java.exe")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"无法下载 Java {major}（请手动安装后通过「浏览…」选择）：{last_err}")


# ==========================================================================
# 网络工具
# ==========================================================================
def _http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_file(url, dest, cb=None, name="文件", timeout=120):
    """流式下载到 dest，通过 cb 上报字节进度。文件已存在则跳过。"""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        if cb:
            cb({"type": "progress", "name": name,
                "downloaded": os.path.getsize(dest), "total": os.path.getsize(dest)})
        return dest

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length", "0") or "0")
            downloaded = 0
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if cb:
                        cb({"type": "progress", "name": name,
                            "downloaded": downloaded, "total": total})
    except urllib.error.HTTPError as e:
        # 报错带上具体 URL，便于定位是哪个文件失败
        raise RuntimeError(f"HTTP {e.code}（{name}）: {url}") from e
    shutil.move(tmp, dest)
    return dest


# ==========================================================================
# 微软 / Xbox / Minecraft 正版账号认证（设备代码流）
# --------------------------------------------------------------------------
# 流程：申请 device_code → 轮询换取 MS token → Xbox Live 令牌 →
#       XSTS 令牌 → Minecraft 服务令牌 → 获取正版档案（名称 + UUID）。
# 走微软标准 OAuth2 设备代码流（login.microsoftonline.com/consumers），
# 使用开源社区公共客户端 ID（mcauth3 内置，无需自行注册 Azure 应用），
# 设备代码流不需要本地回调地址，最适合桌面启动器。
# 注意：v2 端点严格要求 application/x-www-form-urlencoded 表单编码，
# 老的 login.live.com/oauth20_device.srf 端点已下线（404），不可再用。
# ==========================================================================
MS_CLIENT_ID = "4a07b708-b86d-4365-a55f-f4f23ecb85ab"
MS_DEVICE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/sts/v1.0/assert"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
MS_SCOPE = "XboxLive.signin offline_access"


def _post_json(url, data, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_form(url, data, headers=None):
    """向微软 OAuth2 v2 端点提交表单编码请求。"""
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ms_get_device_code():
    return _post_form(MS_DEVICE_URL, {
        "client_id": MS_CLIENT_ID,
        "scope": MS_SCOPE,
    })


def ms_poll_device_token(device_code, interval=5, timeout=300):
    """轮询换取 token；authorization_pending 时按 interval 重试，
    超时或用户拒绝则抛异常。"""
    deadline = time.time() + timeout
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": MS_CLIENT_ID,
        "device_code": device_code,
    }
    while time.time() < deadline:
        try:
            return _post_form(MS_TOKEN_URL, payload)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {}
            err = body.get("error", "")
            if err == "authorization_pending":
                time.sleep(interval)
                continue
            if err == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
            if err in ("expired_token", "access_denied", "authorization_declined"):
                raise RuntimeError("登录被拒绝或已超时：" + err)
            raise
        except Exception as e:
            raise RuntimeError("轮询登录失败：" + str(e))
    raise RuntimeError("登录超时，请重试")


def ms_xbox_auth(ms_access_token):
    return _post_json(XBL_AUTH_URL, {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": "d=" + ms_access_token,
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    })


def ms_xsts_auth(xbl_token):
    return _post_json(XSTS_AUTH_URL, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    })


def ms_minecraft_token(xsts_token, uhs):
    return _post_json(MC_LOGIN_URL, {
        "identityToken": f"XBL3.0 x={uhs};{xsts_token}",
    })


def ms_minecraft_profile(mc_access_token):
    req = urllib.request.Request(
        MC_PROFILE_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": "Bearer " + mc_access_token,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ms_refresh_token(refresh_token):
    return _post_form(MS_TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": MS_CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": MS_SCOPE,
    })


# ==========================================================================
# Forge 模组（基于官方 installer 的客户端安装）
# --------------------------------------------------------------------------
# 通过 Forge Maven 下载 installer.jar，并调用匹配版本的 Java 以
# 无界面方式安装到游戏目录，随后生成 <mc>-forge-<ver> 版本。
# ==========================================================================
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"


def forge_installer_url(mc_version, forge_build):
    """构造 Forge installer 下载地址。"""
    ver = f"{mc_version}-{forge_build}"
    return f"{FORGE_MAVEN}/{ver}/forge-{ver}-installer.jar"


def forge_promos():
    """返回 {mc_version: {"latest": build, "recommended": build}}。"""
    try:
        data = _http_get_json(FORGE_PROMOS)
        out = {}
        for key, build in data.get("promos", {}).items():
            if "-" not in key:
                continue
            mc, kind = key.rsplit("-", 1)
            out.setdefault(mc, {})[kind] = str(build)
        return out
    except Exception:
        return {}


# ==========================================================================
# 模组加载器版本列表（Forge / NeoForge / Fabric / OptiFine）
# ==========================================================================
def fetch_forge_versions(mc_version):
    """BMCLAPI Forge 构建列表（新→旧）。"""
    data = _http_get_json(
        f"https://bmclapi2.bangbang93.com/forge/minecraft/{mc_version}")
    vers = sorted(
        {str(d["version"]) for d in data if d.get("version")},
        key=lambda s: [int(x) if x.isdigit() else 0
                       for x in re.split(r"[.\-]", s)],
        reverse=True)
    return vers


def fetch_neoforge_versions(mc_version):
    """NeoForge 官方 maven 版本列表（新→旧），返回去掉 MC 前缀的构建号。"""
    data = _http_get_json(
        "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/forge")
    return [v[len(mc_version) + 1:] for v in data.get("versions", [])
            if v.startswith(mc_version + "-")]


def neoforge_installer_url(mc_version, build):
    ver = f"{mc_version}-{build}"
    return ("https://maven.neoforged.net/releases/net/neoforged/forge/"
            f"{ver}/forge-{ver}-installer.jar")


def fetch_fabric_versions(mc_version):
    """Fabric loader 版本列表（新→旧）。"""
    data = _http_get_json(
        f"https://meta.fabricmc.net/v2/versions/loader/{mc_version}")
    return [d["loader"]["version"] for d in data if d.get("loader")]


def fabric_profile_url(mc_version, loader_version):
    return (f"https://meta.fabricmc.net/v2/versions/loader/"
            f"{mc_version}/{loader_version}/profile/json")


def fetch_optifine_versions(mc_version):
    """BMCLAPI OptiFine 列表，返回 [{"label", "url"}]（新→旧）。"""
    data = _http_get_json(
        f"https://bmclapi2.bangbang93.com/optifine/{mc_version}")
    out = []
    for e in data:
        label = (e.get("filename") or "").replace(".jar", "")
        label = label.replace("preview_", "").replace(
            f"OptiFine_{mc_version}_", "").replace("_", " ")
        url = (f"https://bmclapi2.bangbang93.com/optifine/"
               f"{e['mcversion']}/{e['type']}/{e['patch']}")
        out.append({"label": label, "url": url,
                    "preview": str(e.get("filename", "")).startswith("preview")})
    return out


def _lib_base_url(lib):
    """库的下载基地址：优先用 lib.url（Fabric 等第三方 JSON），否则官方库。"""
    u = lib.get("url")
    if u:
        return u if u.endswith("/") else u + "/"
    return LIB_BASE


# ==========================================================================
# 版本 JSON 解析（含 inheritsFrom 继承）
# ==========================================================================
def _library_allowed(lib):
    """根据 rules 判断库是否适用于 Windows。"""
    rules = lib.get("rules")
    if not rules:
        return True
    allow = False
    for rule in rules:
        ok = True
        if "os" in rule:
            if rule["os"].get("name") != "windows":
                ok = False
        if "features" in rule:
            for feat, val in rule["features"].items():
                if val is True and feat not in SUPPORTED_FEATURES:
                    ok = False
        if ok:
            allow = (rule.get("action") == "allow")
    return allow


def _lib_maven_path(name):
    """由 maven 坐标 group:artifact:version[:classifier] 计算相对路径。"""
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) >= 4 else ""
    ext = parts[4] if len(parts) >= 5 else "jar"
    group_path = group.replace(".", "/")
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{ext}"
    return os.path.join(group_path, artifact, version, filename)


# ==========================================================================
# MinecraftCore —— 下载与启动核心
# ==========================================================================
class MinecraftCore:
    def __init__(self, game_root, mirror=None):
        self.game_root = os.path.abspath(game_root)
        self.versions_dir = os.path.join(self.game_root, "versions")
        self.libraries_dir = os.path.join(self.game_root, "libraries")
        self.assets_dir = os.path.join(self.game_root, "assets")
        self._manifest = None
        self.set_mirror(mirror or DEFAULT_MIRROR)
        for d in (self.versions_dir, self.libraries_dir, self.assets_dir):
            os.makedirs(d, exist_ok=True)

    # ---- 镜像 ----
    def set_mirror(self, key):
        """切换下载镜像源（'official' / 'bmclapi' 等）。会清空已缓存的清单。"""
        self._mirror = MIRRORS.get(key, MIRRORS[DEFAULT_MIRROR])
        self._mirror_key = key if key in MIRRORS else DEFAULT_MIRROR
        self._manifest = None

    @property
    def mirror_key(self):
        return self._mirror_key

    def mirror_name(self):
        return self._mirror["name"]

    def _rewrite(self, url):
        """将官方下载 URL 按当前镜像重写；官方源则原样返回。"""
        rules = self._mirror.get("rewrite")
        if not rules or not url:
            return url
        # 取最长匹配前缀，避免短前缀误命中
        best = None
        for prefix, target in rules.items():
            if url.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, target)
        if best:
            return best[1] + url[len(best[0]):]
        return url

    # ---- 带回退的下载 / 取 JSON ----
    @staticmethod
    def _sha1_ok(path, sha1):
        if not sha1:
            return True
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest() == sha1

    def _library_fallbacks(self, official_url):
        """libraries.minecraft.net 上缺失的老构件（如 jinput-platform），
        在 Maven 中央仓库 / BMCLAPI maven 仓库通常有存档，作为最后兜底。"""
        for host in ("https://libraries.minecraft.net/",
                     "http://libraries.minecraft.net/"):
            if official_url.startswith(host):
                path = official_url[len(host):]
                return [
                    "https://repo1.maven.org/maven2/" + path,
                    "https://bmclapi2.bangbang93.com/maven/" + path,
                ]
        return []

    def _download(self, official_url, dest, cb=None, name="文件", sha1=None):
        """依次尝试：当前镜像 → 官方源 → Maven 兜底源；校验失败自动换下一个。"""
        attempts = []
        mirrored = self._rewrite(official_url)
        if mirrored != official_url:
            attempts.append(mirrored)
        attempts.append(official_url)
        for u in self._library_fallbacks(official_url):
            if u not in attempts:
                attempts.append(u)

        last_err = None
        for i, url in enumerate(attempts):
            try:
                _download_file(url, dest, cb, name)
                if self._sha1_ok(dest, sha1):
                    return dest
                last_err = RuntimeError(f"SHA1 校验失败（{name}）: {url}")
                if cb:
                    cb({"type": "status",
                        "msg": f"文件校验失败「{name}」（源 {i + 1}/{len(attempts)}），换备用源…"})
            except Exception as e:
                last_err = e
                if cb:
                    cb({"type": "status",
                        "msg": f"源不可用「{name}」（源 {i + 1}/{len(attempts)}），换备用源…"})
            if os.path.exists(dest):
                os.remove(dest)
        raise RuntimeError(
            f"所有下载源均失败（{name}）: {official_url}") from last_err

    def _fetch_json(self, url):
        """优先走镜像取 JSON；失败时回退官方源。"""
        mirrored = self._rewrite(url)
        try:
            return _http_get_json(mirrored)
        except Exception:
            if mirrored != url:
                return _http_get_json(url)
            raise

    # ---- 版本清单 ----
    def get_manifest(self):
        if self._manifest is None:
            self._manifest = _http_get_json(self._mirror["manifest"])
        return self._manifest

    def get_remote_versions(self):
        """返回 (latest, [版本列表])，版本列表含 id/type/releaseTime。"""
        m = self.get_manifest()
        versions = [
            {"id": v["id"], "type": v["type"], "releaseTime": v.get("releaseTime", "")}
            for v in m["versions"]
        ]
        return m["latest"], versions

    def _version_url(self, vid):
        m = self.get_manifest()
        for v in m["versions"]:
            if v["id"] == vid:
                return v["url"], v["type"]
        raise ValueError(f"清单中未找到版本 {vid}")

    # ---- 解析完整版本 JSON（处理继承） ----
    def resolve_version_json(self, vid):
        """下载并合并整个继承链，返回最终版本 JSON（dict）。"""
        chain = []
        visited = set()
        current = vid
        while current and current not in visited:
            visited.add(current)
            vj = self._load_or_fetch_version_json(current)
            chain.append(vj)
            current = vj.get("inheritsFrom")

        merged = {}
        for vj in reversed(chain):  # 父在前，子在后（子覆盖标量）
            for k, v in vj.items():
                if k == "libraries":
                    merged.setdefault("libraries", [])
                    merged["libraries"].extend(v)
                elif k == "arguments":
                    merged.setdefault("arguments", {"game": [], "jvm": []})
                    for side in ("game", "jvm"):
                        if side in v:
                            merged["arguments"][side].extend(v[side])
                else:
                    merged[k] = v
        return merged

    def get_required_java(self, vid):
        """返回该版本所需的 Java 主版本号（优先使用版本 JSON 的 javaVersion 字段）。"""
        try:
            vj = self.resolve_version_json(vid)
            jv = vj.get("javaVersion")
            if jv and jv.get("majorVersion"):
                return int(jv["majorVersion"])
        except Exception:
            pass
        return required_java_for_version(vid)

    def _load_or_fetch_version_json(self, vid):
        path = os.path.join(self.versions_dir, vid, f"{vid}.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        url, _ = self._version_url(vid)
        vj = self._fetch_json(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(vj, f, ensure_ascii=False, indent=2)
        return vj

    # ---- 下载 ----
    def download_version(self, vid, with_assets=True, cb=None):
        """下载某个版本的全部内容。cb 用于进度上报。"""
        def emit(evt):
            if cb:
                cb(evt)

        try:
            emit({"type": "status", "msg": f"正在定位版本 {vid} …"})
            vj = self.resolve_version_json(vid)
            vdir = os.path.join(self.versions_dir, vid)
            os.makedirs(vdir, exist_ok=True)

            # 1. 客户端 jar
            client = vj.get("downloads", {}).get("client")
            if client and client.get("url"):
                emit({"type": "task", "name": "客户端", "current": 0, "total": 1})
                dest = os.path.join(vdir, f"{vid}.jar")
                self._download(client["url"], dest, cb, name="客户端",
                               sha1=client.get("sha1"))
                emit({"type": "taskdone", "name": "客户端"})

            # 2. 依赖库 + natives
            libs = [l for l in vj.get("libraries", []) if _library_allowed(l)]
            emit({"type": "task", "name": "依赖库", "current": 0, "total": len(libs)})
            for i, lib in enumerate(libs, 1):
                emit({"type": "task", "name": f"依赖库 {i}/{len(libs)}",
                      "current": i, "total": len(libs)})
                # 普通 artifact
                art = lib.get("downloads", {}).get("artifact")
                has_natives_win = bool(lib.get("natives", {}).get("windows"))
                if art and art.get("url"):
                    rel = art.get("path") or _lib_maven_path(lib["name"])
                    try:
                        self._download(art["url"],
                                      os.path.join(self.libraries_dir, rel),
                                      cb, name=f"库 {i}/{len(libs)}",
                                      sha1=art.get("sha1"))
                    except Exception as e:
                        if has_natives_win:
                            # 纯 natives 库（如 jinput-platform）：主 jar 不含任何
                            # class，官方 CDN 上本就缺失（404），游戏不需要它，
                            # 只需 natives jar，降级为警告继续。
                            emit({"type": "status",
                                  "msg": f"⚠ 库 {i}/{len(libs)} 主 jar 缺失"
                                         f"（仅原生库，可安全跳过）：{e}"})
                        else:
                            raise
                elif not art:  # 无 downloads，按 maven 坐标拼 URL
                    rel = _lib_maven_path(lib["name"])
                    url = _lib_base_url(lib) + rel.replace(os.sep, "/")
                    try:
                        self._download(url,
                                      os.path.join(self.libraries_dir, rel),
                                      cb, name=f"库 {i}/{len(libs)}")
                    except Exception as e:
                        if has_natives_win:
                            emit({"type": "status",
                                  "msg": f"⚠ 库 {i}/{len(libs)} 主 jar 缺失"
                                         f"（仅原生库，可安全跳过）：{e}"})
                        else:
                            raise
                # natives
                natives = lib.get("natives", {})
                classifiers = lib.get("downloads", {}).get("classifiers", {})
                cls = natives.get("windows")
                if cls and cls in classifiers:
                    c = classifiers[cls]
                    if c.get("url"):
                        base_name = lib["name"].split(":")[1]
                        version = lib["name"].split(":")[2]
                        rel = f"{base_name}-{version}-{cls}.jar"
                        gp = lib["name"].split(":")[0].replace(".", "/")
                        rel_path = c.get("path") or os.path.join(gp, base_name, version, rel)
                        self._download(c["url"],
                                      os.path.join(self.libraries_dir, rel_path),
                                      cb, name=f"原生库 {i}/{len(libs)}",
                                      sha1=c.get("sha1"))
            emit({"type": "taskdone", "name": "依赖库"})

            # 3. 资源文件
            if with_assets and vj.get("assetIndex") and vj["assetIndex"].get("url"):
                emit({"type": "status", "msg": "正在下载资源索引…"})
                idx = self._fetch_json(vj["assetIndex"]["url"])
                idx_name = vj.get("assets", "legacy")
                idx_dest = os.path.join(self.assets_dir, "indexes", f"{idx_name}.json")
                os.makedirs(os.path.dirname(idx_dest), exist_ok=True)
                with open(idx_dest, "w", encoding="utf-8") as f:
                    json.dump(idx, f, ensure_ascii=False)
                objects = idx.get("objects", {})
                emit({"type": "status",
                      "msg": f"开始并发下载 {len(objects)} 个资源文件…"})
                fixed = self._download_assets_concurrent(objects, cb)
                if fixed:
                    emit({"type": "status", "msg": f"资源文件下载完成，本次新下 {fixed} 个。"})
                emit({"type": "taskdone", "name": "资源"})

            # 写启动信息
            info = {
                "id": vid,
                "type": vj.get("type", "release"),
                "mainClass": vj.get("mainClass"),
                "assets": vj.get("assets", "legacy"),
                "releaseTime": vj.get("releaseTime", ""),
                "downloadedAt": datetime.datetime.now().isoformat(),
                "withAssets": with_assets,
            }
            with open(os.path.join(vdir, "launch.info.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)

            emit({"type": "done", "msg": f"版本 {vid} 下载完成", "info": info})
            return info
        except Exception as e:
            emit({"type": "error", "msg": str(e)})
            raise

    # ---- 已安装版本 ----
    def list_installed(self):
        out = []
        if not os.path.isdir(self.versions_dir):
            return out
        for d in sorted(os.listdir(self.versions_dir)):
            jar = os.path.join(self.versions_dir, d, f"{d}.jar")
            info_path = os.path.join(self.versions_dir, d, "launch.info.json")
            if os.path.isfile(jar):
                info = {}
                if os.path.isfile(info_path):
                    try:
                        with open(info_path, "r", encoding="utf-8") as f:
                            info = json.load(f)
                    except Exception:
                        pass
                out.append({
                    "id": d,
                    "type": info.get("type", "release"),
                    "mainClass": info.get("mainClass"),
                    "assets": info.get("assets", "legacy"),
                })
        return out

    # ---- 离线账号 UUID ----
    @staticmethod
    def offline_uuid(username):
        h = hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest()
        b = bytearray(h)
        b[6] = (b[6] & 0x0F) | 0x30  # version 3
        b[8] = (b[8] & 0x3F) | 0x80  # variant
        return str(uuid.UUID(bytes=bytes(b)))

    # ---- 资源文件并发下载 ----
    def _download_mirror_or_official(self, official_url, dest, name="文件",
                                     sha1=None, timeout=120):
        """镜像 → 官方依次尝试，返回 True 表示本次有实际下载。"""
        urls = []
        mirrored = self._rewrite(official_url)
        if mirrored and mirrored != official_url:
            urls.append(mirrored)
        urls.append(official_url)
        last = None
        for u in urls:
            try:
                _download_file(u, dest, None, name, timeout=timeout)
                if self._sha1_ok(dest, sha1):
                    return True
                last = RuntimeError(f"SHA1 校验失败（{name}）: {u}")
            except Exception as e:
                last = e
            if os.path.exists(dest):
                os.remove(dest)
        raise last or RuntimeError(f"下载失败（{name}）: {official_url}")

    def _download_assets_concurrent(self, objects, cb=None, workers=16):
        """多线程并发下载资源对象（每个文件很小，串行会极慢）。返回本次新下载数。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        emit = cb or (lambda e: None)
        keys = list(objects.keys())
        total = len(keys)
        emit({"type": "task", "name": "资源", "current": 0, "total": total})
        lock = threading.Lock()
        state = {"done": 0, "fixed": 0}

        def work(key):
            h = objects[key]["hash"]
            dest = os.path.join(self.assets_dir, "objects", h[:2], h)
            if not (os.path.isfile(dest) and os.path.getsize(dest) > 0):
                url = f"{ASSET_BASE}{h[:2]}/{h}"
                self._download_mirror_or_official(
                    url, dest, name=f"资源 {key}", sha1=h, timeout=30)
                with lock:
                    state["fixed"] += 1
            with lock:
                state["done"] += 1
                done = state["done"]
            emit({"type": "task", "name": f"资源 {done}/{total}",
                  "current": done, "total": total})

        errors = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(work, k) for k in keys]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    errors.append(e)
        if errors:
            raise RuntimeError(
                f"{len(errors)} 个资源文件下载失败，示例：{errors[0]}")
        return state["fixed"]

    # ---- 启动前完整性检查与自动补全 ----
    def _lib_dest(self, lib):
        """返回 (artifact dict 或 None, 目标绝对路径)。"""
        art = lib.get("downloads", {}).get("artifact")
        rel = None
        if art and art.get("path"):
            rel = art["path"]
        else:
            rel = _lib_maven_path(lib["name"])
        return art, os.path.join(self.libraries_dir, rel)

    def _natives_dest(self, lib):
        """返回 windows natives 分类器 (url, sha1, dest)，无则 None。"""
        cls = lib.get("natives", {}).get("windows")
        c = lib.get("downloads", {}).get("classifiers", {}).get(cls) if cls else None
        if not (c and c.get("url")):
            return None
        base_name = lib["name"].split(":")[1]
        version = lib["name"].split(":")[2]
        rel = c.get("path") or os.path.join(
            lib["name"].split(":")[0].replace(".", "/"),
            base_name, version, f"{base_name}-{version}-{cls}.jar")
        return c.get("url"), c.get("sha1"), os.path.join(self.libraries_dir, rel)

    def check_version_files(self, vid):
        """检查版本文件完整性。返回 {'client': bool, 'libs': [缺失库名], 'assetIndex': bool}。"""
        vj = self.resolve_version_json(vid)
        vdir = os.path.join(self.versions_dir, vid)
        report = {"client": True, "libs": [], "assetIndex": True}
        client = vj.get("downloads", {}).get("client")
        if client and client.get("url") and not os.path.isfile(os.path.join(vdir, f"{vid}.jar")):
            report["client"] = False
        for lib in vj.get("libraries", []):
            if not _library_allowed(lib):
                continue
            art, dest = self._lib_dest(lib)
            if (art and art.get("url")) or lib.get("url"):
                if not os.path.isfile(dest):
                    report["libs"].append(lib["name"])
                    continue
            nat = self._natives_dest(lib)
            if nat and not os.path.isfile(nat[2]):
                report["libs"].append(lib["name"] + " (natives)")
        ai = vj.get("assetIndex")
        if ai and ai.get("url"):
            idx_dest = os.path.join(self.assets_dir, "indexes",
                                    f"{vj.get('assets', 'legacy')}.json")
            if not os.path.isfile(idx_dest):
                report["assetIndex"] = False
            elif report["client"]:
                # 索引在：检查资源对象是否齐全（缺对象会导致游戏内崩溃）
                try:
                    objects = json.load(open(idx_dest, encoding="utf-8")).get("objects", {})
                    report["objectsMissing"] = sum(
                        1 for o in objects.values()
                        if not os.path.isfile(os.path.join(
                            self.assets_dir, "objects", o["hash"][:2], o["hash"])))
                except Exception:
                    report["objectsMissing"] = 0
        return report

    def ensure_version_complete(self, vid, cb=None):
        """补全缺失的客户端 / 依赖库 / 资源索引，返回补下的文件数。"""
        emit = cb or (lambda e: None)
        vj = self.resolve_version_json(vid)
        vdir = os.path.join(self.versions_dir, vid)
        fixed = 0

        client = vj.get("downloads", {}).get("client")
        if client and client.get("url"):
            dest = os.path.join(vdir, f"{vid}.jar")
            if not os.path.isfile(dest):
                emit({"type": "status", "msg": "补全缺失的客户端 jar…"})
                self._download(client["url"], dest, cb, name="客户端",
                               sha1=client.get("sha1"))
                fixed += 1

        todo = []
        for lib in vj.get("libraries", []):
            if not _library_allowed(lib):
                continue
            art, dest = self._lib_dest(lib)
            if art and art.get("url"):
                if not os.path.isfile(dest):
                    todo.append((lib, art["url"], dest, art.get("sha1"), False))
            else:
                # 无 downloads（Fabric 等第三方 JSON）：按 maven 坐标 + lib.url 拼 URL
                url = _lib_base_url(lib) + _lib_maven_path(lib["name"]).replace(os.sep, "/")
                if not os.path.isfile(dest):
                    todo.append((lib, url, dest, None, False))
            nat = self._natives_dest(lib)
            if nat and not os.path.isfile(nat[2]):
                todo.append((lib, nat[0], nat[2], nat[1], True))
        if todo:
            emit({"type": "status",
                  "msg": f"检测到 {len(todo)} 个依赖库缺失，正在自动补全…"})
            for i, (lib, url, dest, sha1, is_native) in enumerate(todo, 1):
                try:
                    self._download(url, dest, cb,
                                   name=f"补全 {i}/{len(todo)}", sha1=sha1)
                    fixed += 1
                except Exception as e:
                    if is_native or lib.get("natives", {}).get("windows"):
                        emit({"type": "status",
                              "msg": f"⚠ 补全 {i}/{len(todo)} 主 jar 缺失"
                                     f"（仅原生库，可安全跳过）：{e}"})
                    else:
                        raise

        ai = vj.get("assetIndex")
        if ai and ai.get("url"):
            idx_dest = os.path.join(self.assets_dir, "indexes",
                                    f"{vj.get('assets', 'legacy')}.json")
            if not os.path.isfile(idx_dest):
                emit({"type": "status", "msg": "补全缺失的资源索引…"})
                idx = self._fetch_json(ai["url"])
                os.makedirs(os.path.dirname(idx_dest), exist_ok=True)
                with open(idx_dest, "w", encoding="utf-8") as f:
                    json.dump(idx, f, ensure_ascii=False)
                fixed += 1
            if os.path.isfile(idx_dest):
                # 资源对象缺失会导致游戏内崩溃（如加载贴图 NoSuchFile），一并补全
                try:
                    objects = json.load(open(idx_dest, encoding="utf-8")).get("objects", {})
                    missing = {k: o for k, o in objects.items()
                               if not os.path.isfile(os.path.join(
                                   self.assets_dir, "objects",
                                   o["hash"][:2], o["hash"]))}
                    if missing:
                        emit({"type": "status",
                              "msg": f"检测到 {len(missing)} 个资源文件缺失，正在并发补全…"})
                        fixed += self._download_assets_concurrent(missing, cb)
                except Exception:
                    pass
        return fixed

    # ---- 构建启动命令 ----
    def build_launch_command(self, vid, username, java_path, account=None,
                             max_memory=2048, min_memory=None):
        vj = self.resolve_version_json(vid)
        vdir = os.path.join(self.versions_dir, vid)
        natives_dir = os.path.join(vdir, "natives")
        os.makedirs(natives_dir, exist_ok=True)

        # 收集 classpath（客户端 + 普通库），并解包 natives
        classpath = [os.path.join(vdir, f"{vid}.jar")]
        for lib in vj.get("libraries", []):
            if not _library_allowed(lib):
                continue
            art = lib.get("downloads", {}).get("artifact")
            if art and art.get("url"):
                rel = art.get("path") or _lib_maven_path(lib["name"])
            else:
                rel = _lib_maven_path(lib["name"])
            jar_path = os.path.join(self.libraries_dir, rel)
            if os.path.isfile(jar_path):
                classpath.append(jar_path)
            # 解包原生库
            natives = lib.get("natives", {})
            classifiers = lib.get("downloads", {}).get("classifiers", {})
            cls = natives.get("windows")
            if cls and cls in classifiers and classifiers[cls].get("url"):
                base_name = lib["name"].split(":")[1]
                version = lib["name"].split(":")[2]
                rel2 = f"{base_name}-{version}-{cls}.jar"
                gp = lib["name"].split(":")[0].replace(".", "/")
                native_jar = os.path.join(self.libraries_dir, gp, base_name, version, rel2)
                if os.path.isfile(native_jar):
                    self._extract_natives(native_jar, natives_dir)

        main_class = vj.get("mainClass")
        if not main_class:
            raise ValueError("版本 JSON 缺少 mainClass")

        # JVM 参数
        jvm_args = []
        if "arguments" in vj and "jvm" in vj["arguments"]:
            for a in vj["arguments"]["jvm"]:
                if isinstance(a, str):
                    jvm_args.append(a)
                else:
                    if self._rule_allows(a.get("rules")):
                        val = a["value"]
                        jvm_args.extend(val if isinstance(val, list) else [val])
        else:
            jvm_args = ["-XX:HeapDumpPath=MojangTricksIntelDontCallThisDirectly",
                        "-Djava.library.path=${natives_directory}",
                        "-cp", "${classpath}"]

        # 游戏参数
        game_args = []
        if "arguments" in vj and "game" in vj["arguments"]:
            for a in vj["arguments"]["game"]:
                if isinstance(a, str):
                    game_args.append(a)
                else:
                    if self._rule_allows(a.get("rules")):
                        val = a["value"]
                        game_args.extend(val if isinstance(val, list) else [val])
        else:
            game_args = (vj.get("minecraftArguments", "") or "").split()

        # 占位符替换
        uid = self.offline_uuid(username)
        auth_player_name = username
        auth_uuid = uid
        auth_access_token = uid
        auth_xuid = ""
        user_type = "mojang"

        # 正版（微软）账号：用真实档案覆盖令牌
        if account and account.get("type") == "microsoft":
            auth_player_name = account.get("name", username)
            auth_uuid = account.get("uuid", uid)
            auth_access_token = account.get("access_token", uid)
            auth_xuid = account.get("xuid", "")
            user_type = "msa"

        jvm_tokens = {
            "natives_directory": natives_dir,
            "launcher_name": "PlainMC",
            "launcher_version": "1.0.0",
            "classpath": os.pathsep.join(classpath),
            "classpath_separator": os.pathsep,
            "library_directory": self.libraries_dir,
            "version_name": vid,
        }
        jvm_args = [self._replace(t, jvm_tokens) for t in jvm_args]

        # 老版本（pre-1.6）用 ${game_assets} 指向资源目录，映射为
        # assets/virtual/legacy（PCL/HMCL 同样约定），必须真实存在，
        # 否则游戏从空路径读语言文件会 NullPointerException。
        legacy_assets = os.path.join(self.assets_dir, "virtual", "legacy")
        os.makedirs(legacy_assets, exist_ok=True)

        # 整合包实例：以版本目录作为独立 .minecraft 目录（mods/配置隔离）
        if os.path.isfile(os.path.join(vdir, ".mc_instance")):
            gdir = vdir
        else:
            gdir = self.game_root

        game_tokens = {
            "auth_player_name": auth_player_name,
            "version_name": vid,
            "game_directory": gdir,
            "assets_root": self.assets_dir,
            "assets_index_name": vj.get("assets", "legacy"),
            # 老版本专用
            "game_assets": legacy_assets,
            "auth_session": f"token:{auth_access_token}:{auth_uuid}",
            "user_properties": "{}",
            "auth_uuid": auth_uuid,
            "auth_access_token": auth_access_token,
            "auth_xuid": auth_xuid,
            "clientid": "00000000-0000-0000-0000-000000000000",
            "user_type": user_type,
            "version_type": vj.get("type", "release"),
        }
        game_args = [self._replace(t, game_tokens) for t in game_args]
        # 兜底：清除任何未被识别的占位符（避免新版引入的字段导致 Java 报错）
        game_args = [self._clear_tokens(a) for a in game_args]
        jvm_args = [self._clear_tokens(a) for a in jvm_args]

        # 内存
        min_memory = min_memory or min(512, max_memory)
        jvm_args.insert(0, f"-Xmx{max_memory}m")
        jvm_args.insert(0, f"-Xms{min_memory}m")

        # Windows：改用 javaw.exe（GUI 子系统）启动，避免游戏运行时弹出一个
        # 黑色控制台窗口（java.exe 是控制台程序，从 GUI 程序拉起会新开窗口）。
        if os.name == "nt":
            _javaw = os.path.join(os.path.dirname(java_path or ""), "javaw.exe")
            if os.path.isfile(_javaw):
                java_path = _javaw

        cmd = [java_path] + jvm_args + [main_class] + game_args
        return cmd, {"cwd": gdir}

    @staticmethod
    def _rule_allows(rules):
        if not rules:
            return True
        allow = False
        for rule in rules:
            ok = True
            if "os" in rule and rule["os"].get("name") != "windows":
                ok = False
            if "features" in rule:
                for feat, val in rule["features"].items():
                    if val is True and feat not in SUPPORTED_FEATURES:
                        ok = False
            if ok:
                allow = (rule.get("action") == "allow")
        return allow

    @staticmethod
    def _replace(token, mapping):
        for k, v in mapping.items():
            token = token.replace("${" + k + "}", str(v))
        return token

    @staticmethod
    def _clear_tokens(token):
        """清除任何残留的 ${...} 占位符（替换为空字符串）。"""
        return re.sub(r"\$\{[^}]+\}", "", token)

    @staticmethod
    def _extract_natives(jar_path, natives_dir):
        try:
            with zipfile.ZipFile(jar_path) as z:
                for name in z.namelist():
                    if name.endswith(".dll") or name.endswith(".so") or name.endswith(".dylib"):
                        z.extract(name, natives_dir)
        except Exception:
            pass


# ==========================================================================
# 主题（PCL2 风格 QSS）
# ==========================================================================
THEMES = {
    "light": {
        "BG": "#E9EDF7",
        "BG_TOP": "#DCE6FF",
        "CARD": "#FFFFFF",
        "CARD_BORDER": "#E4E9F2",
        "TEXT": "#2B2F3A",
        "TEXT_2": "#7A8295",
        "ACCENT": "#4C6EF5",
        "ACCENT_2": "#7C5CFC",
        "ACCENT_TEXT": "#FFFFFF",
        "HOVER": "#F1F4FC",
        "SIDEBAR": "#FFFFFF",
        "SHADOW": "rgba(40,60,120,0.10)",
        "INPUT_BG": "#F4F6FC",
        "BADGE_RELEASE": "#2FB37A",
        "BADGE_SNAPSHOT": "#E2893B",
        "BADGE_OLD": "#9AA3B8",
        "CONSOLE_BG": "#1E2230",
        "CONSOLE_TEXT": "#C9D2E3",
    },
    "dark": {
        "BG": "#15171F",
        "BG_TOP": "#1C2030",
        "CARD": "#1F2330",
        "CARD_BORDER": "#2C3142",
        "TEXT": "#E6E9F2",
        "TEXT_2": "#9AA3B8",
        "ACCENT": "#5B7CFA",
        "ACCENT_2": "#8B6CFF",
        "ACCENT_TEXT": "#FFFFFF",
        "HOVER": "#262B3A",
        "SIDEBAR": "#1A1D27",
        "SHADOW": "rgba(0,0,0,0.35)",
        "INPUT_BG": "#262B3A",
        "BADGE_RELEASE": "#37C886",
        "BADGE_SNAPSHOT": "#F0A24B",
        "BADGE_OLD": "#6B7390",
        "CONSOLE_BG": "#0E1018",
        "CONSOLE_TEXT": "#B9C2D6",
    },
}

_TEMPLATE = """
/* 注意：此处不能写 background —— 裸 QWidget 选择器会匹配 QLabel 等所有控件，
   导致每个标签文字后面都被垫一块窗口底色（盖住卡片/标题栏渐变）。
   窗口背景统一由 QWidget#MainWindow 规则负责绘制。 */
QWidget {
    font-family: "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
    color: %%TEXT%%;
}
QWidget#MainWindow {
    background: %%BG%%;
    border-radius: 14px;
}
QLabel#AppTitle {
    font-size: 17px;
    font-weight: bold;
    color: %%ACCENT_TEXT%%;
}
QLabel#PageTitle {
    font-size: 22px;
    font-weight: bold;
    color: %%TEXT%%;
}
QLabel#SubTitle {
    color: %%TEXT_2%%;
    font-size: 12px;
}
QLabel#HintText {
    color: %%TEXT_2%%;
    font-size: 12px;
}

/* 侧边栏 */
QWidget#SideBar {
    background: %%SIDEBAR%%;
    border-right: 1px solid %%CARD_BORDER%%;
}

/* 顶部横排导航（横行布局）*/
QWidget#TopBar {
    background: %%SIDEBAR%%;
    border-bottom: 1px solid %%CARD_BORDER%%;
}

/* 下载合并入口的下拉菜单 */
QMenu {
    background: %%CARD%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 10px;
    padding: 6px;
}
QMenu::item {
    color: %%TEXT%%;
    padding: 9px 22px 9px 12px;
    border-radius: 7px;
    font-size: 14px;
}
QMenu::item:selected {
    background: %%HOVER%%;
}
QMenu::item:disabled {
    color: %%TEXT_2%%;
}
QMenu::separator {
    height: 1px;
    background: %%CARD_BORDER%%;
    margin: 4px 8px;
}
QPushButton#NavButton {
    background: transparent;
    border: none;
    border-radius: 10px;
    color: %%TEXT%%;
    text-align: left;
    padding: 10px 14px;
    font-size: 14px;
}
QPushButton#NavButton:hover {
    background: %%HOVER%%;
}
QPushButton#NavButton:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
    color: %%ACCENT_TEXT%%;
    font-weight: bold;
}
QPushButton#LaunchNav {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
    color: %%ACCENT_TEXT%%;
    border: none;
    border-radius: 12px;
    font-size: 16px;
    font-weight: bold;
    padding: 12px;
}
QPushButton#LaunchNav:hover {
    opacity: 0.92;
}

/* 标题栏 */
QWidget#TitleBar {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
}
QLabel#TitleBarText {
    color: %%ACCENT_TEXT%%;
    font-size: 13px;
    font-weight: bold;
}
QPushButton#TitleBtn {
    background: transparent;
    border: none;
    color: %%ACCENT_TEXT%%;
    font-size: 14px;
    border-radius: 6px;
    padding: 4px 10px;
}
QPushButton#TitleBtn:hover {
    background: rgba(255,255,255,0.18);
}
QPushButton#TitleBtnClose:hover {
    background: #E2564B;
}

/* 启动页 */
QWidget#LaunchLeft {
    background: %%SIDEBAR%%;
    border-right: 1px solid %%CARD_BORDER%%;
}
QWidget#LaunchHero {
    background: %%BG_TOP%%;
}
QPushButton#LaunchBig {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
    border: none;
    border-radius: 14px;
    color: %%ACCENT_TEXT%%;
    text-align: left;
}
QLabel#LaunchBigTitle {
    color: %%ACCENT_TEXT%%;
    font-size: 19px;
    font-weight: bold;
}
QLabel#LaunchBigSub {
    color: %%ACCENT_TEXT%%;
    opacity: 0.88;
    font-size: 12px;
}
QPushButton#ModePill {
    background: %%INPUT_BG%%;
    border: none;
    border-radius: 8px;
    color: %%TEXT%%;
    font-size: 14px;
    padding: 6px 0;
}
QPushButton#ModePill:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
    color: %%ACCENT_TEXT%%;
    font-weight: bold;
}
QWidget#LaunchOverlay {
    background: rgba(20,24,38,0.55);
}
QWidget#LaunchOverlayCard {
    background: %%CARD%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 18px;
}
QLabel#LaunchOverlayTitle {
    color: %%TEXT%%;
    font-size: 22px;
    font-weight: bold;
}
QLabel#LaunchOverlayStatus {
    color: %%TEXT_2%%;
    font-size: 13px;
}
QProgressBar#LaunchProgress {
    background: %%INPUT_BG%%;
    border: none;
    border-radius: 5px;
    height: 10px;
}
QProgressBar#LaunchProgress::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
    border-radius: 5px;
}

/* 卡片 */
QFrame#Card {
    background: %%CARD%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 12px;
}

/* 按钮 */
QPushButton#PrimaryButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
    color: %%ACCENT_TEXT%%;
    border: none;
    border-radius: 10px;
    font-size: 14px;
    font-weight: bold;
    padding: 10px 18px;
}
QPushButton#PrimaryButton:hover { opacity: 0.92; }
QPushButton#PrimaryButton:disabled {
    background: %%CARD_BORDER%%;
    color: %%TEXT_2%%;
}
QPushButton#GhostButton {
    background: %%INPUT_BG%%;
    color: %%TEXT%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 10px;
    padding: 9px 16px;
}
QPushButton#GhostButton:hover { background: %%HOVER%%; }

/* 输入框 */
QLineEdit#SearchEdit, QLineEdit#LineEdit, QComboBox, QSpinBox {
    background: %%INPUT_BG%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 9px;
    padding: 8px 12px;
    color: %%TEXT%%;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid %%ACCENT%%;
}
QComboBox QAbstractItemView {
    background: %%CARD%%;
    color: %%TEXT%%;
    border: 1px solid %%CARD_BORDER%%;
    selection-background-color: %%ACCENT%%;
}
QSpinBox::up-button, QSpinBox::down-button { width: 18px; }

/* 版本列表 */
QListWidget {
    background: transparent;
    border: none;
    outline: 0;
}
QListWidget::item { border: none; }
QFrame#VersionRow {
    background: %%CARD%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 10px;
}
QFrame#VersionRow:hover { border: 1px solid %%ACCENT%%; }

/* 徽章 */
QLabel#Badge {
    color: #FFFFFF;
    font-size: 11px;
    font-weight: bold;
    border-radius: 6px;
    padding: 2px 8px;
    background: %%BADGE_OLD%%;
}

/* 进度条 */
QProgressBar {
    background: %%INPUT_BG%%;
    border: none;
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: %%TEXT_2%%;
    font-size: 11px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
    border-radius: 6px;
}

QScrollBar:vertical { width: 9px; background: transparent; }
QScrollBar::handle:vertical {
    background: %%CARD_BORDER%%; border-radius: 5px;
}
QScrollArea { border: none; background: transparent; }

/* 下载页：最新版本大卡片 */
QFrame#LatestCard {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %%ACCENT%%, stop:1 %%ACCENT_2%%);
    border: none;
    border-radius: 14px;
}
QFrame#LatestCard QLabel#LatestTitle {
    color: %%ACCENT_TEXT%%;
    font-size: 13px;
    font-weight: bold;
}
QFrame#LatestCard QLabel#LatestId {
    color: %%ACCENT_TEXT%%;
    font-size: 22px;
    font-weight: bold;
}
QFrame#LatestCard QLabel#LatestSub {
    color: rgba(255,255,255,0.85);
    font-size: 12px;
}

/* 下载页：分组折叠头 */
QPushButton#GroupHeader {
    background: transparent;
    border: none;
    color: %%TEXT%%;
    font-size: 14px;
    font-weight: bold;
    text-align: left;
    padding: 6px 4px;
}
QPushButton#GroupHeader:hover { color: %%ACCENT%%; }

/* 下载页：可点击版本条目 */
QFrame#VersionItem {
    background: %%CARD%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 10px;
}
QFrame#VersionItem:hover { border: 1px solid %%ACCENT%%; }

/* 详情页 */
QPushButton#BackButton {
    background: transparent;
    border: none;
    color: %%TEXT%%;
    font-size: 15px;
    font-weight: bold;
    padding: 4px 10px;
    border-radius: 8px;
}
QPushButton#BackButton:hover { background: %%HOVER%%; }
QLabel#DetailTitle { font-size: 20px; font-weight: bold; color: %%TEXT%%; }
QLabel#SubTitle { font-size: 14px; font-weight: bold; color: %%TEXT_2%%; }
QPushButton#SegButton {
    background: %%CARD%%;
    color: %%TEXT%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    padding: 6px 4px;
}
QPushButton#SegButton:hover { border: 1px solid %%ACCENT%%; }
QPushButton#SegButton:checked {
    background: %%ACCENT%%;
    color: #ffffff;
    border: 1px solid %%ACCENT%%;
}
QLabel#InstalledOk { color: #2FB37A; font-weight: bold; }
QLabel#InstalledNo { color: %%TEXT_2%%; }
"""


def get_stylesheet(theme="light", accent=None, accent2=None):
    colors = dict(THEMES.get(theme, THEMES["light"]))
    if accent:
        colors["ACCENT"] = accent
    if accent2:
        colors["ACCENT_2"] = accent2
    sheet = _TEMPLATE
    for token, value in colors.items():
        sheet = sheet.replace("%%" + token + "%%", value)
    return sheet


def derive_accent2(accent_hex):
    """由主强调色派生渐变第二段颜色（色相 +28°、略提亮）。"""
    c = QColor(accent_hex)
    if not c.isValid():
        return None
    h = c.hue() if c.hue() >= 0 else 0
    s = max(0, min(255, c.saturation()))
    v = max(0, min(255, int(c.value() * 1.08)))
    c2 = QColor.fromHsv((h + 28) % 360, s, v)
    return c2.name()


def blur_pixmap(pix, radius):
    """离屏渲染对 QPixmap 施加高斯模糊，返回新的 QPixmap。radius<=0 原样返回。"""
    if radius <= 0 or pix.isNull():
        return pix
    from PyQt6.QtWidgets import (QGraphicsScene, QGraphicsBlurEffect,
                                 QGraphicsPixmapItem)
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pix)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    item.setGraphicsEffect(effect)
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    scene.render(painter)
    painter.end()
    return out


# 个性化相关默认配置键（仅作参考；实际值存于 config）
PERSONAL_DIR = os.path.join(PROJECT_ROOT, "personalization")
BG_DIR = os.path.join(PERSONAL_DIR, "background")
MUSIC_DIR = os.path.join(PERSONAL_DIR, "music")

ACCENT_PRESETS = [
    ("经典蓝", "#4C6EF5"),
    ("梦幻紫", "#8B5CF6"),
    ("樱花粉", "#F06595"),
    ("森野绿", "#2FB37A"),
    ("活力橙", "#F08C00"),
    ("烈焰红", "#E03131"),
    ("青碧", "#0CA678"),
    ("暮光", "#7048E8"),
]


# ==========================================================================
# 小部件工厂
# ==========================================================================
def make_nav_button(icon: str, text: str) -> QPushButton:
    btn = QPushButton(f"{icon}  {text}")
    btn.setObjectName("NavButton")
    btn.setCheckable(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setMinimumHeight(42)
    return btn


def make_primary(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("PrimaryButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def make_ghost(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("GhostButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def make_card() -> QFrame:
    card = QFrame()
    card.setObjectName("Card")
    return card


_BADGE_COLORS = {
    "release": ("#FFFFFF", "#2FB37A"),
    "snapshot": ("#FFFFFF", "#E2893B"),
    "old": ("#FFFFFF", "#9AA3B8"),
    "beta": ("#FFFFFF", "#9AA3B8"),
}


def make_badge(kind: str) -> QLabel:
    label = QLabel(kind.upper())
    label.setObjectName("Badge")
    fg, bg = _BADGE_COLORS.get(kind, _BADGE_COLORS["old"])
    label.setStyleSheet(
        f"background:{bg};color:{fg};font-size:11px;font-weight:bold;"
        f"border-radius:6px;padding:2px 8px;"
    )
    return label


def _make_progress():
    bar = QProgressBar()
    bar.setTextVisible(True)
    bar.setMinimum(0)
    bar.setMaximum(100)
    return bar


def _fmt(n):
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ==========================================================================
# 后台工作线程：下载、拉取版本清单、扫描 Java
# ==========================================================================
class DownloadWorker(QThread):
    status = pyqtSignal(str)
    task = pyqtSignal(str, int, int)       # name, current, total
    progress = pyqtSignal(str, int, int)   # name, downloaded, total
    taskdone = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, core: MinecraftCore, vid: str, with_assets: bool = True):
        super().__init__()
        self.core = core
        self.vid = vid
        self.with_assets = with_assets

    def _cb(self, evt):
        t = evt.get("type")
        if t == "status":
            self.status.emit(evt["msg"])
        elif t == "task":
            self.task.emit(evt["name"], evt.get("current", 0), evt.get("total", 0))
        elif t == "progress":
            self.progress.emit(evt["name"], evt.get("downloaded", 0), evt.get("total", 0))
        elif t == "taskdone":
            self.taskdone.emit(evt["name"])
        elif t == "error":
            self.error.emit(evt["msg"])
        elif t == "done":
            self.finished.emit(evt.get("info", {}))

    def run(self):
        try:
            self.core.download_version(self.vid, self.with_assets, self._cb)
        except Exception as e:
            self.error.emit(str(e))


class FetchVersionsWorker(QThread):
    finished = pyqtSignal(dict, list)
    error = pyqtSignal(str)

    def __init__(self, core: MinecraftCore):
        super().__init__()
        self.core = core

    def run(self):
        try:
            latest, versions = self.core.get_remote_versions()
            self.finished.emit(latest, versions)
        except Exception as e:
            self.error.emit(str(e))


class JavaScanWorker(QThread):
    finished = pyqtSignal(list)

    def __init__(self, game_root=None):
        super().__init__()
        self.game_root = game_root

    def run(self):
        try:
            result = find_java(self.game_root)
        except Exception:
            result = []
        self.finished.emit(result)


class JavaPrepareWorker(QThread):
    """在后台下载并解压指定主版本的 Java，完成后返回 java.exe 路径。"""
    status = pyqtSignal(str)
    progress = pyqtSignal(str, int, int)
    error = pyqtSignal(str)
    finished = pyqtSignal(str)

    def __init__(self, major, game_root):
        super().__init__()
        self.major = major
        self.game_root = game_root

    def run(self):
        try:
            dest = os.path.join(self.game_root, ".java", f"jdk-{self.major}")
            # 已下载过则直接复用
            for top, dirs, files in os.walk(dest):
                cand = os.path.join(top, "bin", "java.exe")
                if os.path.isfile(cand):
                    self.finished.emit(cand)
                    return
            java_path = download_java(self.major, dest, cb=self._cb)
            self.finished.emit(java_path)
        except Exception as e:
            self.error.emit(str(e))

    def _cb(self, evt):
        if evt.get("type") == "status":
            self.status.emit(evt["msg"])
        elif evt.get("type") == "progress":
            self.progress.emit(evt["name"], evt.get("downloaded", 0),
                               evt.get("total", 0))


class VersionRepairWorker(QThread):
    """启动前检查版本文件完整性并自动补全缺失文件。"""
    status = pyqtSignal(str)
    progress = pyqtSignal(str, int, int)
    error = pyqtSignal(str)
    finished = pyqtSignal(int)   # 补全的文件数

    def __init__(self, core, vid):
        super().__init__()
        self.core = core
        self.vid = vid

    def run(self):
        try:
            report = self.core.check_version_files(self.vid)
            n_missing = (0 if report["client"] else 1) + len(report["libs"]) \
                        + (0 if report["assetIndex"] else 1) \
                        + report.get("objectsMissing", 0)
            if n_missing == 0:
                self.finished.emit(0)
                return
            self.status.emit(
                f"🔧 检测到 {n_missing} 个游戏文件缺失，正在自动补全…")
            fixed = self.core.ensure_version_complete(self.vid, cb=self._cb)
            self.status.emit(f"✅ 补全完成，共下载 {fixed} 个文件。")
            self.finished.emit(fixed)
        except Exception as e:
            self.error.emit(str(e))

    def _cb(self, evt):
        if evt.get("type") == "status":
            self.status.emit(evt["msg"])
        elif evt.get("type") == "progress":
            self.progress.emit(evt["name"], evt.get("downloaded", 0),
                               evt.get("total", 0))


class MicrosoftAuthWorker(QThread):
    """在后台执行设备代码登录流程，发出 devicecode / status / finished / error。"""
    devicecode = pyqtSignal(str, str)   # verification_uri, user_code
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def run(self):
        try:
            dc = ms_get_device_code()
            self.devicecode.emit(dc.get("verification_uri", ""), dc.get("user_code", ""))
            self.status.emit("等待你在浏览器中完成登录…")
            token = ms_poll_device_token(
                dc["device_code"],
                int(dc.get("interval", 5)),
                int(dc.get("expires_in", 300)),
            )
            self.status.emit("Microsoft 授权成功，正在获取 Xbox 令牌…")
            xbl = ms_xbox_auth(token["access_token"])
            xsts = ms_xsts_auth(xbl["Token"])
            uhs = xsts["DisplayClaims"]["xui"][0]["uhs"]
            self.status.emit("正在登录 Minecraft 服务…")
            mc = ms_minecraft_token(xsts["Token"], uhs)
            profile = ms_minecraft_profile(mc["access_token"])
            uuid_dashed = str(uuid.UUID(profile["id"]))
            self.finished.emit({
                "type": "microsoft",
                "name": profile["name"],
                "uuid": uuid_dashed,
                "access_token": mc["access_token"],
                "refresh_token": token.get("refresh_token", ""),
                "xuid": uhs,
                "expires_at": time.time() + float(token.get("expires_in", 3600)),
            })
        except Exception as e:
            self.error.emit(str(e))


# ==========================================================================
# 各功能页面：下载、启动、设置、关于
# ==========================================================================
class VersionItem(QFrame):
    """可点击的版本条目，点击后进入该版本的详情页。"""
    clicked = pyqtSignal(str)

    def __init__(self, version, installed):
        super().__init__()
        self.setObjectName("VersionItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.vid = version["id"]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)

        info = QVBoxLayout()
        info.setSpacing(3)
        title = QLabel(version["id"])
        title.setStyleSheet("font-size:15px;font-weight:bold;")
        sub = QLabel(f"{version['type'].upper()}  ·  {version.get('releaseTime','')[:10]}")
        sub.setObjectName("HintText")
        info.addWidget(title)
        info.addWidget(sub)
        layout.addLayout(info)
        layout.addStretch(1)

        if installed:
            b = QLabel("已下载")
            b.setStyleSheet("background:#2FB37A;color:#fff;border-radius:6px;"
                           "padding:2px 8px;font-size:11px;font-weight:bold;")
            layout.addWidget(b)
        layout.addWidget(make_badge(version["type"]))

    def mousePressEvent(self, e):
        self.clicked.emit(self.vid)


class LatestCard(QFrame):
    """下载页顶部“最新版本”大卡片，点击进入详情页。"""
    clicked = pyqtSignal(str)

    def __init__(self, version, tag):
        super().__init__()
        self.setObjectName("LatestCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.vid = version["id"]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)
        t = QLabel(tag)
        t.setObjectName("LatestTitle")
        idl = QLabel(version["id"])
        idl.setObjectName("LatestId")
        sub = QLabel(version.get("releaseTime", "")[:10])
        sub.setObjectName("LatestSub")
        layout.addWidget(t)
        layout.addWidget(idl)
        layout.addWidget(sub)

    def mousePressEvent(self, e):
        self.clicked.emit(self.vid)


class GroupSection(QWidget):
    """可折叠的版本分组（正式版 / 预览版 / 远古版）。"""

    def __init__(self, title, versions, installed_set, on_click):
        super().__init__()
        self._title = title
        self._versions = versions
        self._on_click = on_click
        self._collapsed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QPushButton(f"▾  {title}  ({len(versions)})")
        self.header.setObjectName("GroupHeader")
        self.header.clicked.connect(self._toggle)
        layout.addWidget(self.header)

        self.content = QWidget()
        cw = QVBoxLayout(self.content)
        cw.setContentsMargins(0, 6, 0, 6)
        cw.setSpacing(8)
        for v in versions:
            item = VersionItem(v, v["id"] in installed_set)
            item.clicked.connect(on_click)
            cw.addWidget(item)
        layout.addWidget(self.content)

    def _toggle(self):
        self._collapsed = not self._collapsed
        self.content.setVisible(not self._collapsed)
        arrow = "▸" if self._collapsed else "▾"
        self.header.setText(f"{arrow}  {self._title}  ({len(self._versions)})")


class LoaderVersionsWorker(QThread):
    """获取某加载器在某 MC 版本下的可用版本列表（label, value）。"""
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, loader_type, mc_version):
        super().__init__()
        self.loader_type = loader_type
        self.mc_version = mc_version

    def run(self):
        try:
            if self.loader_type == "forge":
                out = [(v, v) for v in fetch_forge_versions(self.mc_version)]
            elif self.loader_type == "neoforge":
                out = [(v, v) for v in fetch_neoforge_versions(self.mc_version)]
            elif self.loader_type == "fabric":
                out = [(v, v) for v in fetch_fabric_versions(self.mc_version)]
            elif self.loader_type == "optifine":
                out = [(it["label"], it["url"])
                       for it in fetch_optifine_versions(self.mc_version)]
            else:
                out = []
            self.finished.emit(out)
        except Exception as e:
            self.error.emit(str(e))


class InstallerInstallWorker(QThread):
    """下载并静默运行 Forge / NeoForge installer 到游戏目录。"""
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, core, mc_version, new_vid, installer_url, major,
                 game_root, loader_keyword="forge"):
        super().__init__()
        self.core = core
        self.mc_version = mc_version
        self.new_vid = new_vid
        self.installer_url = installer_url
        self.major = major
        self.game_root = game_root
        self.loader_keyword = loader_keyword

    def _cb(self, evt):
        if evt.get("type") == "status":
            self.status.emit(evt["msg"])

    def run(self):
        try:
            import tempfile
            versions_dir = os.path.join(self.game_root, "versions")
            before = set(os.listdir(versions_dir)) if os.path.isdir(versions_dir) else set()
            installed = {v["id"] for v in self.core.list_installed()}
            if self.mc_version not in installed:
                self.core.download_version(self.mc_version, cb=self._cb)
            java_path = find_java_for_major(self.major, self.game_root)
            if not java_path:
                self.status.emit(f"未找到 Java {self.major}，正在自动下载…")
                java_path = download_java(
                    self.major,
                    os.path.join(self.game_root, ".java", f"jdk-{self.major}"))
            self.status.emit("下载安装器…")
            tmp = os.path.join(tempfile.gettempdir(),
                              f"{self.new_vid}-installer.jar")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            _download_file(self.installer_url, tmp, name="安装器")
            self.status.emit("运行安装器（无界面模式）…")
            proc = subprocess.run(
                [java_path, "-Djava.awt.headless=true", "-jar", tmp,
                 "--installClient", self.game_root],
                capture_output=True, text=True, timeout=600,
                creationflags=_NO_WINDOW)
            if proc.returncode != 0:
                raise RuntimeError(
                    "安装器返回错误：\n" + (proc.stderr or proc.stdout)[:2000])
            # 安装器产生的版本目录命名不固定（Forge / NeoForge 不同），
            # 通过对比安装前后 versions/ 目录找出新生成的版本。
            after = set(os.listdir(versions_dir)) if os.path.isdir(versions_dir) else set()
            new_dirs = [d for d in (after - before) if self.loader_keyword in d]
            if not new_dirs and os.path.isfile(
                    os.path.join(versions_dir, self.new_vid, f"{self.new_vid}.json")):
                new_dirs = [self.new_vid]
            if not new_dirs:
                raise RuntimeError("安装器执行后未找到版本目录，可能安装失败。")
            chosen = new_dirs[0]
            try:
                os.remove(tmp)
            except Exception:
                pass
            self.finished.emit({"id": chosen})
        except Exception as e:
            self.error.emit(str(e))


class FabricInstallWorker(QThread):
    """写入 Fabric profile JSON 并下载其依赖库。"""
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, core, mc_version, loader_version, game_root):
        super().__init__()
        self.core = core
        self.mc_version = mc_version
        self.loader_version = loader_version
        self.game_root = game_root
        self.new_vid = f"fabric-loader-{loader_version}-{mc_version}"

    def _cb(self, evt):
        if evt.get("type") == "status":
            self.status.emit(evt["msg"])

    def run(self):
        try:
            installed = {v["id"] for v in self.core.list_installed()}
            if self.mc_version not in installed:
                self.core.download_version(self.mc_version, cb=self._cb)
            self.status.emit("下载 Fabric 配置…")
            prof = _http_get_json(
                fabric_profile_url(self.mc_version, self.loader_version))
            vdir = os.path.join(self.game_root, "versions", self.new_vid)
            os.makedirs(vdir, exist_ok=True)
            with open(os.path.join(vdir, f"{self.new_vid}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(prof, f, ensure_ascii=False, indent=2)
            self.status.emit("下载 Fabric 依赖库…")
            self.core.download_version(self.new_vid, with_assets=True, cb=self._cb)
            self.finished.emit({"id": self.new_vid})
        except Exception as e:
            self.error.emit(str(e))


class OptiFinePrepareWorker(QThread):
    """确保原版已下载并下载 OptiFine 安装器，返回 java 路径与安装器路径。"""
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, core, mc_version, installer_url, game_root):
        super().__init__()
        self.core = core
        self.mc_version = mc_version
        self.installer_url = installer_url
        self.game_root = game_root

    def _cb(self, evt):
        if evt.get("type") == "status":
            self.status.emit(evt["msg"])

    def run(self):
        try:
            import tempfile
            installed = {v["id"] for v in self.core.list_installed()}
            if self.mc_version not in installed:
                self.core.download_version(self.mc_version, cb=self._cb)
            major = self.core.get_required_java(self.mc_version)
            java_path = find_java_for_major(major, self.game_root)
            if not java_path:
                self.status.emit(f"未找到 Java {major}，正在自动下载…")
                java_path = download_java(
                    major, os.path.join(self.game_root, ".java", f"jdk-{major}"))
            self.status.emit("下载 OptiFine 安装器…")
            tmp = os.path.join(tempfile.gettempdir(), "optifine-installer.jar")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            _download_file(self.installer_url, tmp, name="OptiFine 安装器")
            self.finished.emit({"java_path": java_path, "installer": tmp})
        except Exception as e:
            self.error.emit(str(e))


class VersionDetailPage(QWidget):
    """点击某版本后进入的详情页：先选择加载器，再点上方『安装』连同加载器一起安装。"""

    LOADERS = [
        ("vanilla", "原版"),
        ("forge", "Forge"),
        ("neoforge", "NeoForge"),
        ("fabric", "Fabric"),
        ("optifine", "OptiFine"),
    ]
    # 不通用加载器：不与某模组加载器组合，单独下载安装器
    NON_UNIVERSAL = {"optifine"}

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.vid = None
        self.sel = "vanilla"
        self.version_map = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        # 顶部：返回 + 标题 + 安装按钮（上面安装）
        top = QHBoxLayout()
        back = QPushButton("\u2039 \u8fd4\u56de")
        back.setObjectName("BackButton")
        back.clicked.connect(lambda: self.main.download_page._back())
        self.title = QLabel("")
        self.title.setObjectName("DetailTitle")
        self.title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.install_btn = make_primary("\u5b89\u88c5")
        self.install_btn.setMinimumHeight(40)
        self.install_btn.setMinimumWidth(120)
        self.install_btn.clicked.connect(self._install)
        top.addWidget(back)
        top.addWidget(self.title, 1)
        top.addWidget(self.install_btn)
        root.addLayout(top)

        # 选择加载器
        sel_wrap = make_card()
        sw = QVBoxLayout(sel_wrap)
        sw.setContentsMargins(18, 16, 18, 16)
        sw.setSpacing(12)
        sl = QLabel("\u9009\u62e9\u8981\u5b89\u88c5\u7684\u7248\u672c / \u52a0\u8f7d\u5668")
        sl.setObjectName("SubTitle")
        sw.addWidget(sl)

        self.btn_group = QButtonGroup(self)
        self.btn_group.setExclusive(True)
        seg = QHBoxLayout()
        seg.setSpacing(8)
        self.seg_buttons = {}
        for key, name in self.LOADERS:
            b = QPushButton(name)
            b.setObjectName("SegButton")
            b.setCheckable(True)
            b.setMinimumHeight(36)
            b.clicked.connect(lambda _=False, k=key: self._select(k))
            self.btn_group.addButton(b)
            self.seg_buttons[key] = b
            seg.addWidget(b, 1)
        sw.addLayout(seg)

        # 版本选择
        self.pick_wrap = QWidget()
        pl = QHBoxLayout(self.pick_wrap)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)
        self.combo = QComboBox()
        self.combo.setMinimumHeight(34)
        self.reload_btn = make_ghost("\u5237\u65b0")
        self.reload_btn.setFixedWidth(80)
        self.reload_btn.clicked.connect(self._fetch_versions)
        pl.addWidget(self.combo, 1)
        pl.addWidget(self.reload_btn)
        sw.addWidget(self.pick_wrap)

        self.note = QLabel("")
        self.note.setObjectName("HintText")
        sw.addWidget(self.note)
        root.addWidget(sel_wrap)

        # 已安装状态
        status_card = make_card()
        stc = QVBoxLayout(status_card)
        stc.setContentsMargins(16, 14, 16, 14)
        self.installed_lbl = QLabel("")
        self.installed_lbl.setObjectName("HintText")
        stc.addWidget(self.installed_lbl)
        root.addWidget(status_card)

        root.addStretch(1)

        # 进度卡
        self.progress_card = make_card()
        self.progress_card.setVisible(False)
        pcard = QVBoxLayout(self.progress_card)
        pcard.setContentsMargins(16, 14, 16, 14)
        pcard.setSpacing(8)
        self.p_status = QLabel("\u51c6\u5907\u4e2d\u2026")
        self.p_status.setObjectName("HintText")
        self.p_bar = _make_progress()
        pcard.addWidget(self.p_status)
        pcard.addWidget(self.p_bar)
        root.addWidget(self.progress_card)

        # 默认选原版
        self._select("vanilla")

    def set_version(self, vid):
        self.vid = vid
        self.title.setText(vid)
        self.combo.clear()
        self.version_map = []
        self.progress_card.setVisible(False)
        # 使旧的版本列表请求失效（防止上一个版本的响应写进当前页面）
        self._current_worker = None
        self._select("vanilla")
        self._refresh_installed_status()

    def _select(self, key):
        self.sel = key
        for k, b in self.seg_buttons.items():
            b.setChecked(k == key)
        name = dict(self.LOADERS)[key]
        if key == "vanilla":
            self.pick_wrap.setVisible(False)
            self.note.setText("\u5c06\u4e0b\u8f7d\u539f\u7248\u5ba2\u6237\u7aef\u5e76\u81ea\u52a8\u8865\u5168\u8d44\u6e90\u6587\u4ef6\u3002")
            self.install_btn.setText("\u5b89\u88c5\u539f\u7248")
        else:
            self.pick_wrap.setVisible(True)
            if key in self.NON_UNIVERSAL:
                self.note.setText(
                    "OptiFine \u4e3a\u4e0d\u901a\u7528\u52a0\u8f7d\u5668\uff1a\u4e0d\u4f1a\u4e0e\u67d0\u6a21\u7ec4\u52a0\u8f7d\u5668\u7ec4\u5408\uff0c"
                    "\u5b89\u88c5\u65f6\u5c06\u76f4\u63a5\u8c03\u7528\u5b98\u65b9\u5b89\u88c5\u5668\uff0c\u8bf7\u5728\u9009\u62e9\u6e38\u620f\u76ee\u5f55\u540e\u5b89\u88c5\u3002")
            else:
                self.note.setText(
                    f"\u5c06\u8fde\u540c {name} \u4e00\u8d77\u5b89\u88c5\uff08\u81ea\u52a8\u4e0b\u8f7d\u6240\u9700 Java \u4e0e\u539f\u7248\uff09\u3002")
            self.install_btn.setText(f"\u5b89\u88c5 {name}")
            self._fetch_versions()

    def _fetch_versions(self):
        if self.sel == "vanilla" or not self.vid:
            return
        self.combo.setEnabled(False)
        self.reload_btn.setEnabled(False)
        base = self.note.text().rstrip("\u2026")
        self.note.setText(base + "\u2026\u83b7\u53d6\u7248\u672c\u5217\u8868\u4e2d")
        w = LoaderVersionsWorker(self.sel, self.vid)
        # 运行期间保持引用（登记到全局表），结束前绝不被回收；
        # 旧请求仍在跑时再次点击只会让旧结果被忽略，不会销毁线程。
        self._current_worker = w
        _track_worker(w)
        w.finished.connect(self._on_versions)
        w.error.connect(self._on_versions_error)
        w.start()

    def _on_versions(self, versions):
        if self.sender() is not getattr(self, "_current_worker", None):
            return  # 旧请求的迟到结果，丢弃
        self.combo.setEnabled(True)
        self.reload_btn.setEnabled(True)
        self.combo.clear()
        self.version_map = versions
        base = self.note.text().rstrip("\u2026")
        if not versions:
            self.combo.addItem("\uff08\u8be5\u7248\u672c\u65e0\u53ef\u7528\u7248\u672c\uff09", None)
            self.note.setText(base + "\uff1a\u8be5 MC \u7248\u672c\u53ef\u80fd\u4e0d\u652f\u6301\u6b64\u52a0\u8f7d\u5668")
            return
        for label, value in versions:
            self.combo.addItem(label, value)
        self.note.setText(base + f"\uff1a\u5171 {len(versions)} \u4e2a\u7248\u672c\uff0c\u5df2\u9009\u4e2d\u6700\u65b0")

    def _on_versions_error(self, err):
        if self.sender() is not getattr(self, "_current_worker", None):
            return  # 旧请求的迟到结果，丢弃
        self.combo.setEnabled(True)
        self.reload_btn.setEnabled(True)
        self.combo.clear()
        self.combo.addItem("\uff08\u83b7\u53d6\u5931\u8d25\uff09", None)
        self.note.setText("\u83b7\u53d6\u7248\u672c\u5217\u8868\u5931\u8d25\uff1a" + err)

    def _selected(self):
        if self.sel == "vanilla":
            return None
        return self.combo.currentData()

    def _install(self):
        if not self.vid:
            return
        if self.sel == "vanilla":
            self._download_vanilla()
            return
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, "\u8bf7\u9009\u62e9", "\u8bf7\u5148\u5728\u4e0b\u65b9\u9009\u62e9\u4e00\u4e2a\u7248\u672c\u3002")
            return
        self.install_btn.setEnabled(False)
        if self.sel in ("forge", "neoforge"):
            self._start_installer(sel)
        elif self.sel == "fabric":
            self._start_fabric(sel)
        elif self.sel == "optifine":
            self._start_optifine(sel)

    def _start_installer(self, build):
        mc = self.vid
        new_vid = f"{mc}-{self.sel}-{build}"
        url = (forge_installer_url(mc, build) if self.sel == "forge"
               else neoforge_installer_url(mc, build))
        try:
            major = self.main.core.get_required_java(mc)
        except Exception:
            major = required_java_for_version(mc)
        self._show_progress(f"\u51c6\u5907\u5b89\u88c5 {dict(self.LOADERS)[self.sel]} {build} \u2026")
        w = _track_worker(InstallerInstallWorker(
            self.main.core, mc, new_vid, url, major, self.main.core.game_root,
            self.sel))
        w.status.connect(self._set_status)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_loader_done)
        w.start()

    def _start_fabric(self, loader_version):
        mc = self.vid
        self._show_progress(f"\u51c6\u5907\u5b89\u88c5 Fabric {loader_version} \u2026")
        w = _track_worker(FabricInstallWorker(
            self.main.core, mc, loader_version, self.main.core.game_root))
        w.status.connect(self._set_status)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_loader_done)
        w.start()

    def _start_optifine(self, installer_url):
        mc = self.vid
        self._show_progress("\u51c6\u5907 OptiFine \u5b89\u88c5\u5668 \u2026")
        w = _track_worker(OptiFinePrepareWorker(
            self.main.core, mc, installer_url, self.main.core.game_root))
        w.status.connect(self._set_status)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_optifine_ready)
        w.start()

    def _refresh_installed_status(self):
        installed = {v["id"] for v in self.main.core.list_installed()}
        base = self.vid in installed
        lines = []
        lines.append(("\u539f\u7248\uff1a" + ("\u5df2\u4e0b\u8f7d" if base else "\u672a\u4e0b\u8f7d"), base))
        for key, name in self.LOADERS[1:]:
            prefixes = self._loader_prefixes(key)
            ok = any(any(i.startswith(p) for p in prefixes) for i in installed)
            lines.append((f"{name}\uff1a" + ("\u5df2\u5b89\u88c5" if ok else "\u672a\u5b89\u88c5"), ok))
        txt = "\u3000".join(("\u2705 " if ok else "\u25cb ") + s for s, ok in lines)
        self.installed_lbl.setText(txt)

    def _loader_prefixes(self, key):
        mc = self.vid
        if key == "forge":
            return (f"{mc}-forge-",)
        if key == "neoforge":
            return (f"{mc}-neoforge-", "neoforge-")
        if key == "fabric":
            return ("fabric-loader-",)
        if key == "optifine":
            return (f"OptiFine_{mc}",)
        return ()

    # ---- 进度 / 结果 ----
    def _show_progress(self, msg):
        self.progress_card.setVisible(True)
        self.p_status.setText(msg)
        self.p_bar.setRange(0, 0)

    def _set_status(self, msg):
        self.p_status.setText(msg)

    def _hide_progress(self):
        self.progress_card.setVisible(False)

    def _on_error(self, msg):
        self.p_status.setText("\u274c " + msg)
        self.p_bar.setRange(0, 100)
        self.install_btn.setEnabled(True)

    def _on_loader_done(self, info):
        self._hide_progress()
        self.install_btn.setEnabled(True)
        self._refresh_installed_status()
        if hasattr(self.main, "launch_page"):
            self.main.launch_page.refresh_installed()
        QMessageBox.information(
            self, "\u5b89\u88c5\u5b8c\u6210",
            f"{dict(self.LOADERS).get(self.sel, '')} \u5df2\u5b89\u88c5\uff0c\u53ef\u5230\u300e\u542f\u52a8\u300f\u9875\u9009\u62e9"
            f"\u300c{info['id']}\u300d\u7248\u672c\u6e38\u73a9\u3002")

    def _on_optifine_ready(self, info):
        self._hide_progress()
        self.install_btn.setEnabled(True)
        java_path = info.get("java_path")
        installer = info.get("installer")
        self._refresh_installed_status()
        QMessageBox.information(
            self, "OptiFine \u5b89\u88c5\u5668",
            f"\u5df2\u4e0b\u8f7d OptiFine \u5b89\u88c5\u5668\uff0c\u5373\u5c06\u542f\u52a8\u3002\n\u8bf7\u5728\u5f39\u51fa\u7684\u7a97\u53e3\u4e2d\u9009\u62e9"
            f"\u6e38\u620f\u76ee\u5f55\uff1a\n{self.main.core.game_root}\n\u7136\u540e\u70b9\u51fb\u5b89\u88c5\u5373\u53ef\u3002")
        try:
            import subprocess as _sp
            _sp.Popen([java_path, "-jar", installer])
        except Exception as e:
            QMessageBox.warning(self, "\u542f\u52a8\u5931\u8d25", str(e))

    def _download_vanilla(self):
        if not self.vid:
            return
        self.install_btn.setEnabled(False)
        self._show_progress(f"\u5f00\u59cb\u4e0b\u8f7d {self.vid} \u2026")
        w = _track_worker(DownloadWorker(self.main.core, self.vid, with_assets=True))
        w.status.connect(self._set_status)
        w.task.connect(lambda n, c, t: self._set_status(f"{n} ({c}/{t})"))
        w.progress.connect(lambda n, d, t: self._set_status(
            f"{n} {_fmt(d)}/{_fmt(t)}"))
        w.error.connect(self._on_error)
        w.finished.connect(self._on_vanilla_done)
        w.start()

    def _on_vanilla_done(self, info):
        self._hide_progress()
        self.install_btn.setEnabled(True)
        self._refresh_installed_status()
        if hasattr(self.main, "launch_page"):
            self.main.launch_page.refresh_installed()
        QMessageBox.information(
            self, "\u4e0b\u8f7d\u5b8c\u6210",
            f"{info.get('id')} \u4e0b\u8f7d\u5b8c\u6210\uff0c\u53ef\u5230\u300e\u542f\u52a8\u300f\u9875\u6e38\u73a9\u3002")


class DownloadPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self._all_versions = []
        self._latest = {}
        self._worker = None
        self._build_ui()
        self._fetch_versions()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QLabel("下载")
        head.setObjectName("PageTitle")
        self.source_label = QLabel()
        self.source_label.setObjectName("SubTitle")
        self.refresh_source()
        root.addWidget(head)
        root.addWidget(self.source_label)

        # 搜索栏
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("SearchEdit")
        self.search.setPlaceholderText("搜索版本，例如 1.20.1")
        self.search.textChanged.connect(self._rebuild_list)
        refresh = make_ghost("刷新列表")
        refresh.clicked.connect(self._fetch_versions)
        bar.addWidget(self.search, 4)
        bar.addWidget(refresh, 1)
        root.addLayout(bar)

        # 内部页面栈：列表 / 详情
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        self.list_view = QWidget()
        lv = QVBoxLayout(self.list_view)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(12)
        scroll.setWidget(self.scroll_content)
        lv.addWidget(scroll, 1)
        self.pages.addWidget(self.list_view)

        self.detail_view = VersionDetailPage(self.main)
        self.pages.addWidget(self.detail_view)

    def refresh_source(self):
        self.source_label.setText(
            f"通过「{self.main.core.mirror_name()}」获取《我的世界》各版本"
            f"（含依赖库与资源文件）")

    def _ensure_list(self):
        self.pages.setCurrentWidget(self.list_view)

    def _fetch_versions(self):
        self._clear_list()
        self.scroll_layout.addWidget(self._hint("正在从"
            f"「{self.main.core.mirror_name()}」获取版本清单…"))
        self._fw = FetchVersionsWorker(self.main.core)
        self._fw.finished.connect(self._on_versions)
        self._fw.error.connect(lambda e: self._show_error(e))
        self._fw.start()

    def _on_versions(self, latest, versions):
        self._latest = latest
        self._all_versions = versions
        self._rebuild_list()

    def _clear_list(self):
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _hint(self, text):
        l = QLabel(text)
        l.setObjectName("HintText")
        return l

    def _rebuild_list(self):
        self._clear_list()
        if not self._all_versions:
            return
        installed = {v["id"] for v in self.main.core.list_installed()}
        text = self.search.text().strip().lower()

        if text:
            matches = [v for v in self._all_versions
                       if text in v["id"].lower()]
            if not matches:
                self.scroll_layout.addWidget(self._hint("没有匹配的版本"))
            else:
                for v in matches:
                    item = VersionItem(v, v["id"] in installed)
                    item.clicked.connect(self._show_detail)
                    self.scroll_layout.addWidget(item)
            return

        # 最新版本卡片
        lr = self._latest.get("release")
        ls = self._latest.get("snapshot")
        if lr or ls:
            latest_frame = QWidget()
            lf = QHBoxLayout(latest_frame)
            lf.setContentsMargins(0, 0, 0, 0)
            lf.setSpacing(12)
            if lr:
                v = next((x for x in self._all_versions if x["id"] == lr),
                         {"id": lr, "type": "release", "releaseTime": ""})
                card = LatestCard(v, "最新正式版")
                card.clicked.connect(self._show_detail)
                lf.addWidget(card, 1)
            if ls:
                v = next((x for x in self._all_versions if x["id"] == ls),
                         {"id": ls, "type": "snapshot", "releaseTime": ""})
                card = LatestCard(v, "最新快照")
                card.clicked.connect(self._show_detail)
                lf.addWidget(card, 1)
            self.scroll_layout.addWidget(latest_frame)

        releases = [v for v in self._all_versions
                    if v["type"] == "release" and v["id"] != lr]
        snapshots = [v for v in self._all_versions if v["type"] == "snapshot"]
        olds = [v for v in self._all_versions
                if v["type"] in ("old_alpha", "old_beta")]

        for title, vers in [("正式版", releases),
                            ("预览版（快照）", snapshots),
                            ("远古版", olds)]:
            if vers:
                sec = GroupSection(title, vers, installed, self._show_detail)
                self.scroll_layout.addWidget(sec)

    def _show_detail(self, vid):
        self.detail_view.set_version(vid)
        self.pages.setCurrentWidget(self.detail_view)

    def _back(self):
        self.pages.setCurrentWidget(self.list_view)
        self._rebuild_list()

    def _show_error(self, msg):
        self._clear_list()
        self.scroll_layout.addWidget(self._hint("获取版本失败：" + msg))
class Spinner(QWidget):
    """一个简单的旋转加载动画（启动遮罩用）。"""
    def __init__(self, parent=None, size=48, color=None):
        super().__init__(parent)
        self._size = size
        self._angle = 0
        self._color = color or "#4C6EF5"
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self):
        if not self._timer.isActive():
            self._timer.start(30)

    def stop(self):
        self._timer.stop()
        self.update()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(self._angle)
        pen = QPen(QColor(self._color), max(3, self._size // 10),
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        r = self._size / 2 - pen.width()
        p.drawArc(int(-r), int(-r), int(r * 2), int(r * 2), 0, 270 * 16)


class LaunchPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self._proc = None
        self._build_ui()
        self.refresh_installed()
        self.refresh_accounts()

    def _build_ui(self):
        self._scan_java()

        # Java 选择状态（Java 控件已移至「版本设置」弹窗，这里只保留数据与选中项）
        self.java_items = [("自动匹配（推荐，无需手动选择）", "")]
        self.java_selected = ""
        # 内存（作为数据存储，由弹窗编辑）
        self.memory = QSpinBox()
        self.memory.setRange(512, 16384)
        self.memory.setSingleStep(256)
        self.memory.setSuffix(" MB")
        self.memory.setValue(int(self.main.config.get("memory", 2048)))

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ===== 左侧面板 =====
        left = QWidget()
        left.setObjectName("LaunchLeft")
        left.setFixedWidth(320)
        lscroll = QVBoxLayout(left)
        lscroll.setContentsMargins(24, 24, 24, 24)
        lscroll.setSpacing(14)

        # 正版 / 离线 切换
        self._build_mode_toggle(lscroll)

        # 头像 + 账号
        self.avatar = QLabel()
        self.avatar.setPixmap(self._make_avatar_pixmap(76))
        self.avatar.setFixedSize(76, 76)
        lscroll.addWidget(self.avatar, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.account_combo = QComboBox()
        self.account_combo.setMinimumHeight(38)
        self.account_combo.currentIndexChanged.connect(self._on_account_changed)
        lscroll.addWidget(self.account_combo)
        self.username = QLineEdit(self.main.config.get("username", "Steve"))
        self.username.setObjectName("LineEdit")
        self.username.setPlaceholderText("离线玩家名")
        lscroll.addWidget(self.username)

        # 启动按钮（带版本副标题）
        self._build_launch_button(lscroll)

        # 版本选择 / 版本设置
        vrow = QHBoxLayout()
        vrow.setSpacing(10)
        b_ver = make_ghost("版本选择")
        b_ver.clicked.connect(self._pick_version)
        b_set = make_ghost("版本设置")
        b_set.clicked.connect(self._open_version_settings)
        vrow.addWidget(b_ver)
        vrow.addWidget(b_set)
        lscroll.addLayout(vrow)

        lscroll.addStretch(1)
        folder_btn = make_ghost("打开游戏文件夹")
        folder_btn.clicked.connect(self._open_folder)
        lscroll.addWidget(folder_btn)

        # 一键释放内存（清理残留 java 进程 + 回收启动器缓存 + 归还工作集）
        self.mem_opt_btn = make_ghost("🧹 释放内存")
        self.mem_opt_btn.setToolTip(
            "清理游戏残留的 Java 进程、回收启动器图片缓存，"
            "并把内存归还给系统")
        self.mem_opt_btn.clicked.connect(self._on_optimize_memory)
        lscroll.addWidget(self.mem_opt_btn)

        root.addWidget(left)

        # ===== 右侧背景 =====
        self.hero = QLabel()
        self.hero.setObjectName("LaunchHero")
        self.hero.setScaledContents(True)
        root.addWidget(self.hero, 1)
        self.refresh_hero()

        # ===== 启动遮罩（动画 + 进度）=====
        self._build_launch_overlay()

        # 版本下拉（不展示，仅作数据，供版本选择菜单 / 副标题使用）
        self.version_combo = QComboBox()
        self.version_combo.currentIndexChanged.connect(self._update_launch_subtitle)

    def _field(self, label, widget):
        box = QVBoxLayout()
        box.setSpacing(6)
        l = QLabel(label)
        l.setObjectName("HintText")
        box.addWidget(l)
        if isinstance(widget, QLayout):
            box.addLayout(widget)
        else:
            box.addWidget(widget)
        return box

    def _scan_java(self):
        self._jw = JavaScanWorker(self.main.core.game_root)
        self._jw.finished.connect(self._on_java)
        self._jw.start()

    def _on_java(self, items):
        # items: [(name, path)]（来自 JavaScanWorker）
        self.java_items = [("自动匹配（推荐，无需手动选择）", "")]
        for name, path in items:
            ver = java_version(path) or ""
            label = f"手动指定：{name}  ({ver})" if ver else f"手动指定：{name}"
            self.java_items.append((label, path))

    def refresh_accounts(self):
        """重建账号下拉：离线账号 + 已保存的正版账号。"""
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        self.account_combo.addItem("离线账号（填名字即可玩）", None)
        for a in self.main.accounts:
            self.account_combo.addItem(f"正版：{a.get('name', '?')}", a.get("uuid"))
        cur = self.main.config.get("account_uuid")
        if cur:
            idx = self.account_combo.findData(cur)
            if idx >= 0:
                self.account_combo.setCurrentIndex(idx)
        self.account_combo.blockSignals(False)
        self._on_account_changed()

    def _on_account_changed(self):
        # 选择正版账号时，玩家名输入框由档案决定，置灰避免误解
        is_offline = self.account_combo.currentData() is None
        self.username.setEnabled(is_offline)
        if not is_offline:
            acc = self._selected_account()
            if acc:
                self.username.setText(acc.get("name", ""))

    def _selected_account(self):
        uid = self.account_combo.currentData()
        if uid is None:
            return None
        for a in self.main.accounts:
            if a.get("uuid") == uid:
                return a
        return None

    def _ensure_account_token(self, account):
        """若正版 token 已过期则尝试用 refresh_token 刷新，返回最新 account 或 None。"""
        if not account.get("refresh_token"):
            return account
        if account.get("expires_at", 0) - 60 > time.time():
            return account  # 仍有效
        try:
            tok = ms_refresh_token(account["refresh_token"])
            account["access_token"] = tok["access_token"]
            account["refresh_token"] = tok.get("refresh_token", account["refresh_token"])
            account["expires_at"] = time.time() + float(tok.get("expires_in", 3600))
            self.main.save_config()
            return account
        except Exception:
            return None

    def refresh_installed(self):
        self.version_combo.clear()
        installed = self.main.core.list_installed()
        if not installed:
            self.version_combo.addItem("（请先在『下载』页下载版本）")
            self.launch_btn.setEnabled(False)
            self._update_launch_subtitle()
            return
        self.launch_btn.setEnabled(True)
        for v in installed:
            self.version_combo.addItem(f"{v['id']}  [{v['type']}]", v["id"])
        self._update_launch_subtitle()

    # ---------------------------------------------------------------- 启动
    def _do_launch(self):
        if getattr(self, "_proc", None) and self._proc.state() == QProcess.ProcessState.Running:
            return
        vid = self.version_combo.currentData()
        if not vid:
            QMessageBox.information(self, "提示", "请先下载一个游戏版本。")
            return
        username = self.username.text().strip() or "Steve"
        mem = self.memory.value()

        # 先显示遮罩，后续的状态文字才不会被覆盖
        self._show_launch_overlay("正在准备启动…")

        # 自动内存分配：按物理内存计算推荐的 -Xmx（PCL 的自动分配思路）
        if self.main.config.get("auto_mem_alloc", False):
            mods = 0
            try:
                mods = len(getattr(self.main.core, "installed_mods", []) or [])
            except Exception:
                mods = 0
            rec = recommend_game_memory(mod_count=mods)
            if rec > 0:
                mem = rec
                self.memory.setValue(rec)
                self._set_launch_status(
                    f"自动分配内存：{rec} MB"
                    + (f"（检测到 {mods} 个 MOD）" if mods else ""))

        # 启动前内存优化：先给游戏腾出空间，再启动
        if self.main.config.get("mem_opt_before_launch", True):
            try:
                QApplication.processEvents()
                optimize_memory(app=QApplication.instance(), main=self.main)
            except Exception:
                pass

        # 校验正版账号 token
        account = self._selected_account()
        if account:
            account = self._ensure_account_token(account)
            if not account:
                QMessageBox.warning(
                    self, "登录失效",
                    "微软账号登录已过期且刷新失败，请到『账号』页重新登录。")
                return
            username = account.get("name", username)

        # 确定该版本所需 Java 主版本
        try:
            required_major = self.main.core.get_required_java(vid)
        except Exception:
            required_major = required_java_for_version(vid)

        # 自动匹配 / 选定 Java
        java_path = self._resolve_java(required_major)
        if java_path:
            self._launch_with(vid, username, java_path, mem, account, required_major)
        else:
            self._start_java_download_and_launch(
                vid, username, mem, account, required_major)

    def _resolve_java(self, required_major):
        """优先使用手动选择且版本匹配的 Java；否则在系统中自动查找匹配版本。"""
        if self.java_selected and os.path.isfile(self.java_selected):
            if java_major_version(self.java_selected) == required_major:
                return self.java_selected
        found = find_java_for_major(required_major, self.main.core.game_root)
        if found:
            self._add_java_item(found)
            return found
        return None

    def _add_java_item(self, path, select=False):
        """把某个 Java 路径加入可选列表（去重）；select=True 时设为当前选定项。"""
        for _, p in self.java_items:
            if p == path:
                if select:
                    self.java_selected = path
                return
        ver = java_version(path) or ""
        name = os.path.basename(os.path.dirname(path))
        label = f"手动指定：{name}  ({ver})" if ver else f"手动指定：{name}"
        self.java_items.append((label, path))
        if select:
            self.java_selected = path

    def _start_java_download_and_launch(self, vid, username, mem, account, major):
        self.launch_btn.setEnabled(False)
        self._set_launch_status(f"未检测到 Java {major}，正在自动下载并安装…")
        self._set_launch_progress(-1)
        self._jpw = JavaPrepareWorker(major, self.main.core.game_root)
        self._jpw.status.connect(self._set_launch_status)
        self._jpw.progress.connect(self._on_progress)
        self._jpw.error.connect(lambda e: self._on_java_error(e, major))
        self._jpw.finished.connect(
            lambda p: self._on_java_ready(vid, username, mem, account, major, p))
        self._jpw.start()

    def _on_java_error(self, msg, major):
        self._set_launch_status(f"Java {major} 自动下载失败")
        self.spinner.stop()
        self.launch_btn.setEnabled(True)
        QMessageBox.warning(
            self, "Java 自动下载失败",
            f"无法自动获取 Java {major}：{msg}\n\n"
            f"请手动安装 Java {major} 后通过「版本设置 → 浏览…」选择，再启动游戏。")
        self._hide_launch_overlay()

    def _on_java_ready(self, vid, username, mem, account, major, java_path):
        self._set_launch_status(f"Java {major} 已就绪，开始检查游戏文件…")
        self._add_java_item(java_path, select=True)
        self._launch_with(vid, username, java_path, mem, account, major)

    def _launch_with(self, vid, username, java_path, mem, account, required_major):
        # 按版本隔离的 MOD：把 versions/<vid>/mods 挂到游戏目录，使其真正被加载
        try:
            link_instance_mods(self.main.core, vid)
        except Exception as e:
            self._set_launch_status("挂载版本 MOD 失败（将使用全局 mods）")
        self.main.config["username"] = username
        self.main.config["memory"] = mem
        acc = self._selected_account()
        self.main.config["account_uuid"] = acc.get("uuid") if acc else None
        self.main.save_config()

        # 启动前自动补全缺失的游戏文件（客户端 / 依赖库 / 资源索引）
        self._set_launch_status("正在检查游戏文件完整性…")
        self._set_launch_progress(-1)
        self._rpw = VersionRepairWorker(self.main.core, vid)
        self._rpw.status.connect(self._set_launch_status)
        self._rpw.progress.connect(self._on_progress)
        self._rpw.error.connect(self._on_repair_error)
        self._rpw.finished.connect(
            lambda n: self._begin_launch(vid, username, java_path, mem,
                                         account, required_major))
        self._rpw.start()

    def _on_repair_error(self, msg):
        self._set_launch_status("游戏文件补全失败")
        self.spinner.stop()
        self.launch_btn.setEnabled(True)
        QMessageBox.critical(
            self, "游戏文件补全失败",
            f"启动前检查发现游戏文件缺失，且自动补全失败：\n{msg}\n\n"
            f"可到『下载』页删除该版本后重新下载。")
        self._hide_launch_overlay()

    def _begin_launch(self, vid, username, java_path, mem, account,
                      required_major):
        self.launch_btn.setEnabled(True)
        try:
            cmd, opts = self.main.core.build_launch_command(
                vid, username, java_path, account=account, max_memory=mem)
        except Exception as e:
            self._hide_launch_overlay()
            QMessageBox.critical(self, "启动失败", str(e))
            return

        self._set_launch_status(f"正在启动《{vid}》…")
        self._set_launch_progress(-1)
        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._proc.setWorkingDirectory(opts.get("cwd", self.main.core.game_root))
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.start(cmd[0], cmd[1:])

    def _on_progress(self, name, downloaded, total):
        # 进度条上方显示当前状态（如“正在补全 xxx 文件”）
        if total and total > 0:
            pct = int(downloaded / total * 100)
            self._set_launch_progress(pct)
            self._set_launch_status(
                f"正在补全：{name}  {_fmt(downloaded)}/{_fmt(total)}")
        else:
            self._set_launch_progress(-1)

    def _on_output(self):
        # 终端输出已移除：不再展示游戏进程原始日志
        pass

    def _on_finished(self, code, status):
        self._set_launch_status(f"游戏已退出（退出码 {code}）")
        self.spinner.stop()
        QTimer.singleShot(1600, self._hide_launch_overlay)
        QTimer.singleShot(1600, lambda: self.launch_btn.setEnabled(True))
        # 游戏退出后自动做一次内存优化：清掉可能残留的 java 进程，
        # 并把启动器占用的内存还给系统（可在设置页关闭）
        if self.main.config.get("mem_opt_on_exit", True):
            QTimer.singleShot(800, self._auto_optimize_memory)

    def _auto_optimize_memory(self):
        """游戏退出后的自动内存优化（静默执行，不打扰用户）。"""
        try:
            res = optimize_memory(app=QApplication.instance(), main=self.main)
            freed = res.get("freed_mb", 0)
            killed = res.get("killed", 0)
            if killed or freed >= 1:
                bits = []
                if killed:
                    bits.append(f"清理 {killed} 个残留进程")
                if freed >= 1:
                    bits.append(f"释放 {freed:.0f} MB")
                self._set_launch_status("游戏已退出 · " + "，".join(bits))
        except Exception:
            pass

    def _on_optimize_memory(self):
        """「释放内存」按钮：手动执行一次完整内存优化。"""
        btn = getattr(self, "mem_opt_btn", None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("正在释放…")
        try:
            QApplication.processEvents()
            res = optimize_memory(app=QApplication.instance(), main=self.main)
            ws, _ = process_memory_mb()
            killed = res.get("killed", 0)
            freed = res.get("freed_mb", 0)
            trimmed = res.get("trimmed", 0)

            lines = [f"当前启动器占用：{ws:.1f} MB"]
            if res.get("killed_list"):
                for _pid, nm, mb in res["killed_list"]:
                    lines.append(f"已结束：{nm}（约 {mb:.0f} MB）")
            else:
                lines.append("没有发现残留的 Java 进程")
            if trimmed:
                lines.append(f"已把 {trimmed} 个进程的工作集归还给系统")
            if freed >= 0.5:
                lines.append(f"整机可用内存增加约 {freed:.0f} MB")
            after = res.get("after", {})
            if after:
                lines.append(
                    f"整机占用 {after.get('load_percent', 0)}%"
                    f"（已用 {after.get('used_mb', 0):.0f} MB，"
                    f"剩余 {after.get('available_mb', 0):.0f} MB）")

            QMessageBox.information(self, "内存优化完成", "\n".join(lines))
        except Exception as e:
            QMessageBox.warning(self, "内存优化", f"执行时出错：\n{e}")
        finally:
            if btn is not None:
                btn.setEnabled(True)
                btn.setText("🧹 释放内存")

    def _open_folder(self):
        path = self.main.core.game_root
        if os.path.isdir(path):
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.run(["xdg-open", path], creationflags=_NO_WINDOW)

    # ---------------------------------------------------- 左侧面板控件
    def _build_mode_toggle(self, parent_layout):
        wrap = QWidget()
        wlay = QVBoxLayout(wrap)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.setSpacing(6)
        lab = QLabel("账号模式")
        lab.setObjectName("HintText")
        wlay.addWidget(lab)
        box = QHBoxLayout()
        box.setSpacing(0)
        self.mode_group = QButtonGroup(self)
        self.mode_offline = QPushButton("离线")
        self.mode_genuine = QPushButton("正版")
        for b in (self.mode_offline, self.mode_genuine):
            b.setObjectName("ModePill")
            b.setCheckable(True)
            b.setMinimumHeight(36)
            self.mode_group.addButton(b)
            box.addWidget(b)
        self.mode_offline.setChecked(True)
        self.mode_group.buttonClicked.connect(self._on_mode_toggle)
        wlay.addLayout(box)
        parent_layout.addWidget(wrap)

    def _on_mode_toggle(self):
        genuine = self.mode_genuine.isChecked()
        if genuine and not self.main.accounts:
            QMessageBox.information(
                self, "提示", "还没有正版账号，请先到『账号』页登录。")
            self.mode_offline.setChecked(True)
            return
        if genuine:
            for i in range(self.account_combo.count()):
                if self.account_combo.itemData(i) is not None:
                    self.account_combo.setCurrentIndex(i)
                    break
        else:
            self.account_combo.setCurrentIndex(0)
        self._on_account_changed()

    def _build_launch_button(self, parent_layout):
        btn = QPushButton()
        btn.setObjectName("LaunchBig")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(66)
        blay = QVBoxLayout(btn)
        blay.setContentsMargins(18, 8, 18, 8)
        blay.setSpacing(2)
        t = QLabel("启动游戏")
        t.setObjectName("LaunchBigTitle")
        self.launch_sub = QLabel("未选择版本")
        self.launch_sub.setObjectName("LaunchBigSub")
        blay.addWidget(t)
        blay.addWidget(self.launch_sub)
        btn.clicked.connect(self._do_launch)
        self.launch_btn = btn
        parent_layout.addWidget(btn)

    def _update_launch_subtitle(self):
        vid = self.version_combo.currentData()
        if vid:
            self.launch_sub.setText(vid)
            self.launch_btn.setEnabled(True)
        else:
            self.launch_sub.setText("请先在『下载』页下载版本")
            self.launch_btn.setEnabled(False)

    def _pick_version(self):
        menu = QMenu(self)
        for i in range(self.version_combo.count()):
            text = self.version_combo.itemText(i)
            data = self.version_combo.itemData(i)
            if data is None:
                a = menu.addAction(text)
                a.setEnabled(False)
                continue
            a = menu.addAction(text)
            a.triggered.connect(
                lambda _, idx=i: self.version_combo.setCurrentIndex(idx))
        sender = self.sender()
        menu.exec(sender.mapToGlobal(sender.rect().bottomLeft()))

    def _open_version_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("版本设置")
        dlg.setMinimumWidth(360)
        root = QVBoxLayout(dlg)
        card = make_card()
        form = QVBoxLayout(card)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(12)
        # 内存
        mem = QSpinBox()
        mem.setRange(512, 16384)
        mem.setSingleStep(256)
        mem.setSuffix(" MB")
        mem.setValue(self.memory.value())
        mem.setMinimumHeight(34)
        form.addLayout(self._field("分配内存", mem))
        # Java
        jc = QComboBox()
        jc.setMinimumHeight(34)
        for label, path in self.java_items:
            jc.addItem(label, path)
        sel_idx = 0
        for i, (_, p) in enumerate(self.java_items):
            if p == self.java_selected:
                sel_idx = i
                break
        jc.setCurrentIndex(sel_idx)
        brow = make_ghost("浏览…")
        jrow = QHBoxLayout()
        jrow.addWidget(jc, 1)
        jrow.addWidget(brow)
        form.addLayout(self._field("Java", jrow))

        def on_browse():
            path, _ = QFileDialog.getOpenFileName(
                dlg, "选择 java.exe", "", "Java 可执行文件 (java.exe)")
            if path:
                self._add_java_item(path)
                jc.addItem(self.java_items[-1][0], path)
                jc.setCurrentIndex(jc.count() - 1)

        brow.clicked.connect(on_browse)
        root.addWidget(card)
        btns = QHBoxLayout()
        ok = make_primary("保存")
        cancel = make_ghost("取消")
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        root.addLayout(btns)
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.memory.setValue(mem.value())
            self.java_selected = jc.currentData() or ""
            self.main.config["memory"] = mem.value()
            self.main.save_config()

    # ---------------------------------------------------- 启动遮罩
    def _build_launch_overlay(self):
        ov = QWidget(self)
        ov.setObjectName("LaunchOverlay")
        ov.hide()
        self.launch_overlay = ov
        olay = QVBoxLayout(ov)
        olay.setContentsMargins(0, 0,0, 0)
        olay.setSpacing(18)
        olay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card = QWidget()
        card.setObjectName("LaunchOverlayCard")
        card.setFixedWidth(440)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(28, 30, 28, 30)
        cl.setSpacing(16)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spinner = Spinner(card, 52, THEMES[self.main.theme]["ACCENT"])
        cl.addWidget(self.spinner, alignment=Qt.AlignmentFlag.AlignCenter)
        self.launch_big_label = QLabel("正在启动")
        self.launch_big_label.setObjectName("LaunchOverlayTitle")
        cl.addWidget(self.launch_big_label,
                    alignment=Qt.AlignmentFlag.AlignCenter)
        self.launch_status = QLabel("准备中…")
        self.launch_status.setObjectName("LaunchOverlayStatus")
        self.launch_status.setWordWrap(True)
        cl.addWidget(self.launch_status, alignment=Qt.AlignmentFlag.AlignCenter)
        self.launch_progress = QProgressBar()
        self.launch_progress.setObjectName("LaunchProgress")
        self.launch_progress.setMinimumHeight(10)
        self.launch_progress.setTextVisible(False)
        self.launch_progress.setRange(0, 100)
        cl.addWidget(self.launch_progress)
        olay.addWidget(card)

    def _show_launch_overlay(self, status="正在启动"):
        self.launch_big_label.setText("正在启动")
        self.launch_status.setText(status)
        self._set_launch_progress(-1)
        self.launch_overlay.setGeometry(self.rect())
        self.launch_overlay.raise_()
        self.launch_overlay.show()
        self.spinner.start()

    def _hide_launch_overlay(self):
        self.spinner.stop()
        self.launch_overlay.hide()

    def _set_launch_status(self, text):
        self.launch_status.setText(text)

    def _set_launch_progress(self, value):
        if value is None or value < 0:
            self.launch_progress.setRange(0, 0)   # 忙碌（不确定）进度
        else:
            self.launch_progress.setRange(0, 100)
            self.launch_progress.setValue(int(value))

    def resizeEvent(self, e):
        if getattr(self, "launch_overlay", None) and self.launch_overlay.isVisible():
            self.launch_overlay.setGeometry(self.rect())
        super().resizeEvent(e)

    # ---------------------------------------------------- 装饰图
    def _make_avatar_pixmap(self, size):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        unit = size / 8.0
        hair = QColor("#4a3118")
        skin = QColor("#e0b07a")
        dark = QColor("#2a1c0e")
        for r in range(8):
            for c in range(8):
                if r < 2 or c in (0, 7) or (r == 2 and c in (0, 7)):
                    col = hair
                else:
                    col = skin
                p.fillRect(int(c * unit), int(r * unit),
                           int(unit) + 1, int(unit) + 1, col)
        p.fillRect(int(2 * unit), int(3 * unit), int(2 * unit), int(2 * unit), dark)
        p.fillRect(int(5 * unit), int(3 * unit), int(2 * unit), int(2 * unit), dark)
        return pm

    def refresh_hero(self):
        """右侧大图：优先使用『个性化』里设置的背景图（与主窗口一致，含模糊/
        自适应），未设置时回退为克制的渐变。"""
        cache = getattr(self.main, "_bg_cache", None)
        w, h = 1100, 720
        if cache is not None and not cache.isNull():
            pm = QPixmap(w, h)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            scale = max(w / cache.width(), h / cache.height())
            dw, dh = cache.width() * scale, cache.height() * scale
            p.drawPixmap(int((w - dw) / 2.0), int((h - dh) / 2.0),
                         int(dw), int(dh), cache)
            p.end()
            self.hero.setPixmap(pm)
        else:
            self.hero.setPixmap(self._make_hero_pixmap(w, h))

    def _make_hero_pixmap(self, w, h):
        """未设置背景图时的克制回退：柔和竖向渐变 + 极淡网格，无大字水印。"""
        pm = QPixmap(w, h)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = THEMES[self.main.theme]
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, QColor(t["ACCENT"]))
        grad.setColorAt(1, QColor(t["ACCENT_2"]))
        p.fillRect(0, 0, w, h, grad)
        # 极淡网格（替代原来满屏随机方块，降低“AI 生成感”）
        p.setPen(QColor(255, 255, 255, 14))
        step = 48
        for x in range(0, w, step):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, step):
            p.drawLine(0, y, w, y)
        p.end()
        return pm


class SettingsPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)
        head = QLabel("设置")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        card = make_card()
        form = QVBoxLayout(card)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(14)

        # 游戏目录
        gdir_row = QHBoxLayout()
        self.gdir = QLineEdit(self.main.core.game_root)
        self.gdir.setObjectName("LineEdit")
        self.gdir.setReadOnly(True)
        browse = make_ghost("更改…")
        browse.clicked.connect(self._on_browse_dir)
        gdir_row.addWidget(self.gdir, 1)
        gdir_row.addWidget(browse)
        form.addLayout(self._field("游戏目录", gdir_row))

        # 主题
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["浅色", "深色"])
        self.theme_combo.setCurrentText("浅色" if self.main.theme == "light" else "深色")
        self.theme_combo.setMinimumHeight(36)
        self.theme_combo.currentTextChanged.connect(self._on_theme)
        form.addLayout(self._field("主题", self.theme_combo))

        # 下载镜像源
        self.mirror_combo = QComboBox()
        for key in MIRRORS:
            self.mirror_combo.addItem(MIRRORS[key]["name"], key)
        cur = self.main.config.get("mirror") or self.main.core.mirror_key
        idx = self.mirror_combo.findData(cur)
        if idx >= 0:
            self.mirror_combo.setCurrentIndex(idx)
        self.mirror_combo.setMinimumHeight(36)
        self.mirror_combo.currentTextChanged.connect(self._on_mirror)
        form.addLayout(self._field("下载镜像源", self.mirror_combo))

        # 导航布局（竖行 / 横行）
        self.nav_layout_combo = QComboBox()
        self.nav_layout_combo.addItem("竖行（侧边栏，原样）", "vertical")
        self.nav_layout_combo.addItem("横行（顶部，下载合并）", "horizontal")
        cur = self.main.config.get("nav_layout", "vertical")
        idx = self.nav_layout_combo.findData(cur)
        if idx >= 0:
            self.nav_layout_combo.setCurrentIndex(idx)
        self.nav_layout_combo.setMinimumHeight(36)
        self.nav_layout_combo.currentTextChanged.connect(self._on_nav_layout)
        form.addLayout(self._field("导航布局（竖行 / 横行列表）", self.nav_layout_combo))


        # 默认内存
        self.mem = QSpinBox()
        self.mem.setRange(512, 16384)
        self.mem.setSingleStep(256)
        self.mem.setSuffix(" MB")
        self.mem.setValue(int(self.main.config.get("memory", 2048)))
        self.mem.setMinimumHeight(36)
        form.addLayout(self._field("默认分配内存", self.mem))

        root.addWidget(card)

        # ============ 内存优化 ============
        opt_card = make_card()
        opt_form = QVBoxLayout(opt_card)
        opt_form.setContentsMargins(18, 16, 18, 16)
        opt_form.setSpacing(12)

        opt_title = QLabel("内存优化")
        opt_title.setObjectName("SubTitle")
        opt_form.addWidget(opt_title)

        opt_desc = QLabel(
            "清理游戏退出后残留的 Java 进程（常驻 1~2GB 物理内存），"
            "回收启动器自身的图标与贴图缓存，并把内存归还给系统 —— "
            "任务管理器里启动器那一行的数字会真实下降。")
        opt_desc.setObjectName("HintText")
        opt_desc.setWordWrap(True)
        opt_form.addWidget(opt_desc)

        # 自动清理开关
        self.chk_mem_on_exit = QCheckBox("游戏退出后自动执行内存优化")
        self.chk_mem_on_exit.setChecked(bool(
            self.main.config.get("mem_opt_on_exit", True)))
        self.chk_mem_on_exit.setMinimumHeight(32)
        opt_form.addWidget(self.chk_mem_on_exit)

        self.chk_mem_before = QCheckBox("启动游戏前先执行内存优化（给游戏腾空间）")
        self.chk_mem_before.setChecked(bool(
            self.main.config.get("mem_opt_before_launch", True)))
        self.chk_mem_before.setMinimumHeight(32)
        opt_form.addWidget(self.chk_mem_before)

        # 当前占用显示
        self.mem_usage_label = QLabel()
        self.mem_usage_label.setObjectName("HintText")
        opt_form.addWidget(self.mem_usage_label)

        # ---- 整机内存占用条（PCL 风格：已用 / 分配 / 剩余）----
        self.mem_bar = QProgressBar()
        self.mem_bar.setObjectName("LaunchProgress")
        self.mem_bar.setRange(0, 1000)
        self.mem_bar.setTextVisible(False)
        self.mem_bar.setFixedHeight(14)
        opt_form.addWidget(self.mem_bar)

        self.mem_bar_label = QLabel()
        self.mem_bar_label.setObjectName("HintText")
        opt_form.addWidget(self.mem_bar_label)

        # ---- 优化级别选择 ----
        self.mem_level = QComboBox()
        self.mem_level.addItems([
            "轻度 — 只清理残留进程与启动器缓存（最安全）",
            "中度 — 额外清空全系统进程工作集（推荐）",
            "深度 — 额外结束占内存的后台进程（最激进）",
        ])
        self.mem_level.setCurrentIndex(
            max(0, min(2, int(self.main.config.get("mem_opt_level", 1)))))
        self.mem_level.setMinimumHeight(36)
        opt_form.addWidget(self.mem_level)

        lvl_hint = QLabel(
            "「深度」会结束没有窗口、占用超过 200MB 的后台进程。"
            "系统关键进程、有窗口的程序、浏览器、编辑器以及启动器自身"
            "的父进程链都会被自动排除，但请确认重要程序已保存再使用。")
        lvl_hint.setObjectName("HintText")
        lvl_hint.setWordWrap(True)
        opt_form.addWidget(lvl_hint)

        # ---- 自动内存分配（PCL 的自动分配 -Xmx）----
        self.chk_auto_mem = QCheckBox("自动按物理内存分配游戏内存")
        self.chk_auto_mem.setChecked(bool(
            self.main.config.get("auto_mem_alloc", False)))
        self.chk_auto_mem.setMinimumHeight(32)
        opt_form.addWidget(self.chk_auto_mem)

        self.auto_mem_label = QLabel()
        self.auto_mem_label.setObjectName("HintText")
        opt_form.addWidget(self.auto_mem_label)

        # 立即优化按钮
        opt_row = QHBoxLayout()
        opt_row.setSpacing(10)
        btn_now = make_primary("立即释放内存")
        btn_now.clicked.connect(self._on_optimize_now)
        opt_row.addWidget(btn_now)

        btn_refresh = make_ghost("刷新占用")
        btn_refresh.clicked.connect(self._refresh_mem_usage)
        opt_row.addWidget(btn_refresh)
        opt_row.addStretch(1)
        opt_form.addLayout(opt_row)

        root.addWidget(opt_card)
        root.addStretch(1)

        save = make_primary("保存设置")
        save.clicked.connect(self._on_save)
        root.addWidget(save)

        # 初次进入就显示当前占用
        self._refresh_mem_usage()

    def _field(self, label, widget):
        box = QVBoxLayout()
        box.setSpacing(6)
        l = QLabel(label)
        l.setObjectName("HintText")
        box.addWidget(l)
        if isinstance(widget, QLayout):
            box.addLayout(widget)
        else:
            box.addWidget(widget)
        return box

    def _on_browse_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择游戏目录", self.gdir.text())
        if path:
            self.gdir.setText(path)

    def _on_theme(self, text):
        self.main.set_theme("light" if text == "浅色" else "dark")

    def _on_mirror(self, text):
        key = self.mirror_combo.currentData()
        if key:
            self.main.set_mirror(key)
            if hasattr(self.main, "download_page"):
                self.main.download_page.refresh_source()

    def _on_nav_layout(self, text):
        layout = self.nav_layout_combo.currentData()
        if layout:
            self.main.set_nav_layout(layout)

    def _on_save(self):
        self.main.config["memory"] = self.mem.value()
        self.main.config["mem_opt_on_exit"] = self.chk_mem_on_exit.isChecked()
        self.main.config["mem_opt_before_launch"] = self.chk_mem_before.isChecked()
        self.main.config["mem_opt_level"] = int(self.mem_level.currentIndex())
        self.main.config["auto_mem_alloc"] = self.chk_auto_mem.isChecked()
        new_dir = self.gdir.text()
        self.main.save_config()
        if new_dir != self.main.core.game_root:
            self.main.set_game_root(new_dir)
            if hasattr(self.main, "launch_page"):
                self.main.launch_page.refresh_installed()
        QMessageBox.information(self, "已保存", "设置已保存。")

    # ---------------------------------------------------- 内存优化
    def _refresh_mem_usage(self):
        """刷新启动器占用 + 整机内存占用条 + 自动分配建议。"""
        ws, pv = process_memory_mb()
        if ws > 0:
            self.mem_usage_label.setText(
                f"启动器占用：工作集 {ws:.1f} MB ｜ 私有提交 {pv:.1f} MB")
        else:
            self.mem_usage_label.setText("当前系统无法读取进程内存占用")

        # 整机占用条
        info = system_memory()
        total = info.get("total_mb", 0.0)
        used = info.get("used_mb", 0.0)
        avail = info.get("available_mb", 0.0)
        if total > 0:
            self.mem_bar.setValue(int(used / total * 1000))
            self.mem_bar_label.setText(
                f"整机内存：已用 {used:.0f} MB / 总计 {total:.0f} MB "
                f"（{info.get('load_percent', 0)}%）｜ 剩余可用 {avail:.0f} MB")

        # 自动分配建议
        if self.chk_auto_mem.isChecked():
            rec = recommend_game_memory()
            mods = 0
            try:
                mods = len(getattr(self.main.core, "installed_mods", []) or [])
            except Exception:
                mods = 0
            if mods:
                rec = recommend_game_memory(mod_count=mods)
            self.auto_mem_label.setText(
                f"建议分配给游戏：{rec} MB"
                + (f"（检测到 {mods} 个 MOD）" if mods else ""))
        else:
            self.auto_mem_label.setText(
                f"当前手动分配：{self.mem.value()} MB")
        return ws

    def _on_optimize_now(self):
        """设置页的「立即释放内存」。"""
        try:
            QApplication.processEvents()
            res = optimize_memory(app=QApplication.instance(), main=self.main)
            self._refresh_mem_usage()
            killed = res.get("killed", 0)
            freed = res.get("freed_mb", 0)
            trimmed = res.get("trimmed", 0)
            lines = []
            if res.get("killed_list"):
                for _pid, nm, mb in res["killed_list"]:
                    lines.append(f"已结束：{nm}（约 {mb:.0f} MB）")
            else:
                lines.append("没有发现残留的 Java 进程")
            if trimmed:
                lines.append(f"已把 {trimmed} 个进程的工作集归还给系统")
            if freed >= 0.5:
                lines.append(f"整机可用内存增加约 {freed:.0f} MB")
            after = res.get("after", {})
            if after:
                lines.append(
                    f"当前整机占用 {after.get('used_mb', 0):.0f} MB "
                    f"（{after.get('load_percent', 0)}%），"
                    f"剩余可用 {after.get('available_mb', 0):.0f} MB")
            QMessageBox.information(
                self, "内存优化完成", "\n".join(lines))
        except Exception as e:
            QMessageBox.warning(self, "内存优化", f"执行时出错：\n{e}")

    def showEvent(self, e):
        super().showEvent(e)
        # 每次进入设置页刷新一次占用数字
        self._refresh_mem_usage()


class AboutPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QLabel("关于")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(8)
        box.addWidget(QLabel(f"{APP_NAME}  v{APP_VERSION}"))
        box.addWidget(QLabel("一个仿 PCL2 美术风格的《我的世界》启动器（Python + PyQt6）。"))
        box.addWidget(QLabel("· 支持从官方源 / 国内镜像(BMCLAPI) 下载正式版 / 快照 / 旧版"))
        box.addWidget(QLabel("· 自动处理依赖库、原生库与资源文件"))
        box.addWidget(QLabel("· 离线 / 正版(微软) 双账号，Forge 一键安装"))
        box.addWidget(QLabel("· 启动前自动检测并匹配/下载所需 Java 版本"))
        box.addWidget(QLabel("· 种子地图：网页地图与离线 cubiomes 双模式，含种子搜索"))
        box.addWidget(QLabel("· 配方生成器：使用游戏真实材质图标"))
        box.addWidget(QLabel("· 像素级皮肤编辑器，可一键同步进游戏"))
        box.addWidget(QLabel("· MOD / 整合包 / 数据包 / 资源包 / 光影包 浏览下载"))
        root.addWidget(card)
        root.addStretch(1)


# ==========================================================================
# 微软账号登录对话框（设备代码流）
# ==========================================================================
class MicrosoftLoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("微软账号登录")
        self.setMinimumSize(480, 240)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.account = None
        self._build_ui()
        self._start()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        title = QLabel("登录正版微软账号")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        hint = QLabel("将在浏览器中完成微软 / Xbox 授权，本启动器不会读取你的密码。")
        hint.setObjectName("SubTitle")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # 设备代码展示卡片
        card = make_card()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(18, 16, 18, 16)
        cv.setSpacing(10)
        self.uri_label = QLabel("正在申请登录码…")
        self.uri_label.setObjectName("HintText")
        self.uri_label.setWordWrap(True)
        self.code_label = QLabel("")
        self.code_label.setStyleSheet(
            "font-size:28px;font-weight:bold;color:#4C6EF5;letter-spacing:2px;")
        cv.addWidget(self.uri_label)
        cv.addWidget(self.code_label)
        root.addWidget(card)

        self.status_label = QLabel("")
        self.status_label.setObjectName("SubTitle")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        row = QHBoxLayout()
        self.copy_btn = make_ghost("复制代码")
        self.copy_btn.clicked.connect(self._copy_code)
        self.open_btn = make_ghost("打开浏览器")
        self.open_btn.clicked.connect(self._open_browser)
        self.cancel_btn = make_ghost("取消")
        self.cancel_btn.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(self.copy_btn)
        row.addWidget(self.open_btn)
        row.addWidget(self.cancel_btn)
        root.addLayout(row)

        self._uri = ""
        self._code = ""

    def _start(self):
        self._worker = MicrosoftAuthWorker()
        self._worker.devicecode.connect(self._on_devicecode)
        self._worker.status.connect(self.status_label.setText)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_devicecode(self, uri, code):
        self._uri = uri
        self._code = code
        self.uri_label.setText(f"请用浏览器访问：{uri}\n并输入下面的代码：")
        self.code_label.setText(code)
        self.open_btn.setEnabled(bool(uri))

    def _copy_code(self):
        if self._code:
            QApplication.clipboard().setText(self._code)
            self.status_label.setText("已复制代码到剪贴板。")

    def _open_browser(self):
        if self._uri:
            webbrowser.open(self._uri)

    def _on_done(self, account):
        self.account = account
        self.accept()

    def _on_error(self, msg):
        self.status_label.setText("❌ " + msg)
        self.cancel_btn.setText("关闭")


# ==========================================================================
# 账号页面
# ==========================================================================
class AccountsPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)
        head = QLabel("账号")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        tip = QLabel("支持离线账号（填名字即可玩）与正版微软账号。正版账号可进入正版服务器、拥有皮肤与成就。")
        tip.setObjectName("SubTitle")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.list = QListWidget()
        self.list.setSpacing(8)
        root.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.add_btn = make_primary("添加微软账号")
        self.add_btn.clicked.connect(self._add)
        self.del_btn = make_ghost("删除选中")
        self.del_btn.clicked.connect(self._delete)
        row.addStretch(1)
        row.addWidget(self.del_btn)
        row.addWidget(self.add_btn)
        root.addLayout(row)

    def refresh(self):
        self.list.clear()
        self.list.addItem("离线账号  ·  无需登录，填名字即可游玩")
        for a in self.main.accounts:
            item = QListWidgetItem(
                f"正版：{a.get('name','?')}  (UUID: {a.get('uuid','')})")
            self.list.addItem(item)

    def _add(self):
        dlg = MicrosoftLoginDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.account:
            acc = dlg.account
            # 去重（同 UUID 则更新）
            self.main.accounts[:] = [
                x for x in self.main.accounts if x.get("uuid") != acc.get("uuid")]
            self.main.accounts.append(acc)
            self.main.save_config()
            self.refresh()
            if hasattr(self.main, "launch_page"):
                self.main.launch_page.refresh_accounts()
            QMessageBox.information(self, "登录成功",
                                    f"已添加正版账号：{acc.get('name')}")

    def _delete(self):
        idx = self.list.currentRow()
        if idx <= 0:
            return
        # idx 0 为离线账号，不可删；正版账号从 1 开始
        acc = self.main.accounts[idx - 1]
        self.main.accounts.remove(acc)
        self.main.save_config()
        if self.main.config.get("account_uuid") == acc.get("uuid"):
            self.main.config["account_uuid"] = None
            self.main.save_config()
        self.refresh()
        if hasattr(self.main, "launch_page"):
            self.main.launch_page.refresh_accounts()


# ==========================================================================
# 主窗口：无边框 + 渐变标题栏 + 左侧导航 + 页面栈
# ==========================================================================
class TitleBar(QWidget):
    def __init__(self, main):
        super().__init__()
        self.setObjectName("TitleBar")
        # 自定义 QWidget 子类必须开启此属性，否则 QSS 背景（渐变）不会绘制
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(40)
        self._drag = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.setSpacing(4)

        self.title_label = QLabel()
        self.title_label.setObjectName("TitleBarText")
        layout.addWidget(self.title_label)
        self.logo_label = QLabel()
        self.logo_label.setFixedSize(26, 26)
        self.logo_label.setScaledContents(True)
        self.logo_label.hide()
        layout.addWidget(self.logo_label)
        layout.addStretch(1)
        self.update_title()

        for sym, slot, cls in [
            ("—", lambda: main.showMinimized(), "TitleBtn"),
            ("▢", lambda: main.toggle_max(), "TitleBtn"),
            ("✕", lambda: main.close(), "TitleBtnClose"),
        ]:
            b = QPushButton(sym)
            b.setObjectName(cls)
            b.setFixedSize(34, 26)
            b.clicked.connect(slot)
            layout.addWidget(b)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            delta = e.globalPosition().toPoint() - self._drag
            self.window().move(self.window().pos() + delta)
            self._drag = e.globalPosition().toPoint()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def update_title(self):
        """根据个性化设置渲染标题栏：无 / 默认 / 文本 / 图片。"""
        cfg = getattr(self.window(), "config", None) or {}
        mode = cfg.get("title_mode", "default")
        if mode == "none":
            self.title_label.hide()
            self.logo_label.hide()
        elif mode == "image":
            logo = cfg.get("title_logo", "")
            pm = QPixmap(logo) if logo else QPixmap()
            if not pm.isNull():
                self.logo_label.setPixmap(pm)
                self.logo_label.show()
            else:
                self.logo_label.hide()
            self.title_label.hide()
        else:  # default / text
            text = (cfg.get("title_text") or f"{APP_NAME} 启动器") \
                if mode == "text" else f"{APP_NAME} 启动器"
            self.title_label.setText(text)
            self.title_label.show()
            self.logo_label.hide()


# ==========================================================================
# MOD 管理页：自动检测版本 → 已安装则管理该版本 mods 文件夹（本地 + Modrinth 仓库下载）；
#               未安装则一键跳到下载页。
# MOD 按版本隔离，存放于 versions/<版本>/mods，启动时由启动器挂到游戏目录真正加载。
# ==========================================================================
# ==========================================================================
# MOD 管理页（PCL2 风格「搜索 Mod」）：自动检测版本 → 已安装则管理该版本
# mods 文件夹（本地 + Modrinth 仓库搜索下载）；未安装则一键跳到下载页。
# MOD 按版本隔离，存放于 versions/<版本>/mods，启动时由启动器挂到游戏目录。
# ==========================================================================
# ==========================================================================
# MOD 管理页（PCL2 风格「搜索 Mod」）：默认按下载量浏览全部 MOD，滑到底
# 「下一页」翻页；点开 MOD 进入详情页（MC 版本筛选 + 按「加载器+游戏版本」
# 分组展开）选择具体文件下载。MOD 按版本隔离存放于 versions/<版本>/mods，
# 未下载任何游戏版本时引导去下载页。启动时由启动器把该目录挂到游戏目录。
# ==========================================================================
MODRINTH_API = "https://api.modrinth.com/v2"
MOD_PAGE_SIZE = 30

# 单次请求超时（秒）。Modrinth 在国内线路不稳定，超时设短 + 自动重试，
# 比「卡 60 秒然后失败」体验好得多。
MODRINTH_TIMEOUT = 12

# 本地缓存目录（API 响应 + 图标）：放在 exe/脚本 同级的 cache\modrinth 下
MODRINTH_CACHE_DIR = os.path.join(_app_dir(), "cache", "modrinth")


def _mod_cache_path(key, ext):
    """缓存文件路径；key 用 sha1 缩短，避免 Windows 路径长度/非法字符问题。"""
    try:
        os.makedirs(MODRINTH_CACHE_DIR, exist_ok=True)
    except Exception:
        return None
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(MODRINTH_CACHE_DIR, h + ext)


def _mod_cache_read(key, ext, max_age):
    """读缓存；max_age 为有效期（秒），超期返回 None。"""
    p = _mod_cache_path(key, ext)
    if not p or not os.path.exists(p):
        return None
    try:
        if max_age and (time.time() - os.path.getmtime(p)) > max_age:
            return None
        with open(p, "rb") as f:
            return f.read()
    except Exception:
        return None


def _mod_cache_write(key, ext, data):
    """写缓存（先写临时文件再原子替换，避免并发写坏文件）。"""
    p = _mod_cache_path(key, ext)
    if not p:
        return
    try:
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except Exception:
        pass


# Modrinth 分类 → 中文名（用于「类型」筛选与结果标签）
MOD_CATEGORIES = [
    ("optimization", "性能优化"),
    ("utility", "实用"),
    ("decoration", "装饰"),
    ("technology", "科技"),
    ("magic", "魔法"),
    ("adventure", "冒险"),
    ("worldgen", "世界生成"),
    ("mob", "生物"),
    ("equipment", "装备"),
    ("food", "食物"),
    ("storage", "存储"),
    ("transportation", "交通"),
    ("game-mechanics", "游戏机制"),
    ("library", "前置 API"),
    ("economy", "经济"),
    ("education", "教育"),
    ("management", "管理"),
    ("minigame", "小游戏"),
    ("social", "社交"),
]
MOD_CATEGORY_CN = dict(MOD_CATEGORIES)

MOD_LOADER_CN = {
    "fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge",
    "quilt": "Quilt", "liteloader": "LiteLoader", "rift": "Rift",
}

# MOD 数据源中文名（来源标签 / 详情页按钮）

def _ver_sort_key(s):
    """版本号排序键：'26.3' > '1.21.4' > '1.16.5'（按数字逐段比较）。"""
    parts = []
    for x in re.split(r"[._\-+]", str(s)):
        if x.isdigit():
            parts.append((0, int(x), ""))
        else:
            parts.append((1, 0, x))
    return parts


def fmt_downloads_cn(n):
    """下载数按中文习惯格式化：2.51 亿 / 18.4 万。"""
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 100000000:
        return f"{n / 100000000:.2f} 亿"
    if n >= 10000:
        return f"{n / 10000:.1f} 万"
    return str(n)


def fmt_rel_time(iso):
    """ISO 时间 → 「6 天前 / 3 个月前 / 1 年前」。"""
    try:
        s = str(iso).replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        sec = max(0, (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds())
    except Exception:
        return ""
    if sec < 3600:
        return f"{max(1, int(sec // 60))} 分钟前"
    if sec < 86400:
        return f"{int(sec // 3600)} 小时前"
    if sec < 86400 * 30:
        return f"{int(sec // 86400)} 天前"
    if sec < 86400 * 365:
        return f"{int(sec // (86400 * 30))} 个月前"
    return f"{int(sec // (86400 * 365))} 年前"


def _http_get_json(url, timeout=MODRINTH_TIMEOUT, retries=2):
    """带重试的 JSON 请求。

    Modrinth 在国内线路偶发 SSL 断开 / 握手超时（实测出现过 20 秒后
    SSL: UNEXPECTED_EOF），重试一次基本都能救回；超时设短一些，
    免得界面长时间停在「正在搜索…」。
    """
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(0.5 * (i + 1))
    raise last


def _modrinth_search_cached(project_type, query=None, game_version=None,
                            category=None, limit=MOD_PAGE_SIZE, offset=0):
    """Modrinth 搜索统一实现（重试 + 磁盘缓存），各类型共用。

    - 超时 12 秒且自动重试 2 次：国内线路偶发 SSL 断开，原先 60 秒超时
      会让页面长时间停在「正在搜索…」。
    - 结果按查询条件缓存 10 分钟：重复搜索/翻页回来时秒回。
    """
    q = urllib.parse.quote(query or "")
    facets = [[f"project_type:{project_type}"]]
    if game_version:
        facets.append([f"versions:{game_version}"])
    if category:
        facets.append([f"categories:{category}"])
    # 无关键词浏览时按下载量排序，有关键词时按相关度
    index = "relevance" if (query or "").strip() else "downloads"
    url = (f"{MODRINTH_API}/search?query={q}&facets="
           + urllib.parse.quote(json.dumps(facets))
           + f"&limit={limit}&offset={offset}&index={index}")
    key = f"search:{project_type}:{url}"
    cached = _mod_cache_read(key, ".json", 600)
    if cached is not None:
        try:
            d = json.loads(cached.decode("utf-8"))
            return d.get("hits", []), int(d.get("total_hits", 0))
        except Exception:
            pass
    d = _http_get_json(url)
    _mod_cache_write(key, ".json", json.dumps(d).encode("utf-8"))
    return d.get("hits", []), int(d.get("total_hits", 0))


def _mod_cache_purge(max_age=7 * 24 * 3600):
    """清理过期缓存文件，避免 cache\\modrinth 无限膨胀。"""
    try:
        if not os.path.isdir(MODRINTH_CACHE_DIR):
            return
        now = time.time()
        for name in os.listdir(MODRINTH_CACHE_DIR):
            p = os.path.join(MODRINTH_CACHE_DIR, name)
            try:
                if os.path.isfile(p) and (now - os.path.getmtime(p)) > max_age:
                    os.remove(p)
            except Exception:
                continue
    except Exception:
        pass


def modrinth_search(query, game_version=None, category=None,
                    limit=MOD_PAGE_SIZE, offset=0):
    return _modrinth_search_cached("mod", query, game_version, category,
                                   limit, offset)


def modrinth_project(project_id):
    key = "project:" + str(project_id)
    cached = _mod_cache_read(key, ".json", 86400)     # 1 天
    if cached is not None:
        try:
            return json.loads(cached.decode("utf-8"))
        except Exception:
            pass
    d = _http_get_json(f"{MODRINTH_API}/project/{project_id}")
    _mod_cache_write(key, ".json", json.dumps(d).encode("utf-8"))
    return d


def modrinth_versions(project_id):
    key = "versions:" + str(project_id)
    cached = _mod_cache_read(key, ".json", 3600)       # 1 小时
    if cached is not None:
        try:
            return json.loads(cached.decode("utf-8"))
        except Exception:
            pass
    d = _http_get_json(f"{MODRINTH_API}/project/{project_id}/version")
    _mod_cache_write(key, ".json", json.dumps(d).encode("utf-8"))
    return d


def _http_download(url, dest, timeout=240, headers=None, progress_cb=None,
                   threads=16, min_chunk=200_000):
    """下载文件到 dest。

    大文件自动改成 HTTP Range 分块并行下载。实测 Modrinth CDN 在国内是
    **按连接限速**的：同一个文件单连接 62 秒，8 连接只要 7.7 秒（快 8 倍），
    16 连接收益趋平。所以这里默认 16 路并发、每块最小 200KB。
    progress_cb(done, total) 可选，用于显示进度（total 为 0 表示未知大小）。
    """
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)

    # 1) 探测文件大小与是否支持 Range
    total = 0
    supports_range = False
    try:
        probe = dict(hdrs)
        probe["Range"] = "bytes=0-0"
        req = urllib.request.Request(url, headers=probe)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cr = r.headers.get("Content-Range", "")
            if getattr(r, "status", 0) == 206 and "/" in cr:
                supports_range = True
                try:
                    total = int(cr.rsplit("/", 1)[-1])
                except Exception:
                    total = 0
    except Exception:
        pass

    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # 2) 不支持 Range 或文件不大 → 单流下载
    if not supports_range or total < min_chunk:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            got = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(got, total)
                        except Exception:
                            pass
        return

    # 3) 分块并行
    from concurrent.futures import ThreadPoolExecutor, as_completed
    n = max(2, min(threads, total // min_chunk + 1))
    part_size = (total + n - 1) // n
    parts = [(i, i * part_size, min(total, (i + 1) * part_size) - 1)
             for i in range(n)]
    tmpdir = dest + ".parts"
    os.makedirs(tmpdir, exist_ok=True)
    done = [0] * n
    lock = threading.Lock()

    def fetch_part(i, s, e):
        req = urllib.request.Request(
            url, headers={**hdrs, "Range": f"bytes={s}-{e}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        with open(os.path.join(tmpdir, f"part{i}"), "wb") as f:
            f.write(data)
        with lock:
            done[i] = len(data)
            if progress_cb:
                try:
                    progress_cb(sum(done), total)
                except Exception:
                    pass

    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(fetch_part, *p) for p in parts]
        for f in as_completed(futs):
            f.result()      # 任一块失败则抛出，交给上层报错

    # 4) 合并分片
    with open(dest, "wb") as out:
        for i in range(n):
            p = os.path.join(tmpdir, f"part{i}")
            with open(p, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    out.write(b)
    # 5) 清理分片
    try:
        for i in range(n):
            os.remove(os.path.join(tmpdir, f"part{i}"))
        os.rmdir(tmpdir)
    except Exception:
        pass


def link_instance_mods(core, vid):
    """把按版本隔离的 MOD 挂到游戏目录，使 Forge/Fabric/NeoForge 真正加载到
    versions/<vid>/mods（saves 等仍留在根目录，不回归已有存档）。
    用 mklink /J 建立 junction（Windows 下无需管理员权限）。"""
    vdir = os.path.join(core.versions_dir, vid)
    if os.path.isfile(os.path.join(vdir, ".mc_instance")):
        return  # 整合包实例：MOD/配置已在实例目录内，不挂全局 mods
    if os.name != "nt":
        return  # 非 Windows：暂无按版本隔离需求，沿用默认 mods
    root_mods = os.path.join(core.game_root, "mods")
    inst_mods = os.path.join(core.versions_dir, vid, "mods")
    if not os.path.isdir(inst_mods):
        # 该版本没有专属 MOD：清掉可能残留的旧 junction，恢复（若有）全局 mods
        try:
            subprocess.run(["cmd", "/c", "rmdir", root_mods],
                           capture_output=True, check=False,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass
        return
    # 移除旧 junction（可能指向别的版本）
    try:
        subprocess.run(["cmd", "/c", "rmdir", root_mods],
                       capture_output=True, check=False,
                       creationflags=_NO_WINDOW)
    except Exception:
        pass
    if os.path.exists(root_mods) and not os.path.islink(root_mods):
        # 真实存在的全局 mods 目录，避免误删，保留全局（不覆盖）
        return
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", root_mods, inst_mods],
                       capture_output=True, check=False,
                       creationflags=_NO_WINDOW)
    except Exception:
        pass


_ICON_CACHE = {}   # url -> bytes，避免重复拉取图标


class IconBatchWorker(QThread):
    """并行批量拉取结果图标，逐个发出 icon_ready(idx, bytes)。

    原实现是「顺序」拉取：一页 30 张图，每张在国内线路上约 1~3 秒，
    合计要等 30~90 秒，Mod/数据包/资源包/光影四个页面都一样慢。
    这里改为线程池并行（默认 12 路）+ 磁盘缓存，30 张图约 3~8 秒，
    二次浏览则直接命中缓存、瞬间完成。
    """

    icon_ready = pyqtSignal(int, bytes)
    done = pyqtSignal()
    error = pyqtSignal(str)

    ICON_WORKERS = 16
    ICON_TIMEOUT = 15

    def __init__(self, icons):
        super().__init__()
        self.icons = icons          # [(行号, url)]

    @classmethod
    def _fetch(cls, url):
        """取单张图标：内存缓存 → 磁盘缓存 → 网络。"""
        data = _ICON_CACHE.get(url)
        if data is not None:
            return data
        cached = _mod_cache_read("icon:" + url, ".img", 7 * 24 * 3600)
        if cached is not None:
            _ICON_CACHE[url] = cached
            return cached
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=cls.ICON_TIMEOUT) as r:
            data = r.read()
        _ICON_CACHE[url] = data
        _mod_cache_write("icon:" + url, ".img", data)
        return data

    def run(self):
        try:
            items = [(idx, url) for idx, url in self.icons if url]
            if not items:
                self.done.emit()
                return
            from concurrent.futures import ThreadPoolExecutor, as_completed
            workers = max(1, min(self.ICON_WORKERS, len(items)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(self._fetch, url): idx for idx, url in items}
                for fut in as_completed(futures):
                    idx = futures[fut]
                    try:
                        data = fut.result()
                    except Exception:
                        continue          # 单张图失败不影响其它
                    self.icon_ready.emit(idx, data)
            self.done.emit()
        except Exception as e:
            self.error.emit(str(e))


class ModrinthSearchWorker(QThread):
    finished = pyqtSignal(list, int)     # hits, total_hits
    error = pyqtSignal(str)

    def __init__(self, query, game_version=None, category=None, offset=0):
        super().__init__()
        self.query = query
        self.gv = game_version
        self.cat = category
        self.offset = offset

    def run(self):
        try:
            hits, total = modrinth_search(self.query, self.gv, self.cat,
                                          offset=self.offset)
            self.finished.emit(hits, total)
        except Exception as e:
            self.error.emit(str(e))


class ModrinthDetailWorker(QThread):
    """拉取 MOD 详情（项目信息 + 全部版本）。

    原本是两个请求串行等待（国内线路上各约 2~3 秒），这里改为并行，
    详情页打开时间减半。
    """

    finished = pyqtSignal(dict, list)
    error = pyqtSignal(str)

    def __init__(self, project_id):
        super().__init__()
        self.pid = project_id

    def run(self):
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as ex:
                fp = ex.submit(modrinth_project, self.pid)
                fv = ex.submit(modrinth_versions, self.pid)
                project = fp.result()
                versions = fv.result()
            self.finished.emit(project, versions)
        except Exception as e:
            self.error.emit(str(e))


class ModFileDownloadWorker(QThread):
    """下载用户在详情页选中的具体文件。"""
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, url, filename, dest_dir):
        super().__init__()
        self.url = url
        self.fn = filename
        self.dest = dest_dir
        self._last_pct = -1

    def run(self):
        try:
            self.status.emit(f"正在下载 {self.fn} …")
            dest = os.path.join(self.dest, self.fn)
            _http_download(self.url, dest, progress_cb=self._progress)
            self.finished.emit(dest)
        except Exception as e:
            self.error.emit(str(e))

    def _progress(self, done, total):
        """下载进度回调（百分比变化时才发信号，避免刷屏）。"""
        if total > 0:
            pct = int(done * 100 / total)
            if pct != self._last_pct:
                self._last_pct = pct
                self.status.emit(f"正在下载 {self.fn} … {pct}%")
        else:
            self.status.emit(f"正在下载 {self.fn} … {done // 1024} KB")


class ModrinthDownloadWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, project_id, game_version, dest_dir):
        super().__init__()
        self.pid = project_id
        self.gv = game_version
        self.dest = dest_dir
        self._last_pct = -1

    def run(self):
        try:
            url, fn, vname = modrinth_pick_file(self.pid, self.gv)
            if not url:
                raise RuntimeError("该 MOD 没有兼容 " + self.gv + " 的文件")
            self._fname = fn
            self.status.emit(f"正在下载 {fn} …")
            dest = os.path.join(self.dest, fn)
            _http_download(url, dest, progress_cb=self._progress)
            self.finished.emit(dest)
        except Exception as e:
            self.error.emit(str(e))

    def _progress(self, done, total):
        if total > 0:
            pct = int(done * 100 / total)
            if pct != self._last_pct:
                self._last_pct = pct
                self.status.emit(f"正在下载 {getattr(self, '_fname', '')} … {pct}%")


class ModCacheWarmer(QThread):
    """后台预热 Modrinth 缓存。

    启动后悄悄把「MOD / 整合包 / 数据包 / 资源包 / 光影包」五个页面的首屏
    数据（列表 + 图标）先拉好缓存；用户点进这些页面时直接命中缓存，秒开，
    不用再等几十秒的慢速国际请求。
    """

    finished = pyqtSignal()

    TYPES = ("mod", "modpack", "datapack", "resourcepack", "shader")

    def run(self):
        try:
            _mod_cache_purge()
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=len(self.TYPES)) as ex:
                futs = {ex.submit(_modrinth_search_cached, t, "", None, None,
                                  MOD_PAGE_SIZE, 0): t for t in self.TYPES}
                icon_urls = []
                for f in as_completed(futs):
                    try:
                        hits, _total = f.result()
                    except Exception:
                        continue
                    for h in hits:
                        u = h.get("icon_url")
                        if u:
                            icon_urls.append(u)
            # 顺手预热首屏图标（与页面加载时同一套并行+缓存逻辑）
            if icon_urls:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=12) as ex:
                    futs = [ex.submit(IconBatchWorker._fetch, u) for u in icon_urls]
                    for f in as_completed(futs):
                        try:
                            f.result()
                        except Exception:
                            pass
            print("[DBG] Modrinth 缓存预热完成", flush=True)
        except Exception as e:
            print("[DBG] 缓存预热异常:", repr(e), flush=True)
        self.finished.emit()


# ==========================================================================
# 整合包（Modrinth modpack）下载与安装
# ==========================================================================
def modpack_search(query, game_version=None, limit=MOD_PAGE_SIZE, offset=0):
    """搜索 Modrinth 整合包。无关键词时按下载量排序，有关键词按相关度。"""
    return _modrinth_search_cached("modpack", query, game_version,
                                   None, limit, offset)


def _modpack_resolve_loader(deps):
    """从 modrinth.index.json 的 dependencies 解析 加载器类型/版本/目标版本id。
    返回 (loader_key, loader_version, loader_vid)。loader_key 为 None 表示仅原版。"""
    mc = deps.get("minecraft")
    for k in ("forge", "neoforge", "fabric-loader", "fabric",
              "quilt-loader", "quilt"):
        if k in deps:
            v = deps[k]
            if k == "forge":
                return ("forge", v, f"{mc}-forge-{v}")
            if k == "neoforge":
                return ("neoforge", v, f"{mc}-neoforge-{v}")
            if k in ("fabric-loader", "fabric"):
                return ("fabric", v, f"fabric-loader-{v}-{mc}")
            if k in ("quilt-loader", "quilt"):
                return ("quilt", v, f"quilt-loader-{v}-{mc}")
    return (None, None, mc)


class ModpackSearchWorker(QThread):
    finished = pyqtSignal(list, int)
    error = pyqtSignal(str)

    def __init__(self, query, game_version=None, offset=0):
        super().__init__()
        self.query = query
        self.gv = game_version
        self.offset = offset

    def run(self):
        try:
            hits, total = modpack_search(self.query, self.gv, offset=self.offset)
            self.finished.emit(hits, total)
        except Exception as e:
            self.error.emit(str(e))


# ==========================================================================
# 数据包（Modrinth datapack）下载
# ==========================================================================
def datapack_search(query, game_version=None, limit=MOD_PAGE_SIZE, offset=0):
    """搜索 Modrinth 数据包。无关键词时按下载量排序，有关键词按相关度。"""
    return _modrinth_search_cached("datapack", query, game_version,
                                   None, limit, offset)


class DatapackSearchWorker(QThread):
    finished = pyqtSignal(list, int)
    error = pyqtSignal(str)

    def __init__(self, query, game_version=None, offset=0):
        super().__init__()
        self.query = query
        self.gv = game_version
        self.offset = offset

    def run(self):
        try:
            hits, total = datapack_search(
                self.query, self.gv, offset=self.offset)
            self.finished.emit(hits, total)
        except Exception as e:
            self.error.emit(str(e))


def resourcepack_search(query, game_version=None, limit=MOD_PAGE_SIZE, offset=0):
    """搜索 Modrinth 资源包。无关键词时按下载量排序，有关键词按相关度。
    project_type:resourcepack 的 facet 是准确的（不像 datapack 会混入 MOD）。"""
    return _modrinth_search_cached("resourcepack", query, game_version,
                                   None, limit, offset)


class ResourcePackSearchWorker(QThread):
    finished = pyqtSignal(list, int)
    error = pyqtSignal(str)

    def __init__(self, query, game_version=None, offset=0):
        super().__init__()
        self.query = query
        self.gv = game_version
        self.offset = offset

    def run(self):
        try:
            hits, total = resourcepack_search(
                self.query, self.gv, offset=self.offset)
            self.finished.emit(hits, total)
        except Exception as e:
            self.error.emit(str(e))


def discover_resource_targets(core):
    """扫描可安装资源包的目标目录（全局 + 整合包实例），
    返回 [(显示名, 目录绝对路径), ...]。资源包 .zip 会放进该目录的
    resourcepacks 文件夹。"""
    targets = [("全局（.minecraft/resourcepacks）", core.game_root)]
    try:
        for v in core.list_installed():
            vid = v["id"]
            vdir = os.path.join(core.versions_dir, vid)
            if os.path.isfile(os.path.join(vdir, ".mc_instance")):
                targets.append((f"实例：{vid}", vdir))
    except Exception:
        pass
    return targets


def _pick_resourcepack_file(version):
    """从某个版本的文件列表中选出资源包文件（.zip 优先）。
    Modrinth 资源包项目通常只含一个 .zip，极少数同时含源码 zip。"""
    files = version.get("files") or []
    zips = [f for f in files if (f.get("filename") or "").lower().endswith(".zip")]
    if zips:
        prim = [f for f in zips if f.get("primary")]
        return prim[0] if prim else zips[0]
    if files:
        prim = [f for f in files if f.get("primary")]
        return prim[0] if prim else files[0]
    return None


def shader_search(query, game_version=None, limit=MOD_PAGE_SIZE, offset=0):
    """搜索 Modrinth 光影包。无关键词时按下载量排序，有关键词按相关度。
    project_type:shader 的 facet 是准确的（光影包为 .zip）。"""
    return _modrinth_search_cached("shader", query, game_version,
                                   None, limit, offset)


class ShaderSearchWorker(QThread):
    finished = pyqtSignal(list, int)
    error = pyqtSignal(str)

    def __init__(self, query, game_version=None, offset=0):
        super().__init__()
        self.query = query
        self.gv = game_version
        self.offset = offset

    def run(self):
        try:
            hits, total = shader_search(
                self.query, self.gv, offset=self.offset)
            self.finished.emit(hits, total)
        except Exception as e:
            self.error.emit(str(e))


def discover_shader_targets(core):
    """扫描可安装光影包的目标目录（全局 + 整合包实例），
    返回 [(显示名, 目录绝对路径), ...]。光影包 .zip 会放进该目录的
    shaderpacks 文件夹。"""
    targets = [("全局（.minecraft/shaderpacks）", core.game_root)]
    try:
        for v in core.list_installed():
            vid = v["id"]
            vdir = os.path.join(core.versions_dir, vid)
            if os.path.isfile(os.path.join(vdir, ".mc_instance")):
                targets.append((f"实例：{vid}", vdir))
    except Exception:
        pass
    return targets


def _pick_shader_file(version):
    """从某个版本的文件列表中选出光影包文件（.zip 优先）。
    Modrinth 光影包项目通常只含一个 .zip。"""
    files = version.get("files") or []
    zips = [f for f in files if (f.get("filename") or "").lower().endswith(".zip")]
    if zips:
        prim = [f for f in zips if f.get("primary")]
        return prim[0] if prim else zips[0]
    if files:
        prim = [f for f in files if f.get("primary")]
        return prim[0] if prim else files[0]
    return None


def discover_worlds(core):
    """扫描所有游戏目录（全局 + 整合包实例）下 saves/*/level.dat，
    返回 [(世界名, 存档目录绝对路径), ...]，用于把数据包下载到对应世界。"""
    worlds = []
    roots = [core.game_root]
    try:
        for v in core.list_installed():
            vid = v["id"]
            vdir = os.path.join(core.versions_dir, vid)
            if os.path.isfile(os.path.join(vdir, ".mc_instance")):
                roots.append(vdir)
    except Exception:
        pass
    for root in roots:
        saves = os.path.join(root, "saves")
        if not os.path.isdir(saves):
            continue
        for name in sorted(os.listdir(saves)):
            wdir = os.path.join(saves, name)
            if not os.path.isdir(wdir):
                continue
            if (os.path.isfile(os.path.join(wdir, "level.dat"))
                    or os.path.isfile(os.path.join(wdir, "level.dat_old"))):
                worlds.append((name, wdir))
    return worlds


def _pick_datapack_file(version):
    """从某个版本的文件列表中选出数据包文件（.zip 优先，其次仅服务端文件），
    返回文件字典或 None。Modrinth 上很多“数据包”其实是带 datapack 分类的 MOD，
    其版本同时含 .jar（MOD）与 .zip（数据包），这里只取 .zip。"""
    files = version.get("files") or []
    zips = [f for f in files if (f.get("filename") or "").lower().endswith(".zip")]
    if zips:
        prim = [f for f in zips if f.get("primary")]
        return prim[0] if prim else zips[0]
    # 没有 zip：退回“仅服务端”（env.client=unsupported）的文件，或第一个
    server_only = [f for f in files
                   if (f.get("env") or {}).get("client") == "unsupported"]
    if server_only:
        return server_only[0]
    if files:
        prim = [f for f in files if f.get("primary")]
        return prim[0] if prim else files[0]
    return None


class ModpackInstallWorker(QThread):
    """下载整合包（.mrpack）并安装为可启动版本：
       - 解析 manifest，按依赖自动安装所需加载器（forge/neoforge/fabric）；
       - 生成 inheritsFrom 加载器版本的版本 JSON + .mc_instance 实例标记，
         配置/资源包/mods 全部隔离在该版本目录内；
       - 下载 manifest 中列出的所有文件与 overrides。"""
    finished = pyqtSignal(str)          # 安装完成后的版本 id
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, core, title, slug, version_number, file_url, file_name):
        super().__init__()
        self.core = core
        self.title = title
        self.slug = slug
        self.version_number = version_number
        self.file_url = file_url
        self.file_name = file_name

    def _safe(self, name):
        import re as _re
        return _re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".")

    def run(self):
        import datetime as _dt
        import tempfile
        try:
            self.status.emit(f"正在下载整合包文件 {self.file_name} …")
            tmp = os.path.join(tempfile.gettempdir(),
                              f"plainmc-modpack-{self._safe(self.slug)}.mrpack")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            _http_download(self.file_url, tmp, headers={"User-Agent": USER_AGENT})

            # 解析 manifest
            self.status.emit("正在解析整合包清单…")
            with zipfile.ZipFile(tmp) as z:
                idx_name = next((n for n in z.namelist()
                                 if n.endswith("modrinth.index.json")), None)
                if not idx_name:
                    raise RuntimeError("整合包内未找到 modrinth.index.json，"
                                       "可能不是有效的 Modrinth 整合包。")
                manifest = json.loads(z.read(idx_name).decode())
                # 先缓存 overrides 数据，稍后写入
                overrides = {n: z.read(n)
                             for n in z.namelist()
                             if (n.startswith("overrides/")
                                 or n.startswith("client-overrides/"))
                             and not n.endswith("/")}

            deps = manifest.get("dependencies", {})
            mc = deps.get("minecraft")
            if not mc:
                raise RuntimeError("整合包清单缺少 minecraft 版本依赖。")
            loader_key, loader_ver, loader_vid = _modpack_resolve_loader(deps)

            installed = {v["id"] for v in self.core.list_installed()}

            # ---- 确保基础版本 / 加载器已安装 ----
            if loader_key in ("forge", "neoforge", "fabric"):
                if loader_vid not in installed:
                    self.status.emit(f"正在安装所需加载器：{loader_vid} …")
                    major = required_java_for_version(mc)
                    flag = {}
                    if loader_key == "forge":
                        w = InstallerInstallWorker(
                            self.core, mc, loader_vid,
                            forge_installer_url(mc, loader_ver),
                            major, self.core.game_root, "forge")
                    elif loader_key == "neoforge":
                        w = InstallerInstallWorker(
                            self.core, mc, loader_vid,
                            neoforge_installer_url(mc, loader_ver),
                            major, self.core.game_root, "neoforge")
                    else:
                        w = FabricInstallWorker(
                            self.core, mc, loader_ver, self.core.game_root)
                    w.error.connect(lambda e: flag.update(failed=e))
                    w.finished.connect(lambda r: flag.update(done=r))
                    w.run()   # 在后台线程内同步执行（w 自身已 try/except）
                    if "failed" in flag:
                        raise RuntimeError("加载器安装失败：" + flag["failed"])
                if "done" in flag:
                    loader_vid = flag["done"].get("id", loader_vid)
            elif loader_key == "quilt":
                # 本启动器暂未内置 Quilt 安装，先装好原版，提示用户手动装 Quilt
                if mc not in installed:
                    self.core.download_version(mc)
                loader_vid = mc
                self.status.emit("⚠ 该整合包依赖 Quilt 加载器，请稍后到『下载』页手动安装"
                                 f" Quilt（游戏版本 {mc}），否则 MOD 不会生效。")
            else:
                # 仅原版
                if mc not in installed:
                    self.core.download_version(mc)

            # ---- 生成整合包版本目录 ----
            mp_vid = self._safe(f"modpack-{self.slug}-{self.version_number}")
            vdir = os.path.join(self.core.versions_dir, mp_vid)
            os.makedirs(vdir, exist_ok=True)
            vj = {
                "id": mp_vid,
                "inheritsFrom": loader_vid,
                "type": "release",
                "releaseTime": _dt.datetime.now().isoformat(),
                "time": _dt.datetime.now().isoformat(),
            }
            with open(os.path.join(vdir, f"{mp_vid}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(vj, f, ensure_ascii=False, indent=2)
            # 实例标记：让启动器以该目录作为 .minecraft 目录（隔离 mods/配置）
            with open(os.path.join(vdir, ".mc_instance"), "w",
                      encoding="utf-8") as f:
                f.write("")

            # ---- 下载 manifest 列出的所有文件 ----
            files = manifest.get("files", []) or []
            nfiles = len(files)
            for i, fdict in enumerate(files):
                path = (fdict.get("path") or "").replace("\\", "/")
                if not path or ".." in path.split("/"):
                    continue
                env = fdict.get("env") or {}
                client = env.get("client", "required")
                if client in ("unsupported", "unknown"):
                    continue
                urls = fdict.get("downloads") or []
                if not urls and fdict.get("url"):
                    urls = [fdict["url"]]
                if not urls:
                    continue
                dest = os.path.join(vdir, path)
                self.status.emit(f"正在下载文件 ({i + 1}/{nfiles})："
                                 f"{os.path.basename(path)} …")
                last = None
                for u in urls:
                    try:
                        _http_download(u, dest, headers={"User-Agent": USER_AGENT})
                        last = None
                        break
                    except Exception as e:
                        last = e
                if last:
                    raise RuntimeError(f"文件 {path} 下载失败：" + str(last))

            # ---- 解包 overrides（配置/资源包等）----
            for name, data in overrides.items():
                rel = name.split("/", 1)[1] if "/" in name else name
                if ".." in rel.replace("\\", "/").split("/"):
                    continue
                dest = os.path.join(vdir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(data)

            self.status.emit(f"整合包安装完成：{mp_vid}")
            self.finished.emit(mp_vid)
        except Exception as e:
            self.error.emit(str(e))


def _resolve_mod_target(main):
    """解析下载目标版本：优先搜索筛选版本（若已安装），否则自动检测版本。
    返回 (target_vid 或 None, installed_ids)。"""
    installed = {v["id"] for v in main.core.list_installed()}
    if not installed:
        return None, installed
    page = main.mod_page
    t = page._filter_version()
    if t and t in installed:
        return t, installed
    return page.vid, installed


class ModRow(QFrame):
    """PCL2 风格的 MOD 结果卡片：图标 + 名称/标签 + 简介 + 版本/下载量/时间/来源。
    点击整行进入详情页选择版本下载。"""
    open_detail = pyqtSignal(str)           # pid

    def __init__(self, hit, target_version):
        super().__init__()
        self.setObjectName("ModRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pid = hit.get("project_id", "")
        self.target_version = target_version

        hl = QHBoxLayout(self)
        hl.setContentsMargins(12, 10, 12, 10)
        hl.setSpacing(12)

        self.icon_lbl = QLabel("📦")
        self.icon_lbl.setFixedSize(44, 44)
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_lbl.setStyleSheet(
            "background:rgba(128,128,128,0.15);border-radius:6px;font-size:22px;")
        hl.addWidget(self.icon_lbl)

        info = QVBoxLayout()
        info.setSpacing(3)
        title = QLabel(hit.get("title") or hit.get("slug") or "未命名")
        title.setStyleSheet("font-size:14px;font-weight:bold;")
        info.addWidget(title)

        cats = [MOD_CATEGORY_CN.get(c, c)
                for c in (hit.get("display_categories") or [])][:3]
        tags = QLabel(" | ".join(cats))
        tags.setObjectName("HintText")
        tags.setVisible(bool(cats))
        info.addWidget(tags)

        desc_text = (hit.get("description") or "").strip()
        if len(desc_text) > 90:
            desc_text = desc_text[:90] + "…"
        desc = QLabel(desc_text)
        desc.setObjectName("HintText")
        info.addWidget(desc)

        vers = hit.get("versions") or []
        ver_str = ", ".join(vers[:2]) + (" 等" if len(vers) > 2 else "")
        meta_text = f"◎ {ver_str or '未知'}    ⬇ {fmt_downloads_cn(hit.get('downloads', 0))}"
        rel = fmt_rel_time(hit.get("date_modified", ""))
        if rel:
            meta_text += f"    ↑ {rel}"
        meta = QLabel(meta_text)
        meta.setObjectName("HintText")
        info.addWidget(meta)

        hl.addLayout(info, 1)

    def set_icon_bytes(self, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            self.icon_lbl.setPixmap(pm.scaled(
                44, 44, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def mousePressEvent(self, e):
        self.open_detail.emit(self.pid)
        super().mousePressEvent(e)


class ModVersionGroupRow(QFrame):
    """详情页里「加载器 + 游戏版本」分组行，展开显示可下载文件。"""

    def __init__(self, label, files, on_download):
        super().__init__()
        self.setObjectName("ModRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.head = QPushButton("▸  " + label)
        self.head.setStyleSheet(
            "text-align:left;padding:10px 14px;border:none;background:transparent;"
            "font-size:14px;font-weight:bold;")
        self.head.setCursor(Qt.CursorShape.PointingHandCursor)
        v.addWidget(self.head)

        self.body = QWidget()
        bv = QVBoxLayout(self.body)
        bv.setContentsMargins(26, 0, 14, 10)
        bv.setSpacing(6)
        for fdict in files:
            fn = fdict.get("filename") or "mod.jar"
            fr = QHBoxLayout()
            name = QLabel(fn)
            name.setStyleSheet("font-size:13px;")
            fr.addWidget(name, 1)
            btn = make_ghost("下载")
            btn.setMaximumWidth(76)
            btn.clicked.connect(lambda _, fd=fdict: on_download(fd))
            fr.addWidget(btn)
            bv.addLayout(fr)
        self.body.setVisible(False)
        v.addWidget(self.body)

        self._open = False
        self._label = label
        self.head.clicked.connect(self._toggle)

    def _toggle(self):
        self._open = not self._open
        self.body.setVisible(self._open)
        self.head.setText(("▾  " if self._open else "▸  ") + self._label)


class ModDetailPage(QWidget):
    """MOD 详情页：信息卡 + MC 版本筛选 + 「加载器+游戏版本」分组展开下载。"""
    back_clicked = pyqtSignal()

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.pid = None
        self._project = {}
        self._versions = []
        self._dw = None
        self._root_layout = None
        self._build_ui()

    # -------------------------------------------------------------- UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        self._root_layout = root

        back = make_ghost("‹ 返回")
        back.setMaximumWidth(100)
        back.clicked.connect(self.back_clicked.emit)
        root.addWidget(back)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

        self.status = QLabel()
        self.status.setObjectName("HintText")
        root.addWidget(self.status)

    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    # -------------------------------------------------------------- 加载
    def load(self, project_id):
        self.pid = project_id
        self._project = {}
        self._versions = []
        self._clear_content()
        self.content_layout.addWidget(QLabel("正在加载 MOD 信息…"))
        w = ModrinthDetailWorker(project_id)
        self._dw = w
        _track_worker(w)
        w.finished.connect(self._on_loaded)
        w.error.connect(self._on_error)
        w.start()

    def _on_error(self, e):
        self._clear_content()
        self.content_layout.addWidget(QLabel("加载失败：" + e))

    def _on_loaded(self, project, versions):
        self._project = project or {}
        self._versions = versions or []
        self._render()

    def _render(self, gv_filter=None):
        p = self._project
        self._clear_content()

        # ---- 信息卡 ----
        card = make_card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon_lbl = QLabel("📦")
        icon_lbl.setFixedSize(52, 52)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            "background:rgba(128,128,128,0.15);border-radius:6px;font-size:26px;")
        top.addWidget(icon_lbl)
        self._detail_icon_lbl = icon_lbl
        if p.get("icon_url"):
            url = p["icon_url"]
            data = _ICON_CACHE.get(url)
            if data is not None:
                pm = QPixmap()
                if pm.loadFromData(data):
                    icon_lbl.setPixmap(pm.scaled(
                        52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
            else:
                w = IconBatchWorker([(0, url)])
                self._dw_icon = w
                _track_worker(w)
                w.icon_ready.connect(self._on_detail_icon)

        info = QVBoxLayout()
        info.setSpacing(3)
        title_row = QHBoxLayout()
        t1 = QLabel(p.get("title") or "")
        t1.setStyleSheet("font-size:17px;font-weight:bold;")
        title_row.addWidget(t1)
        if p.get("slug"):
            t2 = QLabel(p.get("slug"))
            t2.setObjectName("HintText")
            title_row.addWidget(t2)
            title_row.addStretch(1)
        info.addLayout(title_row)

        cats = [MOD_CATEGORY_CN.get(c, c)
                for c in (p.get("display_categories") or [])][:4]
        if cats:
            tg = QLabel(" | ".join(cats))
            tg.setObjectName("HintText")
            info.addWidget(tg)
        if p.get("description"):
            d = QLabel(p["description"])
            d.setObjectName("HintText")
            d.setWordWrap(True)
            info.addWidget(d)

        loaders = sorted({l for v in self._versions
                          for l in (v.get("loaders") or [])})
        loader_str = " / ".join(MOD_LOADER_CN.get(l, l) for l in loaders) or "通用"
        meta = QLabel(f"◎ {loader_str}    ⬇ {fmt_downloads_cn(p.get('downloads', 0))}"
                      + (f"    ↑ {fmt_rel_time(p.get('date_modified', ''))}"
                         if p.get("date_modified") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        top.addLayout(info, 1)
        cl.addLayout(top)

        btns = QHBoxLayout()
        b1 = make_ghost("转到 Modrinth")
        web_url = p.get("web_url") or f"https://modrinth.com/mod/{p.get('slug', '')}"
        b1.clicked.connect(lambda: webbrowser.open(web_url))
        b2 = make_ghost("复制名称")
        b2.clicked.connect(lambda: QApplication.clipboard().setText(
            p.get("title") or ""))
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch(1)
        cl.addLayout(btns)
        self.content_layout.addWidget(card)

        # ---- MC 版本筛选条 ----
        all_gvs = sorted({gv for v in self._versions
                          for gv in (v.get("game_versions") or [])},
                         key=_ver_sort_key, reverse=True)
        chip_card = make_card()
        chips = QHBoxLayout(chip_card)
        chips.setContentsMargins(12, 8, 12, 8)
        chips.setSpacing(6)
        chips.addStretch(0)
        # 全部 + 前 12 个游戏版本
        chip_defs = [("全部", None)] + [(g, g) for g in all_gvs[:12]]
        for label, gv in chip_defs:
            b = make_ghost(label)
            b.setCheckable(True)
            b.setChecked(gv_filter == gv if gv else gv_filter is None)
            b.setMaximumWidth(86)
            b.clicked.connect(lambda _, g=gv: self._render(g))
            chips.addWidget(b)
        chips.addStretch(1)
        self.content_layout.addWidget(chip_card)

        # ---- 分组行：加载器 + 游戏版本 ----
        groups = {}
        for v in self._versions:
            loaders = [MOD_LOADER_CN.get(l, l.capitalize())
                       for l in (v.get("loaders") or [""])] or ["通用"]
            gvs = v.get("game_versions") or []
            if not gvs:
                gvs = ["未知版本"]
            for gv in gvs:
                if gv_filter and gv != gv_filter:
                    continue
                for ld in loaders:
                    key = (ld or "通用", gv)
                    files = [f for f in (v.get("files") or [])
                             if f.get("url")]
                    if files:
                        groups.setdefault(key, []).extend(files)
        if not groups:
            hint = QLabel("（没有匹配的版本文件）")
            hint.setObjectName("HintText")
            self.content_layout.addWidget(hint)
        # 按游戏版本新→旧、加载器排序
        for (ld, gv), files in sorted(
                groups.items(),
                key=lambda kv: (_ver_sort_key(kv[0][1]), kv[0][0]),
                reverse=True)[:200]:
            label = f"{ld} {gv}"
            row = ModVersionGroupRow(label, files, self._download_file)
            self.content_layout.addWidget(row)

        self.content_layout.addStretch(1)

    def _on_detail_icon(self, idx, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            self._detail_icon_lbl.setPixmap(pm.scaled(
                52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    # -------------------------------------------------------------- 下载
    def _download_file(self, fdict):
        url = fdict.get("url")
        filename = fdict.get("filename") or "mod.jar"
        target, installed = _resolve_mod_target(self.main)
        if not target:
            QMessageBox.information(
                self, "提示", "尚未下载任何游戏版本，请先到『下载』页下载一个版本。")
            self.main._switch(1)
            return
        d = os.path.join(self.main.core.versions_dir, target, "mods")
        os.makedirs(d, exist_ok=True)
        w = ModFileDownloadWorker(url, filename, d)
        self._dw = w
        _track_worker(w)
        w.status.connect(lambda m: self.status.setText(m))
        w.finished.connect(lambda p: (self.status.setText(
            f"已下载到 {target}：{os.path.basename(p)}"),
            self.main.mod_page._refresh_local_list()))
        w.error.connect(lambda e: self.status.setText("下载失败：" + e))
        w.start()


class ModPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.vid = None                # 自动检测到的管理版本
        self._rows = []
        self._sw = None
        self._dw = None
        self._iw = None
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._ever_loaded = False
        self._build_ui()

    # -------------------------------------------------------------- UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QLabel("搜索 Mod")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        # 版本检测状态行
        status_row = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("HintText")
        status_row.addWidget(self.status_label, 1)
        self.go_download_btn = make_ghost("前往下载页下载版本")
        self.go_download_btn.clicked.connect(self._go_download)
        status_row.addWidget(self.go_download_btn)
        root.addLayout(status_row)

        # 内部栈：浏览 / 详情
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        # ---------- 浏览页 ----------
        browse = QWidget()
        bv = QVBoxLayout(browse)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(12)

        # 搜索条件卡
        card = make_card()
        grid = QGridLayout(card)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("名称"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("SearchEdit")
        self.name_edit.setPlaceholderText("留空则浏览全部 MOD；或输入关键词搜索")
        self.name_edit.returnPressed.connect(self._do_search)
        grid.addWidget(self.name_edit, 0, 1, 1, 3)


        grid.addWidget(QLabel("版本"), 1, 0)
        self.version_combo = QComboBox()
        self.version_combo.setMinimumHeight(36)
        self.version_combo.setEditable(True)
        grid.addWidget(self.version_combo, 1, 1, 1, 3)

        grid.addWidget(QLabel("类型"), 1, 4)
        self.type_combo = QComboBox()
        self.type_combo.setMinimumHeight(36)
        self.type_combo.addItem("全部", None)
        for en, cn in MOD_CATEGORIES:
            self.type_combo.addItem(cn, en)
        grid.addWidget(self.type_combo, 1, 5)

        btn_row = QHBoxLayout()
        self.search_btn = make_primary("搜索")
        self.search_btn.setMaximumWidth(140)
        self.search_btn.clicked.connect(self._do_search)
        btn_row.addWidget(self.search_btn)
        self.reset_btn = make_ghost("重置条件")
        self.reset_btn.setMaximumWidth(140)
        self.reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(self.reset_btn)
        btn_row.addStretch(1)
        grid.addLayout(btn_row, 2, 0, 1, 6)

        bv.addWidget(card)

        # 搜索状态
        self.search_status = QLabel()
        self.search_status.setObjectName("HintText")
        bv.addWidget(self.search_status)

        # 结果区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.results_layout = QVBoxLayout(scroll_content)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(8)
        self._next_btn = make_ghost("下一页  ↓")
        self._next_btn.clicked.connect(self._load_more)
        self.results_layout.addWidget(self._next_btn)
        self.results_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        bv.addWidget(scroll, 1)

        # 本地 MOD 卡（已安装的 MOD）
        local_card = make_card()
        lc = QVBoxLayout(local_card)
        lc.setContentsMargins(14, 12, 14, 12)
        lc.setSpacing(8)
        local_head = QHBoxLayout()
        self.local_title = QLabel("已安装的 MOD")
        self.local_title.setObjectName("HintText")
        local_head.addWidget(self.local_title, 1)
        self.open_btn = make_ghost("打开 MOD 文件夹")
        self.open_btn.clicked.connect(self._open_mods)
        self.add_btn = make_ghost("＋ 添加 MOD 文件…")
        self.add_btn.clicked.connect(self._add_mods)
        local_head.addWidget(self.open_btn)
        local_head.addWidget(self.add_btn)
        lc.addLayout(local_head)
        self.local_list = QListWidget()
        self.local_list.setMinimumHeight(80)
        self.local_list.setMaximumHeight(130)
        lc.addWidget(self.local_list)
        bv.addWidget(local_card)

        self.pages.addWidget(browse)          # 0 浏览
        self.detail_page = ModDetailPage(self.main)
        self.detail_page.back_clicked.connect(self._back_to_browse)
        self.pages.addWidget(self.detail_page)  # 1 详情

        self.refresh()

    # -------------------------------------------------------------- 检测
    def _auto_detect(self):
        """自动检测要管理的版本：优先取启动页当前所选（若已安装），
        否则取最新已安装的版本。"""
        installed = self.main.core.list_installed()
        ids = [v["id"] for v in installed]
        if not ids:
            return None
        launch_sel = self.main.launch_page.version_combo.currentData()
        if launch_sel and launch_sel in ids:
            return launch_sel
        return ids[0]

    def refresh(self):
        installed = self.main.core.list_installed()
        ids = [v["id"] for v in installed]
        self.vid = self._auto_detect()

        # 版本下拉：全部 + 已安装版本（可自行输入任意版本号）
        self.version_combo.blockSignals(True)
        cur = self.version_combo.currentText()
        self.version_combo.clear()
        self.version_combo.addItem("全部")
        for v in ids:
            self.version_combo.addItem(v)
        if cur and (cur == "全部" or cur in ids):
            self.version_combo.setCurrentText(cur)
        elif self.vid:
            self.version_combo.setCurrentText(self.vid)
        self.version_combo.blockSignals(False)

        # 状态行
        if self.vid:
            d = os.path.join(self.main.core.versions_dir, self.vid, "mods")
            self.status_label.setText(
                f"自动检测到版本：{self.vid} · MOD 文件夹：{d}")
            self.go_download_btn.hide()
        else:
            self.status_label.setText("尚未下载任何游戏版本，无法管理 MOD。")
            self.go_download_btn.show()

        self.local_title.setText(
            f"已安装的 MOD（{self.vid or '无版本'}）")
        self._refresh_local_list()

    def _reset(self):
        self.name_edit.clear()
        self.type_combo.setCurrentIndex(0)
        self.refresh()
        self._do_search()
        self.search_status.setText("已重置搜索条件。")

    # -------------------------------------------------------------- 搜索/分页
    def _filter_version(self):
        t = self.version_combo.currentText().strip()
        if not t or t == "全部":
            return None
        return t

    def _do_search(self):
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._fetch_page(append=False)

    def _load_more(self):
        self._fetch_page(append=True)

    def _fetch_page(self, append):
        q = self.name_edit.text().strip()
        gv = self._filter_version()
        cat = self.type_combo.currentData()
        if not append:
            self._clear_results()
        self.search_status.setText("正在搜索…")
        self._next_btn.setEnabled(False)
        w = ModrinthSearchWorker(q, gv, cat, offset=self._offset)
        self._sw = w
        _track_worker(w)
        w.finished.connect(lambda hits, total: self._on_page(hits, total, append))
        w.error.connect(lambda e: self.search_status.setText("搜索失败：" + e))
        w.start()

    def _clear_results(self):
        self._rows = []
        while self.results_layout.count() > 2:   # next_btn + stretch 保留
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _on_page(self, hits, total, append):
        self._next_btn.setEnabled(True)
        self._total = total
        if not hits:
            if not append:
                self.search_status.setText("没有找到相关 MOD。")
                self._next_btn.hide()
            else:
                self.search_status.setText(
                    f"没有更多了（共 {total} 个）。")
                self._next_btn.hide()
            return

        gv = self._filter_version() or self.vid or ""
        target, _installed = _resolve_mod_target(self.main)
        self._offset += len(hits)
        self._loaded += len(hits)

        base = "已显示" if append else "找到"
        tip = f"{base} {self._loaded} / 共 {total} 个 MOD"
        if target:
            tip += f"，点开 MOD 选择版本下载（保存到「{target}」）"
        if not append and gv and gv not in {v["id"] for v in self.main.core.list_installed()}:
            tip += f"（版本 {gv} 未下载，下载时将回退到 {target or '自动检测版本'}）"
        self.search_status.setText(tip + "。")

        icons = []
        for i, h in enumerate(hits):
            row = ModRow(h, target or "")
            row.open_detail.connect(self._open_detail)
            self.results_layout.insertWidget(
                self.results_layout.count() - 2, row)
            self._rows.append(row)
            if h.get("icon_url"):
                icons.append((len(self._rows) - 1, h["icon_url"]))
        # 批量拉取图标
        if icons:
            w = IconBatchWorker(icons)
            self._iw = w
            _track_worker(w)
            w.icon_ready.connect(self._on_icon)
            w.start()
        # 是否还有下一页
        self._next_btn.setVisible(self._loaded < total)

    def _on_icon(self, idx, data):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_icon_bytes(data)

    # -------------------------------------------------------------- 详情
    def _open_detail(self, pid):
        self.pages.setCurrentWidget(self.detail_page)
        self.detail_page.load(pid)

    def _back_to_browse(self):
        self.pages.setCurrentWidget(self.pages.widget(0))

    def _go_download(self):
        self.main._switch(1)
        if self.vid:
            self.main.download_page._show_detail(self.vid)

    # -------------------------------------------------------------- 本地
    def _mods_dir(self):
        return os.path.join(self.main.core.versions_dir, self.vid, "mods")

    def _refresh_local_list(self):
        self.local_list.clear()
        if not self.vid:
            self.local_list.addItem("（未检测到已下载版本）")
            return
        d = self._mods_dir()
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        found = False
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith((".jar", ".disabled")):
                continue
            found = True
            item = QListWidgetItem()
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(6, 4, 6, 4)
            name = QLabel(fn)
            name.setStyleSheet("font-size:13px;")
            hl.addWidget(name, 1)
            rm = make_ghost("移除")
            rm.setMaximumWidth(70)
            rm.clicked.connect(lambda _, p=os.path.join(d, fn): self._remove_mod(p))
            hl.addWidget(rm)
            item.setSizeHint(row.sizeHint())
            self.local_list.addItem(item)
            self.local_list.setItemWidget(item, row)
        if not found:
            self.local_list.addItem("（该版本暂无 MOD，可添加文件或在上方点开 MOD 下载）")

    def _remove_mod(self, path):
        try:
            os.remove(path)
            self._refresh_local_list()
        except Exception as e:
            QMessageBox.warning(self, "移除失败", str(e))

    def _open_mods(self):
        if not self.vid:
            return
        d = self._mods_dir()
        os.makedirs(d, exist_ok=True)
        if os.name == "nt":
            os.startfile(d)
        else:
            subprocess.run(["xdg-open", d], creationflags=_NO_WINDOW)

    def _add_mods(self):
        if not self.vid:
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择 MOD 文件", "", "MOD 文件 (*.jar)")
        if not files:
            return
        d = self._mods_dir()
        os.makedirs(d, exist_ok=True)
        for src in files:
            try:
                shutil.copy2(src, os.path.join(d, os.path.basename(src)))
            except Exception as e:
                QMessageBox.warning(self, "添加失败", f"{src}\n{str(e)}")
        self._refresh_local_list()

    # -------------------------------------------------------------- 刷新
    def showEvent(self, e):
        self.refresh()
        # 未搜索时默认展示全部 MOD（按下载量），仅首次进入时自动加载
        if not self._ever_loaded:
            self._ever_loaded = True
            self._do_search()
        super().showEvent(e)

# ==========================================================================
# 整合包浏览 / 详情 / 安装 页面
# ==========================================================================
class ModpackDetailPage(QWidget):
    """整合包详情页：信息卡 + 版本列表，每个版本一个『下载整合包』按钮。"""
    back_clicked = pyqtSignal()

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.pid = None
        self._project = {}
        self._versions = []
        self._iw = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        back = make_ghost("‹ 返回")
        back.setMaximumWidth(100)
        back.clicked.connect(self.back_clicked.emit)
        root.addWidget(back)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

        self.status = QLabel()
        self.status.setObjectName("HintText")
        root.addWidget(self.status)

    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def load(self, pid):
        self.pid = pid
        self._project = {}
        self._versions = []
        self._clear_content()
        self.content_layout.addWidget(QLabel("正在加载整合包信息…"))
        w = ModrinthDetailWorker(pid)     # 与 MOD 完全相同的接口
        self._iw = w
        _track_worker(w)
        w.finished.connect(self._on_loaded)
        w.error.connect(lambda e: (self._clear_content(),
                                   self.content_layout.addWidget(
                                       QLabel("加载失败：" + e))))
        w.start()

    def _on_loaded(self, project, versions):
        self._project = project or {}
        self._versions = versions or []
        self._render()

    def _render(self):
        p = self._project
        self._clear_content()

        card = make_card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon_lbl = QLabel("📦")
        icon_lbl.setFixedSize(52, 52)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            "background:rgba(128,128,128,0.15);border-radius:6px;font-size:26px;")
        top.addWidget(icon_lbl)
        self._detail_icon_lbl = icon_lbl
        if p.get("icon_url"):
            url = p["icon_url"]
            data = _ICON_CACHE.get(url)
            if data is not None:
                pm = QPixmap()
                if pm.loadFromData(data):
                    icon_lbl.setPixmap(pm.scaled(
                        52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
            else:
                w = IconBatchWorker([(0, url)])
                self._iw_icon = w
                _track_worker(w)
                w.icon_ready.connect(self._on_detail_icon)
                w.start()

        info = QVBoxLayout()
        info.setSpacing(3)
        t1 = QLabel(p.get("title") or "")
        t1.setStyleSheet("font-size:17px;font-weight:bold;")
        info.addWidget(t1)
        if p.get("slug"):
            t2 = QLabel(p.get("slug"))
            t2.setObjectName("HintText")
            info.addWidget(t2)
        if p.get("description"):
            d = QLabel(p["description"])
            d.setObjectName("HintText")
            d.setWordWrap(True)
            info.addWidget(d)
        meta = QLabel(f"⬇ {fmt_downloads_cn(p.get('downloads', 0))}"
                      + (f"    ↑ {fmt_rel_time(p.get('date_modified', ''))}"
                         if p.get("date_modified") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        top.addLayout(info, 1)
        cl.addLayout(top)

        btns = QHBoxLayout()
        b1 = make_ghost("转到 Modrinth")
        web_url = p.get("web_url") or f"https://modrinth.com/modpack/{p.get('slug', '')}"
        b1.clicked.connect(lambda: webbrowser.open(web_url))
        b2 = make_ghost("复制名称")
        b2.clicked.connect(lambda: QApplication.clipboard().setText(
            p.get("title") or ""))
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch(1)
        cl.addLayout(btns)
        self.content_layout.addWidget(card)

        # 版本列表
        hint = QLabel("选择一个版本下载（将自动安装所需加载器并隔离为独立版本）：")
        hint.setObjectName("HintText")
        self.content_layout.addWidget(hint)

        # 按版本号新→旧排序
        ordered = sorted(
            self._versions,
            key=lambda v: _ver_sort_key(v.get("version_number")
                                        or (v.get("game_versions") or [""])[0]),
            reverse=True)
        for v in ordered:
            self.content_layout.addWidget(self._version_row(v))

        self.content_layout.addStretch(1)

    def _version_row(self, v):
        row = make_card()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(14, 12, 14, 12)
        rl.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(3)
        name = QLabel(v.get("name") or v.get("version_number") or "版本")
        name.setStyleSheet("font-size:14px;font-weight:bold;")
        info.addWidget(name)
        gvs = ", ".join(v.get("game_versions") or [])
        meta = QLabel(f"◎ {gvs or '未知'}"
                      + (f"    ⬇ {fmt_downloads_cn(v.get('downloads', 0))}"
                         if v.get("downloads") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        rl.addLayout(info, 1)

        btn = make_primary("下载整合包")
        btn.setMaximumWidth(150)
        btn.clicked.connect(lambda _, ver=v: self._download(ver))
        rl.addWidget(btn)
        return row

    def _on_detail_icon(self, idx, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            self._detail_icon_lbl.setPixmap(pm.scaled(
                52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _download(self, v):
        files = v.get("files") or []
        f = next((x for x in files if x.get("primary")), None) or (
            files[0] if files else None)
        if not f or not f.get("url"):
            QMessageBox.information(self, "提示", "该版本没有可下载的整合包文件。")
            return
        url = f["url"]
        fn = f.get("filename") or (self._project.get("slug", "modpack") + ".mrpack")
        p = self._project
        worker = ModpackInstallWorker(
            self.main.core, p.get("title", ""), p.get("slug", "modpack"),
            v.get("version_number") or v.get("name") or "1", url, fn)
        self._iw = worker
        _track_worker(worker)
        worker.status.connect(self.status.setText)
        worker.error.connect(lambda e: self.status.setText("安装失败：" + e))
        worker.finished.connect(self._on_installed)
        worker.start()

    def _on_installed(self, vid):
        self.status.setText(f"✅ 整合包已安装为版本：{vid}，可到『启动』页选择游玩。")
        if hasattr(self.main, "launch_page"):
            try:
                self.main.launch_page.refresh_installed()
            except Exception:
                pass
        QMessageBox.information(
            self, "安装完成",
            f"整合包已安装为独立版本「{vid}」。\n请到『启动』页选择该版本开始游玩。\n"
            f"（其 MOD、配置、资源包均隔离在该版本目录内，不影响其他版本。）")


class ModpackPage(QWidget):
    """整合包浏览页：搜索 / 按下载量浏览 / 点开详情选择版本下载。"""
    RECENT_MC = ["1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.19.4",
                 "1.19.2", "1.18.2", "1.16.5", "1.12.2"]

    def __init__(self, main):
        super().__init__()
        self.main = main
        self._rows = []
        self._sw = None
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._ever_loaded = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QLabel("搜索整合包")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        # 内部栈：浏览 / 详情
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        browse = QWidget()
        bv = QVBoxLayout(browse)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(12)

        card = make_card()
        grid = QGridLayout(card)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("名称"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("SearchEdit")
        self.name_edit.setPlaceholderText("留空则浏览全部整合包；或输入关键词搜索")
        self.name_edit.returnPressed.connect(self._do_search)
        grid.addWidget(self.name_edit, 0, 1, 1, 3)

        grid.addWidget(QLabel("版本"), 1, 0)
        self.version_combo = QComboBox()
        self.version_combo.setMinimumHeight(36)
        self.version_combo.setEditable(True)
        self.version_combo.setPlaceholderText("全部版本")
        grid.addWidget(self.version_combo, 1, 1, 1, 3)

        btn_row = QHBoxLayout()
        self.search_btn = make_primary("搜索")
        self.search_btn.setMaximumWidth(140)
        self.search_btn.clicked.connect(self._do_search)
        btn_row.addWidget(self.search_btn)
        self.reset_btn = make_ghost("重置条件")
        self.reset_btn.setMaximumWidth(140)
        self.reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(self.reset_btn)
        btn_row.addStretch(1)
        grid.addLayout(btn_row, 2, 0, 1, 4)

        bv.addWidget(card)

        self.search_status = QLabel()
        self.search_status.setObjectName("HintText")
        bv.addWidget(self.search_status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.results_layout = QVBoxLayout(scroll_content)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(8)
        self._next_btn = make_ghost("下一页  ↓")
        self._next_btn.clicked.connect(self._load_more)
        self.results_layout.addWidget(self._next_btn)
        self.results_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        bv.addWidget(scroll, 1)

        # 使用说明
        note = make_card()
        nl = QVBoxLayout(note)
        nl.setContentsMargins(14, 12, 14, 12)
        nl.setSpacing(6)
        nl.addWidget(QLabel("使用说明"))
        tip = QLabel("① 搜索或浏览整合包 → ② 点开某个整合包 → ③ 选择版本点『下载整合包』。"
                     "启动器会自动安装所需的 Forge / NeoForge / Fabric 加载器，"
                     "并把 MOD、配置、资源包隔离在该版本专属目录内，不影响其他版本。\n"
                     "（Quilt 加载器暂需手动到『下载』页安装；仅原版整合包直接安装。）")
        tip.setObjectName("HintText")
        tip.setWordWrap(True)
        nl.addWidget(tip)
        bv.addWidget(note)

        self.pages.addWidget(browse)          # 0 浏览
        self.detail_page = ModpackDetailPage(self.main)
        self.detail_page.back_clicked.connect(self._back_to_browse)
        self.pages.addWidget(self.detail_page)  # 1 详情

        self.refresh()

    def refresh(self):
        self.version_combo.blockSignals(True)
        cur = self.version_combo.currentText()
        self.version_combo.clear()
        self.version_combo.addItem("全部")
        for v in self.RECENT_MC:
            self.version_combo.addItem(v)
        if cur and (cur == "全部" or cur in self.RECENT_MC
                    or self.version_combo.findText(cur) >= 0):
            self.version_combo.setCurrentText(cur)
        self.version_combo.blockSignals(False)

    def _reset(self):
        self.name_edit.clear()
        self.version_combo.setCurrentIndex(0)
        self._do_search()
        self.search_status.setText("已重置搜索条件。")

    def _filter_version(self):
        t = self.version_combo.currentText().strip()
        if not t or t == "全部":
            return None
        return t

    def _do_search(self):
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._fetch_page(append=False)

    def _load_more(self):
        self._fetch_page(append=True)

    def _fetch_page(self, append):
        q = self.name_edit.text().strip()
        gv = self._filter_version()
        if not append:
            self._clear_results()
        self.search_status.setText("正在搜索…")
        self._next_btn.setEnabled(False)
        w = ModpackSearchWorker(q, gv, offset=self._offset)
        self._sw = w
        _track_worker(w)
        w.finished.connect(lambda hits, total: self._on_page(hits, total, append))
        w.error.connect(lambda e: self.search_status.setText("搜索失败：" + e))
        w.start()

    def _clear_results(self):
        self._rows = []
        while self.results_layout.count() > 2:
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _on_page(self, hits, total, append):
        self._next_btn.setEnabled(True)
        self._total = total
        if not hits:
            base = "没有更多了" if append else "没有找到相关整合包"
            self.search_status.setText(base + f"（共 {total} 个）。")
            self._next_btn.setVisible(self._loaded < total)
            return

        self._offset += len(hits)
        self._loaded += len(hits)
        tip = f"{'已显示' if append else '找到'} {self._loaded} / 共 {total} 个整合包"
        self.search_status.setText(tip + "。")

        icons = []
        for h in hits:
            row = ModRow(h, "")
            row.open_detail.connect(self._open_detail)
            self.results_layout.insertWidget(
                self.results_layout.count() - 2, row)
            self._rows.append(row)
            if h.get("icon_url"):
                icons.append((len(self._rows) - 1, h["icon_url"]))
        if icons:
            w = IconBatchWorker(icons)
            self._iw = w
            _track_worker(w)
            w.icon_ready.connect(self._on_icon)
            w.start()
        self._next_btn.setVisible(self._loaded < total)

    def _on_icon(self, idx, data):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_icon_bytes(data)

    def _open_detail(self, pid):
        self.pages.setCurrentWidget(self.detail_page)
        self.detail_page.load(pid)

    def _back_to_browse(self):
        self.pages.setCurrentWidget(self.pages.widget(0))

    def showEvent(self, e):
        self.refresh()
        if not self._ever_loaded:
            self._ever_loaded = True
            self._do_search()
        super().showEvent(e)


# ==========================================================================
# 数据包浏览 / 详情 / 下载 页面
# ==========================================================================
class DatapackDetailPage(QWidget):
    """数据包详情页：信息卡 + 持久的世界选择卡 + 版本列表，
    每版本一个『下载到世界』按钮（数据包放入所选世界的 datapacks 文件夹）。"""
    back_clicked = pyqtSignal()

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.pid = None
        self._project = {}
        self._versions = []
        self._iw = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        back = make_ghost("‹ 返回")
        back.setMaximumWidth(100)
        back.clicked.connect(self.back_clicked.emit)
        root.addWidget(back)

        # 世界选择（持久卡，不被详情渲染清空）
        wcard = make_card()
        wl = QVBoxLayout(wcard)
        wl.setContentsMargins(14, 12, 14, 12)
        wl.setSpacing(6)
        wh = QLabel("下载到世界（数据包会放进该世界存档的 datapacks 文件夹）")
        wh.setObjectName("HintText")
        wl.addWidget(wh)
        self.world_combo = QComboBox()
        self.world_combo.setMinimumHeight(36)
        self.world_combo.setEditable(True)
        self.world_combo.lineEdit().setPlaceholderText(
            "选择已有世界，或输入新世界名（将新建到游戏目录 saves 下）")
        self.world_combo.currentTextChanged.connect(
            lambda _: self._update_dest_hint())
        wl.addWidget(self.world_combo)
        self.dest_hint = QLabel()
        self.dest_hint.setObjectName("HintText")
        wl.addWidget(self.dest_hint)
        root.addWidget(wcard)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

        self.status = QLabel()
        self.status.setObjectName("HintText")
        root.addWidget(self.status)

    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _refresh_worlds(self):
        self.world_combo.blockSignals(True)
        self.world_combo.clear()
        self.world_combo.addItem("（请选择世界）", None)
        for name, wdir in discover_worlds(self.main.core):
            self.world_combo.addItem(name, wdir)
        self.world_combo.blockSignals(False)
        self._update_dest_hint()

    def _current_world_dir(self):
        data = self.world_combo.currentData()
        if data:
            return data
        text = (self.world_combo.currentText() or "").strip()
        if text and text != "（请选择世界）":
            # 自定义世界名 → 新建到全局游戏目录的 saves 下
            return os.path.join(self.main.core.game_root, "saves", text)
        return None

    def _update_dest_hint(self):
        d = self._current_world_dir()
        if d:
            self.dest_hint.setText(
                "目标文件夹： " + os.path.join(d, "datapacks"))
        else:
            self.dest_hint.setText(
                "目标：未选择世界（数据包需放入某个世界的 datapacks 文件夹）")

    def load(self, pid):
        self.pid = pid
        self._project = {}
        self._versions = []
        self._clear_content()
        self.content_layout.addWidget(QLabel("正在加载数据包信息…"))
        self._refresh_worlds()
        w = ModrinthDetailWorker(pid)     # 与 MOD / 整合包相同的接口
        self._iw = w
        _track_worker(w)
        w.finished.connect(self._on_loaded)
        w.error.connect(lambda e: (self._clear_content(),
                                   self.content_layout.addWidget(
                                       QLabel("加载失败：" + e))))
        w.start()

    def _on_loaded(self, project, versions):
        self._project = project or {}
        self._versions = versions or []
        self._render()

    def _render(self):
        p = self._project
        self._clear_content()

        card = make_card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon_lbl = QLabel("📁")
        icon_lbl.setFixedSize(52, 52)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            "background:rgba(128,128,128,0.15);border-radius:6px;font-size:26px;")
        top.addWidget(icon_lbl)
        self._detail_icon_lbl = icon_lbl
        if p.get("icon_url"):
            url = p["icon_url"]
            data = _ICON_CACHE.get(url)
            if data is not None:
                pm = QPixmap()
                if pm.loadFromData(data):
                    icon_lbl.setPixmap(pm.scaled(
                        52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
            else:
                w = IconBatchWorker([(0, url)])
                self._iw_icon = w
                _track_worker(w)
                w.icon_ready.connect(self._on_detail_icon)
                w.start()

        info = QVBoxLayout()
        info.setSpacing(3)
        t1 = QLabel(p.get("title") or "")
        t1.setStyleSheet("font-size:17px;font-weight:bold;")
        info.addWidget(t1)
        if p.get("slug"):
            t2 = QLabel(p.get("slug"))
            t2.setObjectName("HintText")
            info.addWidget(t2)
        cats = [MOD_CATEGORY_CN.get(c, c)
                for c in (p.get("display_categories") or [])][:4]
        if cats:
            tg = QLabel(" | ".join(cats))
            tg.setObjectName("HintText")
            info.addWidget(tg)
        if p.get("description"):
            d = QLabel(p["description"])
            d.setObjectName("HintText")
            d.setWordWrap(True)
            info.addWidget(d)
        meta = QLabel(f"⬇ {fmt_downloads_cn(p.get('downloads', 0))}"
                      + (f"    ↑ {fmt_rel_time(p.get('date_modified', ''))}"
                         if p.get("date_modified") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        top.addLayout(info, 1)
        cl.addLayout(top)

        btns = QHBoxLayout()
        b1 = make_ghost("转到 Modrinth")
        web_url = p.get("web_url") or \
            f"https://modrinth.com/{p.get('project_type', 'datapack')}/{p.get('slug', '')}"
        b1.clicked.connect(lambda: webbrowser.open(web_url))
        b2 = make_ghost("复制名称")
        b2.clicked.connect(lambda: QApplication.clipboard().setText(
            p.get("title") or ""))
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch(1)
        cl.addLayout(btns)
        self.content_layout.addWidget(card)

        hint = QLabel("选择一个版本与上方世界，点『下载到世界』即可（数据包为 .zip）：")
        hint.setObjectName("HintText")
        self.content_layout.addWidget(hint)

        # 仅列出含数据包文件（.zip）的版本
        ordered = sorted(
            [v for v in self._versions if _pick_datapack_file(v)],
            key=lambda v: _ver_sort_key(v.get("version_number")
                                        or (v.get("game_versions") or [""])[0]),
            reverse=True)
        if not ordered:
            no = QLabel("（该项目没有可直接作为数据包下载的 .zip 文件）")
            no.setObjectName("HintText")
            self.content_layout.addWidget(no)
        for v in ordered:
            self.content_layout.addWidget(self._version_row(v))

        self.content_layout.addStretch(1)

    def _version_row(self, v):
        f = _pick_datapack_file(v)
        row = make_card()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(14, 12, 14, 12)
        rl.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(3)
        name = QLabel(v.get("name") or v.get("version_number") or "版本")
        name.setStyleSheet("font-size:14px;font-weight:bold;")
        info.addWidget(name)
        gvs = ", ".join(v.get("game_versions") or [])
        fn = (f.get("filename") or "") if f else ""
        meta = QLabel(f"◎ {gvs or '未知'}"
                      + (f"    📄 {fn}" if fn else "")
                      + (f"    ⬇ {fmt_downloads_cn(v.get('downloads', 0))}"
                         if v.get("downloads") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        rl.addLayout(info, 1)

        btn = make_primary("下载到世界")
        btn.setMaximumWidth(150)
        btn.clicked.connect(lambda _, ver=v, ff=f: self._download(ver, ff))
        rl.addWidget(btn)
        return row

    def _on_detail_icon(self, idx, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            self._detail_icon_lbl.setPixmap(pm.scaled(
                52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _download(self, v, f):
        wdir = self._current_world_dir()
        if not wdir:
            QMessageBox.information(self, "提示", "请先在上方选择或输入一个世界。")
            return
        if not f or not f.get("url"):
            QMessageBox.information(self, "提示", "该版本没有可下载的数据包文件。")
            return
        url = f["url"]
        fn = f.get("filename") or (self._project.get("slug", "datapack") + ".zip")
        dest = os.path.join(wdir, "datapacks")
        os.makedirs(dest, exist_ok=True)
        w = ModFileDownloadWorker(url, fn, dest)
        self._iw = w
        _track_worker(w)
        w.status.connect(self.status.setText)
        w.error.connect(lambda e: self.status.setText("下载失败：" + e))
        w.finished.connect(lambda p: (self.status.setText(
            f"✅ 已下载 {os.path.basename(p)} → {dest}"),
            self._update_dest_hint()))
        w.start()


class DatapackPage(QWidget):
    """数据包浏览页：搜索 / 按下载量浏览 / 点开详情选择世界下载。"""
    RECENT_MC = ["1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.19.4",
                 "1.19.2", "1.18.2", "1.16.5", "1.12.2"]

    def __init__(self, main):
        super().__init__()
        self.main = main
        self._rows = []
        self._sw = None
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._ever_loaded = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QLabel("搜索数据包")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        browse = QWidget()
        bv = QVBoxLayout(browse)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(12)

        card = make_card()
        grid = QGridLayout(card)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("名称"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("SearchEdit")
        self.name_edit.setPlaceholderText("留空则浏览全部数据包；或输入关键词搜索")
        self.name_edit.returnPressed.connect(self._do_search)
        grid.addWidget(self.name_edit, 0, 1, 1, 3)

        grid.addWidget(QLabel("版本"), 1, 0)
        self.version_combo = QComboBox()
        self.version_combo.setMinimumHeight(36)
        self.version_combo.setEditable(True)
        self.version_combo.setPlaceholderText("全部版本")
        grid.addWidget(self.version_combo, 1, 1, 1, 3)

        btn_row = QHBoxLayout()
        self.search_btn = make_primary("搜索")
        self.search_btn.setMaximumWidth(140)
        self.search_btn.clicked.connect(self._do_search)
        btn_row.addWidget(self.search_btn)
        self.reset_btn = make_ghost("重置条件")
        self.reset_btn.setMaximumWidth(140)
        self.reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(self.reset_btn)
        btn_row.addStretch(1)
        grid.addLayout(btn_row, 2, 0, 1, 4)

        bv.addWidget(card)

        self.search_status = QLabel()
        self.search_status.setObjectName("HintText")
        bv.addWidget(self.search_status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.results_layout = QVBoxLayout(scroll_content)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(8)
        self._next_btn = make_ghost("下一页  ↓")
        self._next_btn.clicked.connect(self._load_more)
        self.results_layout.addWidget(self._next_btn)
        self.results_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        bv.addWidget(scroll, 1)

        note = make_card()
        nl = QVBoxLayout(note)
        nl.setContentsMargins(14, 12, 14, 12)
        nl.setSpacing(6)
        nl.addWidget(QLabel("使用说明"))
        tip = QLabel("① 搜索或浏览数据包 → ② 点开某个数据包 → ③ 选择世界后点『下载到世界』。"
                     "启动器会把 .zip 数据包放进该世界存档的 datapacks 文件夹，"
                     "进入对应世界即可生效（无需任何加载器）。\n"
                     "（上方可下拉选择已有世界，或直接输入新世界名，将自动新建到游戏目录的 saves 下。）")
        tip.setObjectName("HintText")
        tip.setWordWrap(True)
        nl.addWidget(tip)
        bv.addWidget(note)

        self.pages.addWidget(browse)          # 0 浏览
        self.detail_page = DatapackDetailPage(self.main)
        self.detail_page.back_clicked.connect(self._back_to_browse)
        self.pages.addWidget(self.detail_page)  # 1 详情

        self.refresh()

    def refresh(self):
        self.version_combo.blockSignals(True)
        cur = self.version_combo.currentText()
        self.version_combo.clear()
        self.version_combo.addItem("全部")
        for v in self.RECENT_MC:
            self.version_combo.addItem(v)
        if cur and (cur == "全部" or cur in self.RECENT_MC
                    or self.version_combo.findText(cur) >= 0):
            self.version_combo.setCurrentText(cur)
        self.version_combo.blockSignals(False)

    def _reset(self):
        self.name_edit.clear()
        self.version_combo.setCurrentIndex(0)
        self._do_search()
        self.search_status.setText("已重置搜索条件。")

    def _filter_version(self):
        t = self.version_combo.currentText().strip()
        if not t or t == "全部":
            return None
        return t

    def _do_search(self):
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._fetch_page(append=False)

    def _load_more(self):
        self._fetch_page(append=True)

    def _fetch_page(self, append):
        q = self.name_edit.text().strip()
        gv = self._filter_version()
        if not append:
            self._clear_results()
        self.search_status.setText("正在搜索…")
        self._next_btn.setEnabled(False)
        w = DatapackSearchWorker(q, gv, offset=self._offset)
        self._sw = w
        _track_worker(w)
        w.finished.connect(lambda hits, total: self._on_page(hits, total, append))
        w.error.connect(lambda e: self.search_status.setText("搜索失败：" + e))
        w.start()

    def _clear_results(self):
        self._rows = []
        while self.results_layout.count() > 2:
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _on_page(self, hits, total, append):
        self._next_btn.setEnabled(True)
        # Modrinth 的 project_type:datapack facet 会混入“含 datapack 分类”的
        # MOD，需客户端过滤只保留真正带 datapack 标签的项目
        dp_hits = [h for h in hits
                   if "datapack" in (h.get("display_categories") or [])]
        self._offset += len(hits)          # 推进到下一个服务端分页
        if not dp_hits:
            if len(hits) < MOD_PAGE_SIZE:
                self.search_status.setText("没有更多了。")
            else:
                self.search_status.setText(
                    "本页没有匹配的数据包，点『下一页』继续…")
            self._next_btn.setVisible(len(hits) == MOD_PAGE_SIZE)
            return

        self._loaded += len(dp_hits)
        gv = self._filter_version()
        tip = f"{'已显示' if append else '找到'} {self._loaded} 个数据包"
        if gv:
            tip += f"（版本 {gv}）"
        self.search_status.setText(tip + "。点开数据包选择世界下载。")

        icons = []
        for h in dp_hits:
            row = ModRow(h, "")
            row.open_detail.connect(self._open_detail)
            self.results_layout.insertWidget(
                self.results_layout.count() - 2, row)
            self._rows.append(row)
            if h.get("icon_url"):
                icons.append((len(self._rows) - 1, h["icon_url"]))
        if icons:
            w = IconBatchWorker(icons)
            self._iw = w
            _track_worker(w)
            w.icon_ready.connect(self._on_icon)
            w.start()
        # 服务端还有下一页（按原始返回条数判断，过滤后可能为空但仍有更多）
        self._next_btn.setVisible(len(hits) == MOD_PAGE_SIZE)

    def _on_icon(self, idx, data):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_icon_bytes(data)

    def _open_detail(self, pid):
        self.pages.setCurrentWidget(self.detail_page)
        self.detail_page.load(pid)

    def _back_to_browse(self):
        self.pages.setCurrentWidget(self.pages.widget(0))

    def showEvent(self, e):
        self.refresh()
        if not self._ever_loaded:
            self._ever_loaded = True
            self._do_search()
        super().showEvent(e)


class ResourcePackDetailPage(QWidget):
    """资源包详情页：信息卡 + 持久的安装位置选择卡 + 版本列表，
    每版本一个『下载资源包』按钮（.zip 放入所选目标的 resourcepacks 文件夹）。"""
    back_clicked = pyqtSignal()

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.pid = None
        self._project = {}
        self._versions = []
        self._iw = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        back = make_ghost("‹ 返回")
        back.setMaximumWidth(100)
        back.clicked.connect(self.back_clicked.emit)
        root.addWidget(back)

        # 安装位置选择（持久卡，不被详情渲染清空）
        tcard = make_card()
        tl = QVBoxLayout(tcard)
        tl.setContentsMargins(14, 12, 14, 12)
        tl.setSpacing(6)
        th = QLabel("安装到（资源包会放进所选位置的 resourcepacks 文件夹）")
        th.setObjectName("HintText")
        tl.addWidget(th)
        self.target_combo = QComboBox()
        self.target_combo.setMinimumHeight(36)
        self.target_combo.currentTextChanged.connect(
            lambda _: self._update_dest_hint())
        tl.addWidget(self.target_combo)
        self.dest_hint = QLabel()
        self.dest_hint.setObjectName("HintText")
        tl.addWidget(self.dest_hint)
        root.addWidget(tcard)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

        self.status = QLabel()
        self.status.setObjectName("HintText")
        root.addWidget(self.status)

    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _refresh_targets(self):
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for name, d in discover_resource_targets(self.main.core):
            self.target_combo.addItem(name, d)
        self.target_combo.blockSignals(False)
        self._update_dest_hint()

    def _current_target_dir(self):
        data = self.target_combo.currentData()
        return data or self.main.core.game_root

    def _update_dest_hint(self):
        d = self._current_target_dir()
        self.dest_hint.setText(
            "目标文件夹： " + os.path.join(d, "resourcepacks"))

    def load(self, pid):
        self.pid = pid
        self._project = {}
        self._versions = []
        self._clear_content()
        self.content_layout.addWidget(QLabel("正在加载资源包信息…"))
        self._refresh_targets()
        w = ModrinthDetailWorker(pid)     # 与 MOD / 整合包 / 数据包相同接口
        self._iw = w
        _track_worker(w)
        w.finished.connect(self._on_loaded)
        w.error.connect(lambda e: (self._clear_content(),
                                   self.content_layout.addWidget(
                                       QLabel("加载失败：" + e))))
        w.start()

    def _on_loaded(self, project, versions):
        self._project = project or {}
        self._versions = versions or []
        self._render()

    def _render(self):
        p = self._project
        self._clear_content()

        card = make_card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon_lbl = QLabel("🎨")
        icon_lbl.setFixedSize(52, 52)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            "background:rgba(128,128,128,0.15);border-radius:6px;font-size:26px;")
        top.addWidget(icon_lbl)
        self._detail_icon_lbl = icon_lbl
        if p.get("icon_url"):
            url = p["icon_url"]
            data = _ICON_CACHE.get(url)
            if data is not None:
                pm = QPixmap()
                if pm.loadFromData(data):
                    icon_lbl.setPixmap(pm.scaled(
                        52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
            else:
                w = IconBatchWorker([(0, url)])
                self._iw_icon = w
                _track_worker(w)
                w.icon_ready.connect(self._on_detail_icon)
                w.start()

        info = QVBoxLayout()
        info.setSpacing(3)
        t1 = QLabel(p.get("title") or "")
        t1.setStyleSheet("font-size:17px;font-weight:bold;")
        info.addWidget(t1)
        if p.get("slug"):
            t2 = QLabel(p.get("slug"))
            t2.setObjectName("HintText")
            info.addWidget(t2)
        cats = [MOD_CATEGORY_CN.get(c, c)
                for c in (p.get("display_categories") or [])][:4]
        if cats:
            tg = QLabel(" | ".join(cats))
            tg.setObjectName("HintText")
            info.addWidget(tg)
        if p.get("description"):
            d = QLabel(p["description"])
            d.setObjectName("HintText")
            d.setWordWrap(True)
            info.addWidget(d)
        meta = QLabel(f"⬇ {fmt_downloads_cn(p.get('downloads', 0))}"
                      + (f"    ↑ {fmt_rel_time(p.get('date_modified', ''))}"
                         if p.get("date_modified") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        top.addLayout(info, 1)
        cl.addLayout(top)

        btns = QHBoxLayout()
        b1 = make_ghost("转到 Modrinth")
        web_url = p.get("web_url") or \
            f"https://modrinth.com/{p.get('project_type', 'resourcepack')}/{p.get('slug', '')}"
        b1.clicked.connect(lambda: webbrowser.open(web_url))
        b2 = make_ghost("复制名称")
        b2.clicked.connect(lambda: QApplication.clipboard().setText(
            p.get("title") or ""))
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch(1)
        cl.addLayout(btns)
        self.content_layout.addWidget(card)

        hint = QLabel("选择一个版本与上方安装位置，点『下载资源包』即可（资源包为 .zip）：")
        hint.setObjectName("HintText")
        self.content_layout.addWidget(hint)

        ordered = sorted(
            [v for v in self._versions if _pick_resourcepack_file(v)],
            key=lambda v: _ver_sort_key(v.get("version_number")
                                        or (v.get("game_versions") or [""])[0]),
            reverse=True)
        if not ordered:
            no = QLabel("（该项目没有可直接下载的资源包 .zip 文件）")
            no.setObjectName("HintText")
            self.content_layout.addWidget(no)
        for v in ordered:
            self.content_layout.addWidget(self._version_row(v))

        self.content_layout.addStretch(1)

    def _version_row(self, v):
        f = _pick_resourcepack_file(v)
        row = make_card()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(14, 12, 14, 12)
        rl.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(3)
        name = QLabel(v.get("name") or v.get("version_number") or "版本")
        name.setStyleSheet("font-size:14px;font-weight:bold;")
        info.addWidget(name)
        gvs = ", ".join(v.get("game_versions") or [])
        fn = (f.get("filename") or "") if f else ""
        meta = QLabel(f"◎ {gvs or '未知'}"
                      + (f"    📄 {fn}" if fn else "")
                      + (f"    ⬇ {fmt_downloads_cn(v.get('downloads', 0))}"
                         if v.get("downloads") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        rl.addLayout(info, 1)

        btn = make_primary("下载资源包")
        btn.setMaximumWidth(150)
        btn.clicked.connect(lambda _, ver=v, ff=f: self._download(ver, ff))
        rl.addWidget(btn)
        return row

    def _on_detail_icon(self, idx, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            self._detail_icon_lbl.setPixmap(pm.scaled(
                52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _download(self, v, f):
        tdir = self._current_target_dir()
        if not f or not f.get("url"):
            QMessageBox.information(self, "提示", "该版本没有可下载的资源包文件。")
            return
        url = f["url"]
        fn = f.get("filename") or (self._project.get("slug", "resourcepack") + ".zip")
        dest = os.path.join(tdir, "resourcepacks")
        os.makedirs(dest, exist_ok=True)
        w = ModFileDownloadWorker(url, fn, dest)
        self._iw = w
        _track_worker(w)
        w.status.connect(self.status.setText)
        w.error.connect(lambda e: self.status.setText("下载失败：" + e))
        w.finished.connect(lambda p: (self.status.setText(
            f"✅ 已下载 {os.path.basename(p)} → {dest}"),
            self._update_dest_hint()))
        w.start()


class ResourcePackPage(QWidget):
    """资源包浏览页：搜索 / 按下载量浏览 / 点开详情选择安装位置下载。"""
    RECENT_MC = ["1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.19.4",
                 "1.19.2", "1.18.2", "1.16.5", "1.12.2"]

    def __init__(self, main):
        super().__init__()
        self.main = main
        self._rows = []
        self._sw = None
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._ever_loaded = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QLabel("搜索资源包")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        browse = QWidget()
        bv = QVBoxLayout(browse)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(12)

        card = make_card()
        grid = QGridLayout(card)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("名称"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("SearchEdit")
        self.name_edit.setPlaceholderText("留空则浏览全部资源包；或输入关键词搜索")
        self.name_edit.returnPressed.connect(self._do_search)
        grid.addWidget(self.name_edit, 0, 1, 1, 3)

        grid.addWidget(QLabel("版本"), 1, 0)
        self.version_combo = QComboBox()
        self.version_combo.setMinimumHeight(36)
        self.version_combo.setEditable(True)
        self.version_combo.setPlaceholderText("全部版本")
        grid.addWidget(self.version_combo, 1, 1, 1, 3)

        btn_row = QHBoxLayout()
        self.search_btn = make_primary("搜索")
        self.search_btn.setMaximumWidth(140)
        self.search_btn.clicked.connect(self._do_search)
        btn_row.addWidget(self.search_btn)
        self.reset_btn = make_ghost("重置条件")
        self.reset_btn.setMaximumWidth(140)
        self.reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(self.reset_btn)
        btn_row.addStretch(1)
        grid.addLayout(btn_row, 2, 0, 1, 4)

        bv.addWidget(card)

        self.search_status = QLabel()
        self.search_status.setObjectName("HintText")
        bv.addWidget(self.search_status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.results_layout = QVBoxLayout(scroll_content)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(8)
        self._next_btn = make_ghost("下一页  ↓")
        self._next_btn.clicked.connect(self._load_more)
        self.results_layout.addWidget(self._next_btn)
        self.results_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        bv.addWidget(scroll, 1)

        note = make_card()
        nl = QVBoxLayout(note)
        nl.setContentsMargins(14, 12, 14, 12)
        nl.setSpacing(6)
        nl.addWidget(QLabel("使用说明"))
        tip = QLabel("① 搜索或浏览资源包 → ② 点开某个资源包 → ③ 选择安装位置后点『下载资源包』。"
                     "启动器会把 .zip 资源包放进所选位置（默认全局 .minecraft）的 resourcepacks 文件夹，"
                     "在游戏内『资源包』设置里即可启用（无需任何加载器）。\n"
                     "（整合包实例可在上方下拉选择，资源包将仅对该实例生效。）")
        tip.setObjectName("HintText")
        tip.setWordWrap(True)
        nl.addWidget(tip)
        bv.addWidget(note)

        self.pages.addWidget(browse)          # 0 浏览
        self.detail_page = ResourcePackDetailPage(self.main)
        self.detail_page.back_clicked.connect(self._back_to_browse)
        self.pages.addWidget(self.detail_page)  # 1 详情

        self.refresh()

    def refresh(self):
        self.version_combo.blockSignals(True)
        cur = self.version_combo.currentText()
        self.version_combo.clear()
        self.version_combo.addItem("全部")
        for v in self.RECENT_MC:
            self.version_combo.addItem(v)
        if cur and (cur == "全部" or cur in self.RECENT_MC
                    or self.version_combo.findText(cur) >= 0):
            self.version_combo.setCurrentText(cur)
        self.version_combo.blockSignals(False)

    def _reset(self):
        self.name_edit.clear()
        self.version_combo.setCurrentIndex(0)
        self._do_search()
        self.search_status.setText("已重置搜索条件。")

    def _filter_version(self):
        t = self.version_combo.currentText().strip()
        if not t or t == "全部":
            return None
        return t

    def _do_search(self):
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._fetch_page(append=False)

    def _load_more(self):
        self._fetch_page(append=True)

    def _fetch_page(self, append):
        q = self.name_edit.text().strip()
        gv = self._filter_version()
        if not append:
            self._clear_results()
        self.search_status.setText("正在搜索…")
        self._next_btn.setEnabled(False)
        w = ResourcePackSearchWorker(q, gv, offset=self._offset)
        self._sw = w
        _track_worker(w)
        w.finished.connect(lambda hits, total: self._on_page(hits, total, append))
        w.error.connect(lambda e: self.search_status.setText("搜索失败：" + e))
        w.start()

    def _clear_results(self):
        self._rows = []
        while self.results_layout.count() > 2:
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _on_page(self, hits, total, append):
        self._next_btn.setEnabled(True)
        self._total = total
        # Modrinth 的 project_type:resourcepack facet 偶尔会混入极少量 MOD，
        # 客户端过滤只保留真正的资源包
        rp_hits = [h for h in hits if h.get("project_type") == "resourcepack"]
        self._offset += len(hits)     # 推进到下一个服务端分页
        if not rp_hits:
            if len(hits) < MOD_PAGE_SIZE:
                self.search_status.setText("没有更多了。")
            else:
                self.search_status.setText("本页没有匹配的资源包，点『下一页』继续…")
            self._next_btn.setVisible(len(hits) == MOD_PAGE_SIZE)
            return

        self._loaded += len(rp_hits)
        tip = f"{'已显示' if append else '找到'} {self._loaded} 个资源包"
        gv = self._filter_version()
        if gv:
            tip += f"（版本 {gv}）"
        if total:
            tip += f"（共 {total} 个）"
        self.search_status.setText(tip + "。")

        icons = []
        for h in rp_hits:
            row = ModRow(h, "")
            row.open_detail.connect(self._open_detail)
            self.results_layout.insertWidget(
                self.results_layout.count() - 2, row)
            self._rows.append(row)
            if h.get("icon_url"):
                icons.append((len(self._rows) - 1, h["icon_url"]))
        if icons:
            w = IconBatchWorker(icons)
            self._iw = w
            _track_worker(w)
            w.icon_ready.connect(self._on_icon)
            w.start()
        self._next_btn.setVisible(len(hits) == MOD_PAGE_SIZE)

    def _on_icon(self, idx, data):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_icon_bytes(data)

    def _open_detail(self, pid):
        self.pages.setCurrentWidget(self.detail_page)
        self.detail_page.load(pid)

    def _back_to_browse(self):
        self.pages.setCurrentWidget(self.pages.widget(0))

    def showEvent(self, e):
        self.refresh()
        if not self._ever_loaded:
            self._ever_loaded = True
            self._do_search()
        super().showEvent(e)


class ShaderDetailPage(QWidget):
    """光影包详情页：信息卡 + 持久的安装位置选择卡 + 版本列表，
    每版本一个『下载光影包』按钮（.zip 放入所选目标的 shaderpacks 文件夹）。"""
    back_clicked = pyqtSignal()

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.pid = None
        self._project = {}
        self._versions = []
        self._iw = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        back = make_ghost("‹ 返回")
        back.setMaximumWidth(100)
        back.clicked.connect(self.back_clicked.emit)
        root.addWidget(back)

        # 安装位置选择（持久卡，不被详情渲染清空）
        tcard = make_card()
        tl = QVBoxLayout(tcard)
        tl.setContentsMargins(14, 12, 14, 12)
        tl.setSpacing(6)
        th = QLabel("安装到（光影包会放进所选位置的 shaderpacks 文件夹）")
        th.setObjectName("HintText")
        tl.addWidget(th)
        self.target_combo = QComboBox()
        self.target_combo.setMinimumHeight(36)
        self.target_combo.currentTextChanged.connect(
            lambda _: self._update_dest_hint())
        tl.addWidget(self.target_combo)
        self.dest_hint = QLabel()
        self.dest_hint.setObjectName("HintText")
        tl.addWidget(self.dest_hint)
        root.addWidget(tcard)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

        self.status = QLabel()
        self.status.setObjectName("HintText")
        root.addWidget(self.status)

    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _refresh_targets(self):
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for name, d in discover_shader_targets(self.main.core):
            self.target_combo.addItem(name, d)
        self.target_combo.blockSignals(False)
        self._update_dest_hint()

    def _current_target_dir(self):
        data = self.target_combo.currentData()
        return data or self.main.core.game_root

    def _update_dest_hint(self):
        d = self._current_target_dir()
        self.dest_hint.setText(
            "目标文件夹： " + os.path.join(d, "shaderpacks"))

    def load(self, pid):
        self.pid = pid
        self._project = {}
        self._versions = []
        self._clear_content()
        self.content_layout.addWidget(QLabel("正在加载光影包信息…"))
        self._refresh_targets()
        w = ModrinthDetailWorker(pid)     # 与 MOD / 整合包 / 数据包 / 资源包相同接口
        self._iw = w
        _track_worker(w)
        w.finished.connect(self._on_loaded)
        w.error.connect(lambda e: (self._clear_content(),
                                   self.content_layout.addWidget(
                                       QLabel("加载失败：" + e))))
        w.start()

    def _on_loaded(self, project, versions):
        self._project = project or {}
        self._versions = versions or []
        self._render()

    def _render(self):
        p = self._project
        self._clear_content()

        card = make_card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon_lbl = QLabel("💡")
        icon_lbl.setFixedSize(52, 52)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            "background:rgba(128,128,128,0.15);border-radius:6px;font-size:26px;")
        top.addWidget(icon_lbl)
        self._detail_icon_lbl = icon_lbl
        if p.get("icon_url"):
            url = p["icon_url"]
            data = _ICON_CACHE.get(url)
            if data is not None:
                pm = QPixmap()
                if pm.loadFromData(data):
                    icon_lbl.setPixmap(pm.scaled(
                        52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
            else:
                w = IconBatchWorker([(0, url)])
                self._iw_icon = w
                _track_worker(w)
                w.icon_ready.connect(self._on_detail_icon)
                w.start()

        info = QVBoxLayout()
        info.setSpacing(3)
        t1 = QLabel(p.get("title") or "")
        t1.setStyleSheet("font-size:17px;font-weight:bold;")
        info.addWidget(t1)
        if p.get("slug"):
            t2 = QLabel(p.get("slug"))
            t2.setObjectName("HintText")
            info.addWidget(t2)
        cats = [MOD_CATEGORY_CN.get(c, c)
                for c in (p.get("display_categories") or [])][:4]
        if cats:
            tg = QLabel(" | ".join(cats))
            tg.setObjectName("HintText")
            info.addWidget(tg)
        if p.get("description"):
            d = QLabel(p["description"])
            d.setObjectName("HintText")
            d.setWordWrap(True)
            info.addWidget(d)
        meta = QLabel(f"⬇ {fmt_downloads_cn(p.get('downloads', 0))}"
                      + (f"    ↑ {fmt_rel_time(p.get('date_modified', ''))}"
                         if p.get("date_modified") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        top.addLayout(info, 1)
        cl.addLayout(top)

        btns = QHBoxLayout()
        b1 = make_ghost("转到 Modrinth")
        web_url = p.get("web_url") or \
            f"https://modrinth.com/{p.get('project_type', 'shader')}/{p.get('slug', '')}"
        b1.clicked.connect(lambda: webbrowser.open(web_url))
        b2 = make_ghost("复制名称")
        b2.clicked.connect(lambda: QApplication.clipboard().setText(
            p.get("title") or ""))
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch(1)
        cl.addLayout(btns)
        self.content_layout.addWidget(card)

        hint = QLabel("选择一个版本与上方安装位置，点『下载光影包』即可（光影包为 .zip）：")
        hint.setObjectName("HintText")
        self.content_layout.addWidget(hint)

        ordered = sorted(
            [v for v in self._versions if _pick_shader_file(v)],
            key=lambda v: _ver_sort_key(v.get("version_number")
                                        or (v.get("game_versions") or [""])[0]),
            reverse=True)
        if not ordered:
            no = QLabel("（该项目没有可直接下载的光影包 .zip 文件）")
            no.setObjectName("HintText")
            self.content_layout.addWidget(no)
        for v in ordered:
            self.content_layout.addWidget(self._version_row(v))

        self.content_layout.addStretch(1)

    def _version_row(self, v):
        f = _pick_shader_file(v)
        row = make_card()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(14, 12, 14, 12)
        rl.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(3)
        name = QLabel(v.get("name") or v.get("version_number") or "版本")
        name.setStyleSheet("font-size:14px;font-weight:bold;")
        info.addWidget(name)
        gvs = ", ".join(v.get("game_versions") or [])
        fn = (f.get("filename") or "") if f else ""
        meta = QLabel(f"◎ {gvs or '未知'}"
                      + (f"    📄 {fn}" if fn else "")
                      + (f"    ⬇ {fmt_downloads_cn(v.get('downloads', 0))}"
                         if v.get("downloads") else ""))
        meta.setObjectName("HintText")
        info.addWidget(meta)
        rl.addLayout(info, 1)

        btn = make_primary("下载光影包")
        btn.setMaximumWidth(150)
        btn.clicked.connect(lambda _, ver=v, ff=f: self._download(ver, ff))
        rl.addWidget(btn)
        return row

    def _on_detail_icon(self, idx, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            self._detail_icon_lbl.setPixmap(pm.scaled(
                52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _download(self, v, f):
        tdir = self._current_target_dir()
        if not f or not f.get("url"):
            QMessageBox.information(self, "提示", "该版本没有可下载的光影包文件。")
            return
        url = f["url"]
        fn = f.get("filename") or (self._project.get("slug", "shader") + ".zip")
        dest = os.path.join(tdir, "shaderpacks")
        os.makedirs(dest, exist_ok=True)
        w = ModFileDownloadWorker(url, fn, dest)
        self._iw = w
        _track_worker(w)
        w.status.connect(self.status.setText)
        w.error.connect(lambda e: self.status.setText("下载失败：" + e))
        w.finished.connect(lambda p: (self.status.setText(
            f"✅ 已下载 {os.path.basename(p)} → {dest}"),
            self._update_dest_hint()))
        w.start()


class ShaderPage(QWidget):
    """光影包浏览页：搜索 / 按下载量浏览 / 点开详情选择安装位置下载。"""
    RECENT_MC = ["1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.19.4",
                 "1.19.2", "1.18.2", "1.16.5", "1.12.2"]

    def __init__(self, main):
        super().__init__()
        self.main = main
        self._rows = []
        self._sw = None
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._ever_loaded = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QLabel("搜索光影包")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        browse = QWidget()
        bv = QVBoxLayout(browse)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(12)

        card = make_card()
        grid = QGridLayout(card)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("名称"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("SearchEdit")
        self.name_edit.setPlaceholderText("留空则浏览全部光影包；或输入关键词搜索")
        self.name_edit.returnPressed.connect(self._do_search)
        grid.addWidget(self.name_edit, 0, 1, 1, 3)

        grid.addWidget(QLabel("版本"), 1, 0)
        self.version_combo = QComboBox()
        self.version_combo.setMinimumHeight(36)
        self.version_combo.setEditable(True)
        self.version_combo.setPlaceholderText("全部版本")
        grid.addWidget(self.version_combo, 1, 1, 1, 3)

        btn_row = QHBoxLayout()
        self.search_btn = make_primary("搜索")
        self.search_btn.setMaximumWidth(140)
        self.search_btn.clicked.connect(self._do_search)
        btn_row.addWidget(self.search_btn)
        self.reset_btn = make_ghost("重置条件")
        self.reset_btn.setMaximumWidth(140)
        self.reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(self.reset_btn)
        btn_row.addStretch(1)
        grid.addLayout(btn_row, 2, 0, 1, 4)

        bv.addWidget(card)

        self.search_status = QLabel()
        self.search_status.setObjectName("HintText")
        bv.addWidget(self.search_status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.results_layout = QVBoxLayout(scroll_content)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(8)
        self._next_btn = make_ghost("下一页  ↓")
        self._next_btn.clicked.connect(self._load_more)
        self.results_layout.addWidget(self._next_btn)
        self.results_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        bv.addWidget(scroll, 1)

        note = make_card()
        nl = QVBoxLayout(note)
        nl.setContentsMargins(14, 12, 14, 12)
        nl.setSpacing(6)
        nl.addWidget(QLabel("使用说明"))
        tip = QLabel("① 搜索或浏览光影包 → ② 点开某个光影包 → ③ 选择安装位置后点『下载光影包』。"
                     "启动器会把 .zip 光影包放进所选位置（默认全局 .minecraft）的 shaderpacks 文件夹，"
                     "在游戏内『光影包』设置里即可启用（需安装 Iris 或 OptiFine 等光影加载器）。\n"
                     "（整合包实例可在上方下拉选择，光影包将仅对该实例生效。）")
        tip.setObjectName("HintText")
        tip.setWordWrap(True)
        nl.addWidget(tip)
        bv.addWidget(note)

        self.pages.addWidget(browse)          # 0 浏览
        self.detail_page = ShaderDetailPage(self.main)
        self.detail_page.back_clicked.connect(self._back_to_browse)
        self.pages.addWidget(self.detail_page)  # 1 详情

        self.refresh()

    def refresh(self):
        self.version_combo.blockSignals(True)
        cur = self.version_combo.currentText()
        self.version_combo.clear()
        self.version_combo.addItem("全部")
        for v in self.RECENT_MC:
            self.version_combo.addItem(v)
        if cur and (cur == "全部" or cur in self.RECENT_MC
                    or self.version_combo.findText(cur) >= 0):
            self.version_combo.setCurrentText(cur)
        self.version_combo.blockSignals(False)

    def _reset(self):
        self.name_edit.clear()
        self.version_combo.setCurrentIndex(0)
        self._do_search()
        self.search_status.setText("已重置搜索条件。")

    def _filter_version(self):
        t = self.version_combo.currentText().strip()
        if not t or t == "全部":
            return None
        return t

    def _do_search(self):
        self._offset = 0
        self._total = 0
        self._loaded = 0
        self._fetch_page(append=False)

    def _load_more(self):
        self._fetch_page(append=True)

    def _fetch_page(self, append):
        q = self.name_edit.text().strip()
        gv = self._filter_version()
        if not append:
            self._clear_results()
        self.search_status.setText("正在搜索…")
        self._next_btn.setEnabled(False)
        w = ShaderSearchWorker(q, gv, offset=self._offset)
        self._sw = w
        _track_worker(w)
        w.finished.connect(lambda hits, total: self._on_page(hits, total, append))
        w.error.connect(lambda e: self.search_status.setText("搜索失败：" + e))
        w.start()

    def _clear_results(self):
        self._rows = []
        while self.results_layout.count() > 2:
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _on_page(self, hits, total, append):
        self._next_btn.setEnabled(True)
        self._total = total
        # Modrinth 的 project_type:shader facet 偶尔会混入极少量非光影包，
        # 客户端过滤只保留真正的 shader 类型
        shader_hits = [h for h in hits if h.get("project_type") == "shader"]
        self._offset += len(hits)     # 推进到下一个服务端分页
        if not shader_hits:
            if len(hits) < MOD_PAGE_SIZE:
                self.search_status.setText("没有更多了。")
            else:
                self.search_status.setText("本页没有匹配的光影包，点『下一页』继续…")
            self._next_btn.setVisible(len(hits) == MOD_PAGE_SIZE)
            return

        self._loaded += len(shader_hits)
        tip = f"{'已显示' if append else '找到'} {self._loaded} 个光影包"
        gv = self._filter_version()
        if gv:
            tip += f"（版本 {gv}）"
        if total:
            tip += f"（共 {total} 个）"
        self.search_status.setText(tip + "。")

        icons = []
        for h in shader_hits:
            row = ModRow(h, "")
            row.open_detail.connect(self._open_detail)
            self.results_layout.insertWidget(
                self.results_layout.count() - 2, row)
            self._rows.append(row)
            if h.get("icon_url"):
                icons.append((len(self._rows) - 1, h["icon_url"]))
        if icons:
            w = IconBatchWorker(icons)
            self._iw = w
            _track_worker(w)
            w.icon_ready.connect(self._on_icon)
            w.start()
        self._next_btn.setVisible(len(hits) == MOD_PAGE_SIZE)

    def _on_icon(self, idx, data):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_icon_bytes(data)

    def _open_detail(self, pid):
        self.pages.setCurrentWidget(self.detail_page)
        self.detail_page.load(pid)

    def _back_to_browse(self):
        self.pages.setCurrentWidget(self.pages.widget(0))

    def showEvent(self, e):
        self.refresh()
        if not self._ever_loaded:
            self._ever_loaded = True
            self._do_search()
        super().showEvent(e)


# ==========================================================================
# 联机功能（内核：陶瓦联机 Terracotta，基于 EasyTier，AGPL-3.0）
# 第三方启动器通过 HTTP API 与陶瓦联机进程交互（AGPL 例外条款第 2 条允许）。
# 启动参数 --hmcl <file>：集成模式，HTTP 就绪后将 {"port": N} 写入该文件。
# ==========================================================================
TERRACOTTA_REPO = "burningtnt/Terracotta"
TERRACOTTA_HOMEPAGE = "https://github.com/burningtnt/Terracotta"


class TerracottaManager(QObject):
    """管理陶瓦联机（Terracotta）内核子进程的下载、启动、停止与 HTTP 控制。"""

    kernel_status = pyqtSignal(str)     # missing | present | starting | running | stopped | error
    state_changed = pyqtSignal(dict)    # GET /state 的 JSON
    log_line = pyqtSignal(str)
    port_ready = pyqtSignal(int)
    download_progress = pyqtSignal(int, str)
    download_done = pyqtSignal(bool, str)

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.exe_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "terracotta")
        self.exe_path = os.path.join(self.exe_dir, "terracotta.exe")
        self.port_file = os.path.join(self.exe_dir, "port.json")
        self.proc = None
        self.port = None
        self._last_state = None
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------------ 路径
    def is_kernel_present(self):
        return os.path.isfile(self.exe_path)

    @staticmethod
    def _arch_tag():
        m = (platform.machine() or "AMD64").lower()
        if "arm" in m or "aarch" in m:
            return "arm64"
        return "x86_64"

    def _platform_key(self):
        if sys.platform.startswith("win"):
            return "windows-" + self._arch_tag()
        if sys.platform.startswith("linux"):
            return "linux-" + self._arch_tag()
        if sys.platform == "darwin":
            return "macos-" + self._arch_tag()
        return "windows-x86_64"

    # ---------------------------------------------------------- 发行包解析/下载
    def resolve_download_url(self):
        url = (f"https://api.github.com/repos/{TERRACOTTA_REPO}"
               f"/releases/latest")
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT,
                          "Accept": "application/vnd.github+json"})
        d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        tag = d.get("tag_name", "")
        key = self._platform_key()
        plat_kw = ("windows" if sys.platform.startswith("win")
                   else "linux" if sys.platform.startswith("linux")
                   else "macos" if sys.platform == "darwin" else "windows")
        cand = None
        for a in d.get("assets", []):
            n = (a.get("name") or "").lower()
            if key in n and (n.endswith(".tar.gz") or n.endswith(".tgz")
                             or n.endswith(".zip") or n.endswith(".exe")
                             or ".appimage" in n):
                cand = a
                break
        if cand is None:
            for a in d.get("assets", []):
                n = (a.get("name") or "").lower()
                if plat_kw in n:
                    cand = a
                    break
        if cand is None:
            raise RuntimeError("未找到适配当前平台（%s）的陶瓦联机发行包" % key)
        return cand.get("browser_download_url"), tag

    def _do_download(self, emit):
        emit(0, "解析最新发行版…")
        url, tag = self.resolve_download_url()
        emit(5, "正在下载陶瓦联机 %s …" % tag)
        os.makedirs(self.exe_dir, exist_ok=True)
        tmp = os.path.join(self.exe_dir, "tc_download.tmp")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        resp = urllib.request.urlopen(req, timeout=180)
        total = int(resp.headers.get("Content-Length", "0") or "0")
        got = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    emit(int(5 + 90 * got / total),
                         "下载 %d / %d KB" % (got // 1024, total // 1024))
        emit(96, "解压内核…")
        self._extract_kernel(tmp, url)
        try:
            os.remove(tmp)
        except Exception:
            pass
        emit(100, "完成")

    def _extract_kernel(self, tmp, url):
        is_tar = url.endswith(".tar.gz") or url.endswith(".tgz")
        is_zip = url.endswith(".zip")
        if is_tar:
            with tarfile.open(tmp, "r:gz") as t:
                for m in t.getmembers():
                    self._write_member(m.name,
                                       t.extractfile(m).read())
        elif is_zip:
            with zipfile.ZipFile(tmp) as z:
                for info in z.infolist():
                    self._write_member(info.filename,
                                       z.read(info))
        else:
            # 直接是可执行文件
            with open(tmp, "rb") as f:
                data = f.read()
            with open(self.exe_path, "wb") as out:
                out.write(data)

    def _write_member(self, name, data):
        low = name.lower()
        base = os.path.basename(name)
        if low.endswith(".exe"):
            with open(self.exe_path, "wb") as out:
                out.write(data)
        elif low.endswith(".dll") or low.endswith(".so") \
                or low.endswith(".dylib"):
            dst = os.path.join(self.exe_dir, base)
            with open(dst, "wb") as out:
                out.write(data)

    # -------------------------------------------------------------- 子进程控制
    def _read_existing_port(self):
        try:
            with open(self.port_file, "r", encoding="utf-8") as f:
                return int(json.load(f).get("port", 0) or 0)
        except Exception:
            return 0

    def _reachable(self, port):
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d/state" % port, timeout=2).read()
            return True
        except Exception:
            return False

    def auto_attach(self):
        """若内核已在运行（端口文件存在且可达），则直接接管。"""
        port = self._read_existing_port()
        if port and self._reachable(port):
            self.port = port
            self._timer.start()
            self.kernel_status.emit("running")
            return True
        return False

    def _do_start(self):
        if not self.is_kernel_present():
            raise RuntimeError("陶瓦联机内核未安装，请先下载。")
        port = self._read_existing_port()
        if port and self._reachable(port):
            self.port = port
            self._timer.start()
            self.kernel_status.emit("running")
            return port
        self.kernel_status.emit("starting")
        args = [self.exe_path, "--hmcl", self.port_file]
        cf = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=cf, close_fds=True)
        for _ in range(150):          # 最多等 15 秒
            if os.path.isfile(self.port_file):
                try:
                    with open(self.port_file, "r", encoding="utf-8") as f:
                        j = json.load(f)
                    port = int(j.get("port", 0) or 0)
                    if port and self._reachable(port):
                        self.port = port
                        self._timer.start()
                        self.kernel_status.emit("running")
                        self.port_ready.emit(port)
                        return port
                except Exception:
                    pass
            time.sleep(0.1)
        raise RuntimeError("陶瓦联机内核启动超时。虚拟网卡需要管理员权限；"
                           "请尝试以管理员身份运行启动器，或查看陶瓦联机日志。")

    def stop(self):
        if self.port:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/panic?peaceful=true" % self.port,
                    timeout=5).read()
            except Exception:
                pass
        self._timer.stop()
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        try:
            if os.path.isfile(self.port_file):
                os.remove(self.port_file)
        except Exception:
            pass
        self.port = None
        self.kernel_status.emit("stopped")

    # ---------------------------------------------------------------- HTTP 控制
    def _get_json(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())

    def _poll(self):
        if not self.port:
            return
        try:
            st = self._get_json("http://127.0.0.1:%d/state" % self.port)
            self._last_state = st
            self.state_changed.emit(st)
        except Exception as e:
            self.log_line.emit("状态轮询失败：" + str(e))

    def refresh_state(self):
        if not self.port:
            return
        try:
            st = self._get_json("http://127.0.0.1:%d/state" % self.port)
            self._last_state = st
            self.state_changed.emit(st)
        except Exception as e:
            self.log_line.emit("刷新状态失败：" + str(e))

    def host_create(self, player="", room=""):
        if not self.port:
            raise RuntimeError("内核未运行。")
        q = {}
        if player:
            q["player"] = player
        if room:
            q["room"] = room
        url = ("http://127.0.0.1:%d/state/scanning" % self.port
               + ("?" + urllib.parse.urlencode(q) if q else ""))
        urllib.request.urlopen(url, timeout=10).read()
        self.refresh_state()

    def guest_join(self, room, player=""):
        if not self.port:
            raise RuntimeError("内核未运行。")
        room = (room or "").strip()
        if not room:
            raise RuntimeError("请填写房间码。")
        q = {"room": room}
        if player:
            q["player"] = player
        url = ("http://127.0.0.1:%d/state/guesting?%s"
               % (self.port, urllib.parse.urlencode(q)))
        try:
            urllib.request.urlopen(url, timeout=10).read()
        except urllib.error.HTTPError as e:
            if e.code == 400:
                raise RuntimeError("房间码无效或格式错误（应为 U/XXXX-XXXX-XXXX-XXXX）")
            raise
        self.refresh_state()

    def reset(self):
        if not self.port:
            return
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d/state/ide" % self.port, timeout=10).read()
        except Exception:
            pass
        self.refresh_state()


class _TerracottaDownloadWorker(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(bool, str)

    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr

    def run(self):
        try:
            self.mgr._do_download(self.progress.emit)
            self.done.emit(True, "陶瓦联机内核已就绪，可点击『启动内核』。")
        except Exception as e:
            self.done.emit(False, str(e))


class _TerracottaStartWorker(QThread):
    ready = pyqtSignal(int)
    error = pyqtSignal(str)

    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr

    def run(self):
        try:
            self.ready.emit(self.mgr._do_start())
        except Exception as e:
            self.error.emit(str(e))


_TC_STATE_CN = {
    "waiting": "待机",
    "host-scanning": "房主：正在扫描本机 Minecraft 端口…",
    "host-starting": "房主：正在创建房间…",
    "host-ok": "房主：房间已就绪 ✓",
    "guest-connecting": "访客：正在连接…",
    "guest-starting": "访客：正在建立连接…",
    "guest-ok": "访客：已连接 ✓ 打开 MC 即可看到房间",
    "exception": "异常",
}


class MultiplayerPage(QWidget):
    """联机页：下载/启动陶瓦联机内核，创建或加入房间，实时显示状态。"""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.mgr = TerracottaManager(main.core)
        self._running = False
        self._current_room_code = None
        self._build_ui()
        self._wire_mgr()
        self._refresh_kernel_status()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QLabel("联机 · 陶瓦联机")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # ---- 主面板 ----
        main_w = QWidget()
        mv = QVBoxLayout(main_w)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(12)

        # 内核状态卡
        kcard = make_card()
        kl = QVBoxLayout(kcard)
        kl.setContentsMargins(16, 14, 16, 14)
        kl.setSpacing(8)
        kh = QLabel("陶瓦联机内核")
        kh.setStyleSheet("font-size:15px;font-weight:bold;")
        kl.addWidget(kh)
        self.kernel_status_lbl = QLabel("检测中…")
        self.kernel_status_lbl.setObjectName("HintText")
        kl.addWidget(self.kernel_status_lbl)

        krow = QHBoxLayout()
        krow.setSpacing(8)
        self.download_btn = make_primary("下载内核")
        self.download_btn.setMaximumWidth(160)
        self.download_btn.clicked.connect(self._download)
        self.start_btn = make_primary("启动内核")
        self.start_btn.setMaximumWidth(160)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = make_ghost("停止内核")
        self.stop_btn.setMaximumWidth(160)
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        krow.addWidget(self.download_btn)
        krow.addWidget(self.start_btn)
        krow.addWidget(self.stop_btn)
        krow.addStretch(1)
        kl.addLayout(krow)

        self.dl_bar = QProgressBar()
        self.dl_bar.setMaximum(100)
        self.dl_bar.setVisible(False)
        kl.addWidget(self.dl_bar)

        attr = QLabel("内核：Terracotta | 陶瓦联机（burningtnt），基于 EasyTier，"
                      "AGPL-3.0。本启动器通过 HTTP API 与之交互。"
                      " <a href='%s'>项目主页</a>" % TERRACOTTA_HOMEPAGE)
        attr.setObjectName("HintText")
        attr.setOpenExternalLinks(True)
        attr.setWordWrap(True)
        kl.addWidget(attr)
        mv.addWidget(kcard)

        # 房间卡
        rcard = make_card()
        rl = QVBoxLayout(rcard)
        rl.setContentsMargins(16, 14, 16, 14)
        rl.setSpacing(10)

        rtabs = QButtonGroup(self)
        rtab_row = QHBoxLayout()
        rtab_row.setSpacing(8)
        self.tab_host = make_ghost("创建房间（房主）")
        self.tab_guest = make_ghost("加入房间（访客）")
        rtabs.addButton(self.tab_host, 0)
        rtabs.addButton(self.tab_guest, 1)
        self.tab_host.setCheckable(True)
        self.tab_guest.setCheckable(True)
        self.tab_host.setChecked(True)
        self.tab_host.clicked.connect(lambda: self._switch_tab(0))
        self.tab_guest.clicked.connect(lambda: self._switch_tab(1))
        rtabs.buttonClicked.connect(lambda b: self._sync_tab(b))
        rtab_row.addWidget(self.tab_host)
        rtab_row.addWidget(self.tab_guest)
        rtab_row.addStretch(1)
        rl.addLayout(rtab_row)

        self.room_stack = QStackedWidget()
        rl.addWidget(self.room_stack, 1)

        # 房主面板
        host_w = QWidget()
        hv = QVBoxLayout(host_w)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(8)
        hhl = QLabel("开始后请到 Minecraft 中『对局域网开放世界』，"
                     "好友输入下方房间码即可加入。")
        hhl.setObjectName("HintText")
        hhl.setWordWrap(True)
        hv.addWidget(hhl)
        hn = QHBoxLayout()
        hn.setSpacing(8)
        hn.addWidget(QLabel("房主昵称"))
        self.host_name = QLineEdit()
        self.host_name.setObjectName("SearchEdit")
        self.host_name.setPlaceholderText("可选，留空则随机")
        hn.addWidget(self.host_name, 1)
        hv.addLayout(hn)
        self.create_btn = make_primary("开始创建房间")
        self.create_btn.setMaximumWidth(220)
        self.create_btn.clicked.connect(self._create_room)
        hv.addWidget(self.create_btn)
        self.room_code_box = QHBoxLayout()
        self.room_code_lbl = QLabel("房间码：—")
        self.room_code_lbl.setStyleSheet(
            "font-size:18px;font-weight:bold;font-family:Consolas,monospace;")
        self.copy_btn = make_ghost("复制")
        self.copy_btn.setMaximumWidth(90)
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_code)
        self.room_code_box.addWidget(self.room_code_lbl)
        self.room_code_box.addWidget(self.copy_btn)
        self.room_code_box.addStretch(1)
        hv.addLayout(self.room_code_box)
        hv.addStretch(1)
        self.room_stack.addWidget(host_w)

        # 访客面板
        guest_w = QWidget()
        gv = QVBoxLayout(guest_w)
        gv.setContentsMargins(0, 0, 0, 0)
        gv.setSpacing(8)
        ghl = QLabel("输入好友分享的房间码，连接成功后打开 Minecraft 的『多人游戏』"
                     "即可在局域网列表中看到对方的房间。")
        ghl.setObjectName("HintText")
        ghl.setWordWrap(True)
        gv.addWidget(ghl)
        gn = QHBoxLayout()
        gn.setSpacing(8)
        gn.addWidget(QLabel("房间码"))
        self.guest_code = QLineEdit()
        self.guest_code.setObjectName("SearchEdit")
        self.guest_code.setPlaceholderText("U/XXXX-XXXX-XXXX-XXXX")
        gn.addWidget(self.guest_code, 1)
        gv.addLayout(gn)
        gn2 = QHBoxLayout()
        gn2.setSpacing(8)
        gn2.addWidget(QLabel("昵称"))
        self.guest_name = QLineEdit()
        self.guest_name.setObjectName("SearchEdit")
        self.guest_name.setPlaceholderText("可选")
        gn2.addWidget(self.guest_name, 1)
        gv.addLayout(gn2)
        gj = QHBoxLayout()
        gj.setSpacing(8)
        self.join_btn = make_primary("加入房间")
        self.join_btn.setMaximumWidth(180)
        self.join_btn.clicked.connect(self._join_room)
        self.leave_btn = make_ghost("退出房间")
        self.leave_btn.setMaximumWidth(180)
        self.leave_btn.clicked.connect(self._leave_room)
        gj.addWidget(self.join_btn)
        gj.addWidget(self.leave_btn)
        gj.addStretch(1)
        gv.addLayout(gj)
        gv.addStretch(1)
        self.room_stack.addWidget(guest_w)

        mv.addWidget(rcard)

        # 状态 + 日志
        scard = make_card()
        sl = QVBoxLayout(scard)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(8)
        sl.addWidget(QLabel("连接状态"))
        self.state_lbl = QLabel("—")
        self.state_lbl.setWordWrap(True)
        sl.addWidget(self.state_lbl)
        sl.addWidget(QLabel("日志"))
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(150)
        self.log_edit.setObjectName("SearchEdit")
        sl.addWidget(self.log_edit)
        mv.addWidget(scard)

        self.stack.addWidget(main_w)   # 0 主面板

        # 内核未安装时的引导提示由状态卡覆盖，无需单独页面

    def _wire_mgr(self):
        self.mgr.kernel_status.connect(self._on_kernel_status)
        self.mgr.state_changed.connect(self._on_state)
        self.mgr.log_line.connect(self._append_log)
        self.mgr.port_ready.connect(lambda p: self._append_log("内核已就绪，端口 %d" % p))
        self.mgr.download_progress.connect(
            lambda p, t: (self.dl_bar.setValue(p), self.dl_bar.setFormat(t)))
        self.mgr.download_done.connect(self._on_download_done)

    def _refresh_kernel_status(self):
        if self.mgr.is_kernel_present():
            if not self.mgr.auto_attach():
                self.mgr.kernel_status.emit("present")
        else:
            self.mgr.kernel_status.emit("missing")

    def _on_kernel_status(self, s):
        self._running = (s == "running")
        map_txt = {
            "missing": "未安装内核。请点击『下载内核』（约 8 MB）。",
            "present": "内核已就绪，点击『启动内核』开始联机。",
            "starting": "正在启动内核…",
            "running": "内核运行中。",
            "stopped": "内核已停止。",
            "error": "内核出错。",
        }
        self.kernel_status_lbl.setText(map_txt.get(s, s))
        self.download_btn.setText("重新下载" if s in ("present", "error") else "下载内核")
        self.start_btn.setEnabled(s in ("present", "stopped", "error"))
        self.stop_btn.setEnabled(s == "running")
        self._set_room_enabled(self._running)
        if s == "running":
            self.mgr.refresh_state()

    def _set_room_enabled(self, on):
        for w in (self.create_btn, self.join_btn, self.leave_btn,
                  self.host_name, self.guest_code, self.guest_name):
            w.setEnabled(on)
        if not on:
            self.room_code_lbl.setText("房间码：—")
            self.copy_btn.setEnabled(False)
            self.state_lbl.setText("—")

    def _switch_tab(self, i):
        self.room_stack.setCurrentIndex(i)

    def _sync_tab(self, btn):
        self.room_stack.setCurrentWidget(
            self.room_stack.widget(0) if btn is self.tab_host
            else self.room_stack.widget(1))

    def _download(self):
        self.dl_bar.setVisible(True)
        self.dl_bar.setValue(0)
        self.download_btn.setEnabled(False)
        w = _TerracottaDownloadWorker(self.mgr)
        self._dw = w
        _track_worker(w)
        w.done.connect(self._on_download_done)
        w.start()

    def _on_download_done(self, ok, msg):
        self.dl_bar.setVisible(False)
        self.download_btn.setEnabled(True)
        self._append_log(msg)
        if ok:
            self.mgr.kernel_status.emit("present")
        else:
            self.mgr.kernel_status.emit("error")
            QMessageBox.warning(self, "下载失败", msg)

    def _start(self):
        self.start_btn.setEnabled(False)
        w = _TerracottaStartWorker(self.mgr)
        self._sw = w
        _track_worker(w)
        w.ready.connect(lambda p: self._append_log("内核已启动，端口 %d" % p))
        w.error.connect(lambda e: (self._append_log("启动失败：" + e),
                                   QMessageBox.warning(self, "启动失败", e),
                                   self.mgr.kernel_status.emit("error")))
        w.start()

    def _stop(self):
        self.mgr.stop()
        self._append_log("已发送停止指令。")

    def _create_room(self):
        try:
            self.mgr.host_create(player=self.host_name.text().strip())
            self._append_log("已请求创建房间，请到 MC 开局域网世界。")
        except Exception as e:
            self._append_log("创建失败：" + str(e))
            QMessageBox.warning(self, "提示", str(e))

    def _join_room(self):
        try:
            self.mgr.guest_join(self.guest_code.text(),
                                player=self.guest_name.text().strip())
            self._append_log("已请求加入房间。")
        except Exception as e:
            self._append_log("加入失败：" + str(e))
            QMessageBox.warning(self, "提示", str(e))

    def _leave_room(self):
        self.mgr.reset()
        self._append_log("已退出房间。")

    def _copy_code(self):
        code = self._current_room_code
        if code:
            QApplication.clipboard().setText(code)

    def _on_state(self, st):
        s = st.get("state", "waiting")
        txt = _TC_STATE_CN.get(s, s)
        diff = st.get("difficulty")
        if diff and s == "guest-starting":
            txt += "（NAT 难度：%s）" % diff
        self.state_lbl.setText(txt)
        room = st.get("room")
        if room and s in ("host-starting", "host-ok"):
            self._current_room_code = room
            self.room_code_lbl.setText("房间码：" + room)
            self.copy_btn.setEnabled(True)
            self.tab_host.setChecked(True)
            self._switch_tab(0)
        if s == "guest-ok":
            self._append_log("连接成功，地址：%s" % st.get("url", "127.0.0.1"))

    def _append_log(self, line):
        self.log_edit.append(line)

    def refresh(self):
        self._refresh_kernel_status()

    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)


# ==========================================================================
# 个性化页（仿 PCL2「个性化」）：主题强调色 / 窗口不透明度 / 背景图片 /
# 背景音乐 / 标题栏 / 功能隐藏。所有改动即时生效并写入配置。
# ==========================================================================
class PersonalizationPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self._slider_registry = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)
        head = QLabel("个性化")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(14)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        cv.addWidget(self._basics_card())
        cv.addWidget(self._bg_card())
        cv.addWidget(self._music_card())
        cv.addWidget(self._title_card())
        cv.addWidget(self._hide_card())
        cv.addStretch(1)

    # ---------- 基础：主题色 / 不透明度 ----------
    def _basics_card(self):
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(14)
        box.addWidget(self._section_title("基础"))

        # 主题强调色
        lbl = QLabel("主题强调色（导航 / 标题栏 / 按钮渐变）")
        lbl.setObjectName("HintText")
        box.addWidget(lbl)

        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(10)
        self._swatches = []
        for name, hexc in ACCENT_PRESETS:
            b = QPushButton()
            b.setFixedSize(34, 34)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(name)
            b.setStyleSheet(
                f"background:{hexc};border-radius:8px;border:2px solid transparent;")
            b.clicked.connect(lambda _, h=hexc: self._pick_accent(h))
            swatch_row.addWidget(b)
            self._swatches.append((hexc, b))
        custom = make_ghost("自定义…")
        custom.clicked.connect(self._pick_custom_accent)
        swatch_row.addWidget(custom)
        reset = make_ghost("恢复默认")
        reset.clicked.connect(lambda: self._pick_accent(""))
        swatch_row.addWidget(reset)
        swatch_row.addStretch(1)
        box.addLayout(swatch_row)
        self._accent_hint = QLabel()
        self._accent_hint.setObjectName("HintText")
        box.addWidget(self._accent_hint)
        self._refresh_accent_hint()

        # 窗口不透明度
        op = self._slider_row(
            "window_opacity", "窗口不透明度", 40, 100,
            int(float(self.main.config.get("window_opacity", 1.0)) * 100),
            "{}%")
        op.slider.valueChanged.connect(self._on_window_opacity)
        box.addLayout(op.layout)

        box.addWidget(self._tip_label(
            "提示：调低不透明度可让启动器半透明叠加在桌面之上。"))
        return card

    def _pick_accent(self, hexc):
        self.main.set_accent(hexc)
        self.main.save_config()
        self._refresh_accent_hint()

    def _pick_custom_accent(self):
        cur = self.main.config.get("accent") or "#4C6EF5"
        c = QColorDialog.getColor(QColor(cur), self, "选择主题强调色")
        if c.isValid():
            self._pick_accent(c.name())

    def _refresh_accent_hint(self):
        a = self.main.config.get("accent") or ""
        self._accent_hint.setText(
            "当前：跟随主题默认" if not a else f"当前强调色：{a}")
        for hexc, b in self._swatches:
            sel = (a == hexc)
            b.setStyleSheet(
                f"background:{hexc};border-radius:8px;"
                f"border:2px solid {'#222' if sel else 'transparent'};")

    def _on_window_opacity(self, v):
        self.main.config["window_opacity"] = v / 100.0
        self.main.apply_window_opacity()
        self.main.save_config()
        self._set_slider_label("window_opacity", f"{v}%")

    # ---------- 背景图片 ----------
    def _bg_card(self):
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(14)
        box.addWidget(self._section_title("背景图片"))

        self._bg_status = QLabel()
        self._bg_status.setObjectName("HintText")
        box.addWidget(self._bg_status)
        self._refresh_bg_status()

        fit = QComboBox()
        for k, t in [("auto", "自动"), ("stretch", "拉伸"),
                     ("center", "居中"), ("tile", "平铺")]:
            fit.addItem(t, k)
        ci = fit.findData(self.main.config.get("bg_fit", "auto"))
        if ci >= 0:
            fit.setCurrentIndex(ci)
        fit.setMinimumHeight(36)
        fit.currentTextChanged.connect(
            lambda _: (self.main.config.__setitem__("bg_fit", fit.currentData()),
                      self.main.save_config(), self.main.apply_bg_settings()))
        box.addLayout(self._field("自适应方式", fit))

        op = self._slider_row(
            "bg_opacity", "不透明度", 0, 100,
            int(float(self.main.config.get("bg_opacity", 1.0)) * 100), "{}%")
        op.slider.valueChanged.connect(self._on_bg_opacity)
        box.addLayout(op.layout)

        blur = self._slider_row(
            "bg_blur", "背景模糊", 0, 30, int(self.main.config.get("bg_blur", 0) or 0),
            "{}")
        blur.slider.valueChanged.connect(self._on_bg_blur)
        box.addLayout(blur.layout)

        tint = QPushButton()
        tint.setObjectName("GhostButton")
        tint.setCheckable(True)
        tint.setChecked(bool(self.main.config.get("bg_tint", False)))
        tint.setText("叠加彩色背景（用强调色淡化）")
        tint.clicked.connect(self._on_bg_tint)
        box.addWidget(tint)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        b_open = make_ghost("打开文件夹")
        b_open.clicked.connect(self._open_bg_folder)
        b_refresh = make_ghost("刷新背景图片")
        b_refresh.clicked.connect(self._refresh_bg_image)
        b_clear = make_ghost("清空背景图片")
        b_clear.clicked.connect(self._clear_bg_image)
        btns.addWidget(b_open)
        btns.addWidget(b_refresh)
        btns.addWidget(b_clear)
        btns.addStretch(1)
        box.addLayout(btns)
        box.addWidget(self._tip_label(
            "把图片放进背景文件夹（支持 png/jpg/webp 等），启动器取第一张作为壁纸；"
            "模糊 / 不透明度 / 叠加色即时预览。"))
        return card

    def _refresh_bg_status(self):
        d = self.main.config.get("bg_image_dir") or BG_DIR
        n = 0
        if os.path.isdir(d):
            exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
            n = sum(1 for f in os.listdir(d) if f.lower().endswith(exts))
        self._bg_status.setText(f"背景图片文件夹：{d}（共 {n} 张）")

    def _on_bg_opacity(self, v):
        self.main.config["bg_opacity"] = v / 100.0
        self.main.save_config()
        self.main.apply_bg_settings()
        self._set_slider_label("bg_opacity", f"{v}%")

    def _on_bg_blur(self, v):
        self.main.config["bg_blur"] = v
        self.main.save_config()
        self.main.apply_bg_settings()
        self._set_slider_label("bg_blur", f"{v}")

    def _on_bg_tint(self):
        self.main.config["bg_tint"] = not self.main.config.get("bg_tint", False)
        self.main.save_config()
        self.main.apply_bg_settings()

    def _open_bg_folder(self):
        d = self.main.config.get("bg_image_dir") or BG_DIR
        os.makedirs(d, exist_ok=True)
        if os.name == "nt":
            os.startfile(d)
        else:
            subprocess.run(["xdg-open", d], creationflags=_NO_WINDOW)

    def _refresh_bg_image(self):
        self.main._load_bg_from_config()
        self.main.apply_bg_settings()
        self._refresh_bg_status()

    def _clear_bg_image(self):
        d = self.main.config.get("bg_image_dir") or BG_DIR
        if not os.path.isdir(d):
            return
        exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
        files = [os.path.join(d, f) for f in os.listdir(d)
                 if f.lower().endswith(exts)]
        if not files:
            QMessageBox.information(self, "提示", "背景文件夹里还没有图片。")
            return
        ans = QMessageBox.question(
            self, "清空背景图片",
            f"确定删除背景文件夹里的 {len(files)} 张图片？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        for f in files:
            try:
                os.remove(f)
            except Exception:
                pass
        self.main._load_bg_from_config()
        self.main.apply_bg_settings()
        self._refresh_bg_status()

    # ---------- 背景音乐 ----------
    def _music_card(self):
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(14)
        box.addWidget(self._section_title("背景音乐"))

        self._music_status = QLabel()
        self._music_status.setObjectName("HintText")
        box.addWidget(self._music_status)
        self._refresh_music_status()

        auto = QPushButton()
        auto.setObjectName("GhostButton")
        auto.setCheckable(True)
        auto.setChecked(bool(self.main.config.get("music_autoplay", False)))
        auto.setText("启动器启动时自动播放")
        auto.clicked.connect(self._on_music_auto)
        box.addWidget(auto)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        b_open = make_ghost("打开文件夹")
        b_open.clicked.connect(self._open_music_folder)
        b_refresh = make_ghost("刷新背景音乐")
        b_refresh.clicked.connect(self._refresh_music)
        b_play = make_ghost("试听")
        b_play.clicked.connect(self._toggle_play)
        self._play_btn = b_play
        btns.addWidget(b_open)
        btns.addWidget(b_refresh)
        btns.addWidget(b_play)
        btns.addStretch(1)
        box.addLayout(btns)
        return card

    def _refresh_music_status(self):
        d = self.main.config.get("music_dir") or MUSIC_DIR
        n = len(self.main._scan_music())
        self._music_status.setText(f"背景音乐文件夹：{d}（共 {n} 首）")

    def _on_music_auto(self):
        self.main.config["music_autoplay"] = \
            not self.main.config.get("music_autoplay", False)
        self.main.save_config()

    def _open_music_folder(self):
        d = self.main.config.get("music_dir") or MUSIC_DIR
        os.makedirs(d, exist_ok=True)
        if os.name == "nt":
            os.startfile(d)
        else:
            subprocess.run(["xdg-open", d], creationflags=_NO_WINDOW)

    def _refresh_music(self):
        self.main._scan_music()
        self._refresh_music_status()

    def _toggle_play(self):
        if getattr(self.main, "_music_player", None) and \
                self.main._music_player.isPlaying():
            self.main.stop_music()
            self._play_btn.setText("试听")
        else:
            self.main.start_music()
            self._play_btn.setText("停止")

    # ---------- 标题栏 ----------
    def _title_card(self):
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(14)
        box.addWidget(self._section_title("标题栏"))

        mode = self.main.config.get("title_mode", "default")
        group = QButtonGroup(self)
        group.setExclusive(True)
        self._title_radios = {}
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        for key, txt in [("none", "无"), ("default", "默认"),
                         ("text", "文本"), ("image", "图片")]:
            r = QPushButton(txt)
            r.setObjectName("GhostButton")
            r.setCheckable(True)
            r.setChecked(mode == key)
            r.clicked.connect(lambda _, k=key: self._on_title_mode(k))
            group.addButton(r)
            mode_row.addWidget(r)
            self._title_radios[key] = r
        mode_row.addStretch(1)
        box.addLayout(self._field("显示方式", mode_row))

        self._title_text_edit = QLineEdit(
            self.main.config.get("title_text", ""))
        self._title_text_edit.setObjectName("LineEdit")
        self._title_text_edit.setMinimumHeight(34)
        self._title_text_edit.setPlaceholderText("自定义标题栏文字")
        self._title_text_edit.textChanged.connect(self._on_title_text)
        box.addLayout(self._field("标题栏文本", self._title_text_edit))

        b_logo = make_ghost("选择标题栏图片…")
        b_logo.clicked.connect(self._choose_logo)
        box.addLayout(self._field("标题栏图片（选『图片』模式生效）", b_logo))
        return card

    def _on_title_mode(self, key):
        self.main.config["title_mode"] = key
        self.main.save_config()
        self.main.apply_title()

    def _on_title_text(self, text):
        self.main.config["title_text"] = text
        self.main.save_config()
        self.main.apply_title()

    def _choose_logo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择标题栏图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.main.config["title_logo"] = path
            self.main.config["title_mode"] = "image"
            self.main.save_config()
            self.main.apply_title()
            for k, r in self._title_radios.items():
                r.setChecked(k == "image")

    # ---------- 功能隐藏 ----------
    def _hide_card(self):
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(12)
        box.addWidget(self._section_title("功能隐藏"))

        # 与侧边栏导航对应的页面（不含「启动」与「个性化」本身）
        items = [("下载", 1), ("MOD", 2), ("整合包", 6), ("数据包", 7),
                 ("资源包", 8), ("光影包", 9), ("联机", 10), ("账号", 3),
                 ("设置", 4), ("关于", 5)]
        hidden = set(self.main.config.get("hidden_pages", []) or [])
        for name, idx in items:
            chk = QPushButton()
            chk.setObjectName("GhostButton")
            chk.setCheckable(True)
            chk.setChecked(idx in hidden)
            chk.setText(f"隐藏「{name}」")
            chk.clicked.connect(
                lambda _, i=idx, c=chk: self._on_hide_toggle(i, c))
            box.addWidget(chk)
        box.addWidget(self._tip_label(
            "被隐藏的功能仍可通过按 F12 临时显示，再按一次恢复隐藏。"))
        return card

    def _on_hide_toggle(self, idx, chk):
        hidden = set(self.main.config.get("hidden_pages", []) or [])
        if chk.isChecked():
            hidden.add(idx)
        else:
            hidden.discard(idx)
        self.main.config["hidden_pages"] = sorted(hidden)
        self.main.save_config()
        self.main.apply_hidden_pages()

    # ---------- 小工具 ----------
    def _section_title(self, text):
        l = QLabel(text)
        l.setObjectName("SubTitle")
        l.setStyleSheet("font-size:14px;font-weight:bold;")
        return l

    def _tip_label(self, text):
        l = QLabel(text)
        l.setObjectName("HintText")
        l.setWordWrap(True)
        return l

    def _field(self, label, widget):
        b = QVBoxLayout()
        b.setSpacing(6)
        l = QLabel(label)
        l.setObjectName("HintText")
        b.addWidget(l)
        if isinstance(widget, QLayout):
            b.addLayout(widget)
        else:
            b.addWidget(widget)
        return b

    def _slider_row(self, key, label, lo, hi, val, fmt):
        b = QVBoxLayout()
        b.setSpacing(6)
        head = QHBoxLayout()
        l = QLabel(label)
        l.setObjectName("HintText")
        head.addWidget(l)
        val_lbl = QLabel(fmt.format(val))
        val_lbl.setObjectName("HintText")
        head.addStretch(1)
        head.addWidget(val_lbl)
        b.addLayout(head)
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setValue(val)
        b.addWidget(s)
        b.layout = b
        b.slider = s
        b.label = val_lbl
        b.fmt = fmt
        self._slider_registry[key] = b
        return b

    def _set_slider_label(self, key, text):
        b = self._slider_registry.get(key)
        if b is not None:
            b.label.setText(text)


# ==========================================================================
# 种子地图查找器
# ==========================================================================
# cubiomes 的 C 调用无法在「Qt 事件循环运行期间」的任意线程中执行（会死锁），
# 因此地图生成放到独立子进程中进行（子进程内无 Qt，可安全并行）。
import concurrent.futures as _cf

_EXEC = None


def _init_executor():
    global _EXEC
    if _EXEC is None:
        try:
            _EXEC = _cf.ProcessPoolExecutor(
                max_workers=1, initializer=_child_init)
        except Exception:
            _EXEC = None
    return _EXEC


def _child_init():
    # 子进程内无 Qt；但 cubiomes 的 get_biome_at 并非线程安全，
    # 多线程并发会崩溃/死锁，故子进程内也强制单线程（保证正确）。
    try:
        global _MAX_THREADS
        _MAX_THREADS = 1
    except Exception:
        pass


class _MapWorker(QThread):
    # qimg, cx, cz, img_w, img_h, world_bpp
    preview = pyqtSignal(object, int, int, int, int, float)
    # qimg, cx, cz, img_w, img_h, world_bpp, bid_grid, spawn, markers
    done = pyqtSignal(object, int, int, int, int, float, object, object, object)

    def __init__(self, seed, dim, cx, cz, w, h, bpp, version="1.20.1",
                 visible=None):
        super().__init__()
        self.seed = seed
        self.dim = dim
        self.cx = cx
        self.cz = cz
        self.w = w
        self.h = h
        self.bpp = bpp
        self.version = version
        self.visible = list(visible or [])

    def run(self):
        ex = _init_executor()
        if ex is None:
            return
        try:
            # 执行器（及其子进程）由 _start_worker 在【主线程】创建，这里只复用。
            # 所有 cubiomes C 调用都在子进程内完成；主线程只负责渲染。
            # 1) 低分辨率快速预览（先让画面动起来）
            pw, ph = max(1, self.w // 4), max(1, self.h // 4)
            prev_rgb, _ = ex.submit(generate_biome_rgb, self.seed, self.cx,
                                    self.cz, pw, ph, self.bpp * 4, self.dim,
                                    self.version, 2500).result(timeout=180)
            q0 = QImage(prev_rgb.tobytes(), pw, ph, pw * 3,
                        QImage.Format.Format_RGB888).copy()
            self.preview.emit(q0, self.cx, self.cz, pw, ph, float(self.bpp * 4))

            # 2) 全分辨率地图 + 生物群系 id 网格（cubiomes 精确还原）
            full_rgb, bid_grid = ex.submit(generate_biome_rgb, self.seed,
                                           self.cx, self.cz, self.w, self.h,
                                           self.bpp, self.dim, self.version,
                                           16000).result(timeout=180)

            # 3) 出生点（cubiomes 在子进程内计算，主线程安全）
            spawn = None
            if self.dim == "overworld":
                try:
                    spawn = find_spawn(self.seed, version=self.version)
                except Exception:
                    spawn = None

            # 4) 结构标记（子进程内计算；仅算当前打开的结构开关，
            #    主线程再按视野裁剪。要塞已按种子缓存，重复重绘近乎零开销。）
            markers = []
            bpp = self.bpp
            w, h = self.w, self.h
            half_w = (w / 2.0) * bpp
            half_h = (h / 2.0) * bpp
            min_cx = int((self.cx - half_w) // 16) - 1
            max_cx = int((self.cx + half_w) // 16) + 1
            min_cz = int((self.cz - half_h) // 16) - 1
            max_cz = int((self.cz + half_h) // 16) + 1
            visible = set(self.visible)
            for name in visible:
                if name == "stronghold":
                    if self.dim != "overworld":
                        continue
                    cfg = STRUCTURES["stronghold"]
                    for (bx, bz) in stronghold_positions(self.seed,
                                                            version=self.version):
                        markers.append({"name": cfg["name"], "color": cfg["color"],
                                        "bx": bx, "bz": bz, "kind": "stronghold"})
                else:
                    info = STRUCT_TO_CUBIOMES.get(name)
                    if info is None or info["dim"] != self.dim:
                        continue
                    pts = structure_positions(self.seed, name, min_cx, max_cx,
                                                  min_cz, max_cz,
                                                  version=self.version)
                    cfg = STRUCTURES.get(name, {})
                    for (bx, bz) in pts:
                        markers.append({"name": cfg.get("name", name),
                                        "color": cfg.get("color", (255, 255, 255)),
                                        "bx": bx, "bz": bz, "kind": name})

            q1 = QImage(full_rgb.tobytes(), self.w, self.h, self.w * 3,
                        QImage.Format.Format_RGB888).copy()
            self.done.emit(q1, self.cx, self.cz, self.w, self.h, float(self.bpp),
                           bid_grid, spawn, markers)
        except Exception:
            pass


class _SeedSearchWorker(QThread):
    """在 cubiomes 子进程内扫描种子（MinecraftSearch）。

    本线程只负责把任务提交给 ProcessPoolExecutor 并等待结果；真正的
    cubiomes C 调用发生在子进程里（QThread 内直接调用会死锁）。
    """
    done = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, criterion, version, max_tries):
        super().__init__()
        self.criterion = criterion
        self.version = version
        self.max_tries = max_tries

    def run(self):
        ex = _init_executor()
        if ex is None:
            self.error.emit("子进程执行器不可用（cubiomes 未就绪）")
            return
        try:
            res = ex.submit(find_seeds, self.criterion, self.version,
                            self.max_tries).result(timeout=300)
            self.done.emit(list(res) if res else [])
        except Exception as e:
            self.error.emit(str(e)[:200])


class _SearchNearbyWorker(QThread):
    """在 cubiomes 子进程内按群系扫描附近坐标（避免主线程调用 cubiomes 死锁）。"""
    done = pyqtSignal(list)

    def __init__(self, seed, biome_cn, cx, cz, radius, step, dim, version):
        super().__init__()
        self.args = (seed, biome_cn, cx, cz, radius, step, dim, version)

    def run(self):
        ex = _init_executor()
        if ex is None:
            self.done.emit([])
            return
        try:
            found = ex.submit(search_biome_near, *self.args).result(timeout=120)
        except Exception:
            found = []
        self.done.emit(found if found else [])


class SeedMapCanvas(QWidget):
    markerClicked = pyqtSignal(dict)

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.setMinimumSize(360, 260)
        self.setMouseTracking(True)
        self._img = None
        self._img_meta = None       # {seed,dim,cx,cz,img_w,img_h,world_bpp}
        self._bid_grid = None       # 与地图同分辨率的生物群系 id 网格（悬停查名用）
        self._bid_grid_meta = None  # {cx,cz,img_w,img_h,world_bpp}
        self._cx = 0
        self._cz = 0
        self._pending = False
        self._hover = None          # (bx, bz) 方块坐标
        self._markers = []          # [{name,color,bx,bz,kind}]
        self._popup = None
        self._spawn_visible = True
        self._recenter_pending = False
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._do_regenerate)
        self._worker = None
        self._desired_job = None    # 当前期望生成的视图参数
        self._last_job = None       # 最近一次完整生成完成的参数
        self._drag = None

    # ----- 坐标换算 -----
    def _bpp(self):
        return self.page.bpp

    def block_to_pixel(self, bx, bz):
        w, h = self.width(), self.height()
        bpp = self._bpp()
        px = (bx - self._cx) / bpp + w / 2.0
        pz = (bz - self._cz) / bpp + h / 2.0
        return px, pz

    def pixel_to_block(self, px, pz):
        w, h = self.width(), self.height()
        bpp = self._bpp()
        bx = (px - w / 2.0) * bpp + self._cx
        bz = (pz - h / 2.0) * bpp + self._cz
        return bx, bz

    def regenerate(self, immediate=False):
        if immediate:
            self._do_regenerate()
        else:
            self._debounce.start(180)

    def _current_job(self):
        return (self.page.seed_int, self.page.dimension, self._cx, self._cz,
                max(50, self.width()), max(50, self.height()), self._bpp(),
                self.page.version)

    def _do_regenerate(self):
        job = self._current_job()
        self._desired_job = job
        if self._worker is not None and self._worker.isRunning():
            return  # 正在算；算完会按最新 _desired_job 补一次
        self._start_worker(job)

    def _start_worker(self, job):
        # 在主线程（而非 QThread.run 内）创建子进程执行器，避免 Windows/spawn
        # 下「在 worker 线程里首次 spawn 子进程」这一不稳定组合。
        if _init_executor() is None:
            return
        seed, dim, cx, cz, w, h, bpp, version = job
        self._pending = True
        self.update()
        self._worker = _MapWorker(seed, dim, cx, cz, w, h, bpp, version,
                                  visible=self.page.visible_structures())
        self._worker.preview.connect(self._on_preview)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_preview(self, qimg, cx, cz, img_w, img_h, world_bpp):
        # 只要仍是当前视图（种子/维度/中心一致）就作为即时预览
        if (self._desired_job is None or self._desired_job[0] != self.page.seed_int
                or self._desired_job[1] != self.page.dimension):
            return
        self._img = qimg
        self._img_meta = {"seed": self.page.seed_int, "dim": self.page.dimension,
                          "cx": cx, "cz": cz, "img_w": img_w, "img_h": img_h,
                          "world_bpp": world_bpp}
        self.update()

    def _on_done(self, qimg, cx, cz, img_w, img_h, world_bpp, bids, spawn, markers):
        seed, dim = self.page.seed_int, self.page.dimension
        self._img = qimg
        self._img_meta = {"seed": seed, "dim": dim, "cx": cx, "cz": cz,
                          "img_w": img_w, "img_h": img_h, "world_bpp": world_bpp}
        self._last_job = (seed, dim, cx, cz, img_w, img_h, world_bpp)
        self._pending = False
        self._bid_grid = bids
        self._bid_grid_meta = {"cx": cx, "cz": cz, "img_w": img_w,
                               "img_h": img_h, "world_bpp": world_bpp}
        self.page._spawn = spawn
        self.page._all_markers = markers
        self._rebuild_markers()
        self.update()
        # 若需要以出生点为中心（种子/版本变更后），补一次居中重算
        if self._recenter_pending and dim == "overworld" and spawn is not None:
            self._recenter_pending = False
            self._cx, self._cz = spawn
            self.regenerate(immediate=True)
            return
        self._recenter_pending = False
        # 视图可能又变了，补算直到追上
        if self._desired_job is not None and self._desired_job != self._last_job:
            self._start_worker(self._desired_job)

    def _rebuild_markers(self):
        # 纯过滤：结构与出生点已由 _MapWorker（子进程）预先算好并缓存到
        # page._all_markers / page._spawn；主线程只按当前视野裁剪 + 按开关过滤，
        # 绝不调用 cubiomes（否则会死锁）。
        w, h = self.width(), self.height()
        bpp = self._bpp()
        half_w = (w / 2.0) * bpp
        half_h = (h / 2.0) * bpp
        visible = set(self.page.visible_structures())
        markers = []
        for m in getattr(self.page, "_all_markers", []):
            if m["kind"] not in visible:
                continue
            bx, bz = m["bx"], m["bz"]
            if (self._cx - half_w) <= bx <= (self._cx + half_w) and \
               (self._cz - half_h) <= bz <= (self._cz + half_h):
                markers.append(m)
        # 出生点
        if self.page.dimension == "overworld" and self._spawn_visible \
                and self.page._spawn is not None:
            sx, sz = self.page._spawn
            if (self._cx - half_w) <= sx <= (self._cx + half_w) and \
               (self._cz - half_h) <= sz <= (self._cz + half_h):
                markers.append({"name": "出生点", "color": (255, 255, 255),
                                "bx": sx, "bz": sz, "kind": "spawn"})
        self._markers = markers

    # ----- 绘制 -----
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        t = THEMES[self.page.main.theme]
        p.fillRect(0, 0, W, H, QColor(t["CARD"]))
        self._draw_base(p)
        if self.page.grid_on:
            self._draw_grid(p)
        self._draw_rulers(p)
        self._draw_markers(p)
        if self._hover is not None:
            self._draw_tooltip(p)
        if self._pending:
            # 首次 / 切换种子：旧图无效，整屏遮罩
            if (self._img is None or self._img_meta is None
                    or self._img_meta["seed"] != self.page.seed_int
                    or self._img_meta["dim"] != self.page.dimension):
                p.fillRect(0, 0, W, H, QColor(20, 20, 28, 150))
                p.setPen(QColor(255, 255, 255))
                f = p.font(); f.setPointSize(15); p.setFont(f)
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "生成地图中…")
            else:
                # 缩放 / 平移中：保留预览，仅右上角小提示
                p.setPen(QColor(255, 255, 255, 210))
                f = p.font(); f.setPointSize(11); p.setFont(f)
                p.drawText(self.rect(),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                           "更新中… ")

    def _draw_base(self, p):
        """把已生成的位图按当前视图（缩放/平移）做仿射变换绘制，实现即时预览。"""
        if self._img is None or self._img.isNull() or self._img_meta is None:
            return
        m = self._img_meta
        if m["seed"] != self.page.seed_int or m["dim"] != self.page.dimension:
            return  # 种子/维度变了，旧图不再有效
        w1, h1 = self.width(), self.height()
        bpp1 = self._bpp()
        s = m["world_bpp"] / bpp1
        tx = (m["cx"] - self._cx) / bpp1 + w1 / 2.0 - (m["img_w"] / 2.0) * s
        ty = (m["cz"] - self._cz) / bpp1 + h1 / 2.0 - (m["img_h"] / 2.0) * s
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.translate(tx, ty)
        p.scale(s, s)
        p.drawImage(0, 0, self._img)
        p.restore()

    def _draw_grid(self, p):
        w, h = self.width(), self.height()
        bpp = self._bpp()
        # 每 128 方块一条网格线（对齐世界坐标）
        step = 128.0
        # 找到左侧最近的网格线
        start_bx = math.floor((self._cx - w / 2.0 * bpp) / step) * step
        x = self.block_to_pixel(start_bx, 0)[0]
        while x < w:
            p.setPen(QColor(255, 255, 255, 30))
            p.drawLine(int(x), 0, int(x), h)
            start_bx += step
            x = self.block_to_pixel(start_bx, 0)[0]
        start_bz = math.floor((self._cz - h / 2.0 * bpp) / step) * step
        y = self.block_to_pixel(0, start_bz)[1]
        while y < h:
            p.setPen(QColor(255, 255, 255, 30))
            p.drawLine(0, int(y), w, int(y))
            start_bz += step
            y = self.block_to_pixel(0, start_bz)[1]

    def _draw_rulers(self, p):
        w, h = self.width(), self.height()
        bpp = self._bpp()
        p.setPen(QColor(255, 255, 255, 200))
        # 顶部 X 标尺
        step = self._ruler_step(bpp)
        bx0 = math.floor((self._cx - w / 2.0 * bpp) / step) * step
        while True:
            px = self.block_to_pixel(bx0, 0)[0]
            if px > w:
                break
            if px >= 0:
                p.drawLine(int(px), 0, int(px), 8)
                p.drawText(int(px) + 2, 16, str(int(bx0)))
            bx0 += step
        # 左侧 Z 标尺
        bz0 = math.floor((self._cz - h / 2.0 * bpp) / step) * step
        while True:
            pz = self.block_to_pixel(0, bz0)[1]
            if pz > h:
                break
            if pz >= 0:
                p.drawLine(0, int(pz), 8, int(pz))
                p.drawText(2, int(pz) + 12, str(int(bz0)))
            bz0 += step

    def _ruler_step(self, bpp):
        # 选择一个让相邻标尺间隔约 70px 的“整”步长
        target = 70.0 * bpp
        for s in (64, 128, 256, 512, 1024, 2048, 4096, 8192):
            if s >= target:
                return float(s)
        return 8192.0

    def _draw_markers(self, p):
        for m in self._markers:
            x, y = self.block_to_pixel(m["bx"], m["bz"])
            if -20 <= x <= self.width() + 20 and -20 <= y <= self.height() + 20:
                col = QColor(*m["color"])
                if m["kind"] == "spawn":
                    p.setBrush(col); p.setPen(QPen(QColor(0, 0, 0, 120), 2))
                    p.drawEllipse(int(x) - 7, int(y) - 7, 14, 14)
                    p.setPen(QColor(0, 0, 0)); p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawText(int(x) - 14, int(y) - 12, "出生")
                else:
                    p.setBrush(col); p.setPen(QPen(QColor(255, 255, 255, 180), 1))
                    p.drawRect(int(x) - 5, int(y) - 5, 10, 10)

    def _biome_name_at_pixel(self, bx, bz):
        """根据已缓存的生物群系 id 网格，返回某方块坐标处的群系中文名。
        纯 Python / numpy，不调用 cubiomes，可在主线程安全执行。
        """
        meta = self._bid_grid_meta
        if self._bid_grid is not None and meta is not None:
            x0 = meta["cx"] - meta["img_w"] / 2.0 * meta["world_bpp"]
            z0 = meta["cz"] - meta["img_h"] / 2.0 * meta["world_bpp"]
            px = int(round((bx - x0) / meta["world_bpp"]))
            pz = int(round((bz - z0) / meta["world_bpp"]))
            if 0 <= px < meta["img_w"] and 0 <= pz < meta["img_h"]:
                bid = int(self._bid_grid[pz, px])
                return biome_cn_name(bid)
        return "未知"

    def _draw_tooltip(self, p):
        bx, bz = self._hover
        name = self._biome_name_at_pixel(bx, bz)
        text = f"{name}\nX {int(bx)}  Z {int(bz)}"
        f = p.font(); f.setPointSize(11); p.setFont(f)
        tw = max(p.boundingRect(self.rect(), 0, text).width(), 90) + 16
        th = 40
        mx, my = self._hover_px
        rx = min(mx + 12, self.width() - tw - 4)
        ry = max(4, my - th - 8)
        p.fillRect(int(rx), int(ry), int(tw), int(th), QColor(20, 20, 28, 220))
        p.setPen(QColor(255, 255, 255))
        p.drawText(int(rx) + 8, int(ry) + 17, name)
        p.drawText(int(rx) + 8, int(ry) + 34, f"X {int(bx)}  Z {int(bz)}")

    # ----- 交互 -----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            # 先看是否点中标记
            hit = self._marker_at(e.position().x(), e.position().y())
            if hit:
                self.markerClicked.emit(hit)
                return
            self._drag = (e.position().x(), e.position().y(),
                         self._cx, self._cz)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        bx, bz = self.pixel_to_block(x, y)
        self._hover = (bx, bz)
        self._hover_px = (x, y)
        if self._drag is not None:
            dx, dy, cx0, cz0 = self._drag
            bpp = self._bpp()
            self._cx = cx0 - (x - dx) * bpp
            self._cz = cz0 - (y - dy) * bpp
            self._drag = (x, y, cx0, cz0)
            self.regenerate()
        self.update()

    def leaveEvent(self, e):
        self._hover = None
        self.update()

    def wheelEvent(self, e):
        # 先记录光标下的世界坐标（用旧缩放），再以光标为中心缩放
        bx, bz = self.pixel_to_block(e.position().x(), e.position().y())
        self._cx, self._cz = bx, bz
        if e.angleDelta().y() > 0:
            self.page.zoom_in()   # 内部已触发 regenerate（防抖）
        else:
            self.page.zoom_out()  # 内部已触发 regenerate（防抖）

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.regenerate()

    def _marker_at(self, x, y):
        best = None
        best_d = 12.0
        for m in self._markers:
            mx, my = self.block_to_pixel(m["bx"], m["bz"])
            d = math.hypot(mx - x, my - y)
            if d < best_d:
                best = m
                best_d = d
        return best


# 网页地图：内嵌 Minecraft Search（minecraftsearch.com）。
# 该站点用 WebAssembly 编译 cubiomes 在浏览器内运行，因此又快又清晰，
# 并自带「种子搜索器」。以下 URL 已做百分号编码（中文路径）。
_SEEDMAP_WEB_MAP_URL = (
    "https://minecraftsearch.com/zh-CN/%E5%B7%A5%E5%85%B7/%E7%A7%8D%E5%AD%90%E5%9C%B0%E5%9B%BE")
_SEEDMAP_WEB_FINDER_URL = (
    "https://minecraftsearch.com/zh-CN/%E5%B7%A5%E5%85%B7/%E7%A7%8D%E5%AD%90%E6%9F%A5%E6%89%BE%E5%99%A8")

# 把启动器的 种子/版本/维度 注入到内嵌网页（尽力而为，找不到控件也不影响）。
# 占位符 __SEED__ / __VER__ / __DIM__ 由 _sync_web 替换为 JSON 字符串。
_WEB_SYNC_JS = r"""
(function(){
  function fire(el, ev){ try{ el.dispatchEvent(new Event(ev, {bubbles:true})); }catch(e){} }
  function setVal(el, v){
    try{
      var s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      s.call(el, String(v));
    }catch(e){ try{ el.value = String(v); }catch(e2){} }
    fire(el, 'input'); fire(el, 'change');
  }
  var seed = __SEED__;
  var ver  = __VER__;
  var dim  = __DIM__;
  var ok = false;
  // 种子：优先 placeholder/id 含 seed/种子 的输入框，否则取首个可见文本输入框
  var inputs = document.querySelectorAll('input');
  var best = null;
  for (var i=0;i<inputs.length;i++){
    var el = inputs[i];
    var t = (el.type||'').toLowerCase();
    if (t==='hidden'||t==='checkbox'||t==='radio'||t==='button'||t==='submit'||t==='range'||t==='file') continue;
    var ph = (el.placeholder||'').toLowerCase();
    var id = (el.id||'').toLowerCase();
    if (ph.indexOf('seed')>=0 || id.indexOf('seed')>=0 || ph.indexOf('种子')>=0){ best = el; break; }
    if (!best && el.offsetParent !== null) best = best || el;
  }
  if (best){ setVal(best, seed); ok = true; }
  // 版本（尽力）：找含该版本文字的 <select> 选项
  var sels = document.querySelectorAll('select');
  for (var j=0;j<sels.length;j++){
    var sel = sels[j];
    var opts = sel.options;
    for (var k=0;k<opts.length;k++){
      if ((opts[k].textContent||'').indexOf(ver)>=0 || (opts[k].value||'').indexOf(ver)>=0){
        sel.selectedIndex = k; fire(sel, 'change'); break;
      }
    }
  }
  // 维度（尽力）：点击文字匹配 主世界/下界/末地 的按钮或标签
  var labels = {'overworld':'主世界','nether':'下界','end':'末地'};
  var want = labels[dim] || '';
  if (want){
    var all = document.querySelectorAll('button, a, [role=tab], [role=button]');
    for (var m=0;m<all.length;m++){
      if ((all[m].textContent||'').trim() === want){ try{ all[m].click(); }catch(e){} break; }
    }
  }
  return ok;
})();
"""


class SeedMapPage(QWidget):
    # 结构分组（用于“主结构”总开关与逐个开关）
    STRUCT_GROUPS = {
        "overworld": ["village", "desert_pyramid", "jungle_temple", "swamp_hut",
                      "igloo", "monument", "ocean_ruin", "shipwreck", "pillager",
                      "mansion", "ruined_portal", "stronghold"],
        "nether": ["fortress", "bastion"],
        "end": ["end_city"],
    }
    BIOMES = ["平原", "森林", "黑森林", "针叶林", "雪原", "沙漠", "热带草原",
              "恶地", "山地", "沙滩", "海洋", "深海", "河流", "冷冻海洋"]

    def __init__(self, main):
        super().__init__()
        import sys as _sys
        print("[DBG] SeedMapPage.__init__: start", flush=True)
        self.main = main
        self.seed_text = "54545"
        self.seed_int = parse_seed(self.seed_text)
        self.dimension = "overworld"
        self.version = "1.19.2"
        self.bpp = 4
        self.grid_on = True
        self._spawn = None
        self._all_markers = []        # 由 _MapWorker 在子进程算好回填（含结构+要塞）
        self._struct_toggles = {}     # name -> QCheckBox/button
        self._build_ui()
        self._rebuild_structure_bar()
        self._recompute_spawn()
        # 离线模式才需要在启动时出图；网页模式下由网页自行渲染，避免误触发 cubiomes
        if not getattr(self, "_web_mode", False):
            print("[DBG] SeedMapPage: offline regenerate(immediate)", flush=True)
            self.canvas._cx, self.canvas._cz = (self._spawn or (0, 0))
            self.canvas.regenerate(immediate=True)
        print("[DBG] SeedMapPage.__init__: end", flush=True)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QLabel("种子地图")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        # ---- 工具栏 ----
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.version_combo = QComboBox()
        self.version_combo.setMinimumHeight(34)
        self.version_combo.addItems([
            "1.21.1", "1.20.4", "1.20.1", "1.19.4", "1.19.2", "1.18.2"])
        # 追加已安装版本
        try:
            for v in self.main.core.list_installed():
                vid = v.get("id")
                if vid and vid not in [self.version_combo.itemText(i)
                                       for i in range(self.version_combo.count())]:
                    self.version_combo.addItem(vid)
        except Exception:
            pass
        self.version_combo.setCurrentText(self.version)
        self.version_combo.currentTextChanged.connect(self._on_version)
        bar.addWidget(self.version_combo)

        self.seed_edit = QLineEdit(self.seed_text)
        self.seed_edit.setObjectName("LineEdit")
        self.seed_edit.setPlaceholderText("输入种子（数字或文字）")
        self.seed_edit.setMinimumHeight(34)
        self.seed_edit.returnPressed.connect(self._apply_seed)
        bar.addWidget(self.seed_edit, 1)

        b_rand = make_ghost("🎲 随机")
        b_rand.clicked.connect(self._random_seed)
        bar.addWidget(b_rand)

        b_imp = make_ghost("从实例导入")
        b_imp.clicked.connect(self._import_seed)
        bar.addWidget(b_imp)

        root.addLayout(bar)

        # ---- 维度切换 ----
        dim_row = QHBoxLayout()
        dim_row.setSpacing(8)
        self.dim_buttons = {}
        for key, label in [("overworld", "主世界"),
                           ("nether", "下界"),
                           ("end", "末地")]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setMinimumHeight(32)
            b.clicked.connect(lambda _, k=key: self._set_dimension(k))
            self.dim_buttons[key] = b
            dim_row.addWidget(b)
        dim_row.addStretch(1)

        # 坐标跳转
        self.x_edit = QLineEdit("0"); self.x_edit.setObjectName("LineEdit")
        self.x_edit.setFixedWidth(90); self.x_edit.setMinimumHeight(32)
        self.z_edit = QLineEdit("0"); self.z_edit.setObjectName("LineEdit")
        self.z_edit.setFixedWidth(90); self.z_edit.setMinimumHeight(32)
        dim_row.addWidget(QLabel("X")); dim_row.addWidget(self.x_edit)
        dim_row.addWidget(QLabel("Z")); dim_row.addWidget(self.z_edit)
        b_go = make_primary("前往")
        b_go.setMinimumHeight(32); b_go.setFixedWidth(80)
        b_go.clicked.connect(self._goto)
        dim_row.addWidget(b_go)
        b_link = make_ghost("复制地图链接")
        b_link.clicked.connect(self._copy_link)
        dim_row.addWidget(b_link)
        root.addLayout(dim_row)

        self._refresh_dim_buttons()

        # ---- 地图模式切换 ----
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.web_btn = make_ghost("🌐 网页地图 (Minecraft Search)")
        self.offline_btn = make_ghost("🗺 离线地图 (cubiomes)")
        self.web_btn.setCheckable(True)
        self.offline_btn.setCheckable(True)
        self.web_btn.clicked.connect(lambda: self._set_mode(True))
        self.offline_btn.clicked.connect(lambda: self._set_mode(False))
        mode_row.addWidget(self.web_btn)
        mode_row.addWidget(self.offline_btn)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        # ---- 地图区域（堆叠：网页 / 离线）----
        self.map_stack = QStackedWidget()

        # 网页页：内嵌 Minecraft Search，又快又清晰，自带种子搜索器
        self.web_page = QWidget()
        web_l = QVBoxLayout(self.web_page)
        web_l.setContentsMargins(0, 0, 0, 0)
        self._web_placeholder = QLabel("（网页地图将在打开此页时加载）")
        web_l.addWidget(self._web_placeholder)
        self.web = None  # 懒加载：首次显示/切换到网页模式时才创建 QWebEngineView
        self.map_stack.addWidget(self.web_page)

        # 离线页：本地 cubiomes 精确还原（无网时使用，较慢且较粗糙）
        offline_page = QWidget()
        off_l = QHBoxLayout(offline_page)
        off_l.setSpacing(0)
        self.canvas = SeedMapCanvas(self)
        self.canvas.markerClicked.connect(self._on_marker_clicked)
        off_l.addWidget(self.canvas, 1)

        zoom_col = QVBoxLayout()
        zoom_col.setSpacing(6)
        b_zin = make_ghost("＋"); b_zin.setFixedSize(36, 36)
        b_zin.clicked.connect(self.zoom_in)
        b_zout = make_ghost("－"); b_zout.setFixedSize(36, 36)
        b_zout.clicked.connect(self.zoom_out)
        b_grid = make_ghost("网格"); b_grid.setFixedSize(36, 80)
        b_grid.setCheckable(True); b_grid.setChecked(True)
        b_grid.clicked.connect(lambda c: self._set_grid(c))
        zoom_col.addWidget(b_zin); zoom_col.addWidget(b_zout)
        zoom_col.addWidget(b_grid)
        zoom_col.addStretch(1)
        off_l.addLayout(zoom_col)
        self.map_stack.addWidget(offline_page)

        # 默认走「网页地图」；WebEngine 真正不可用时，_ensure_web() 会自动降级为离线模式
        self._web_mode = True
        self._set_mode(self._web_mode)
        root.addWidget(self.map_stack, 1)

        # ---- 结构开关条 ----
        self.struct_bar = QHBoxLayout()
        self.struct_bar.setSpacing(6)
        self._struct_container = QWidget()
        self._struct_container.setLayout(self.struct_bar)
        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setWidget(self._struct_container)
        scroller.setFixedHeight(52)
        root.addWidget(scroller)

        # ---- 底栏 ----
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.biome_combo = QComboBox()
        self.biome_combo.setMinimumHeight(32)
        self.biome_combo.addItems(self.BIOMES)
        bottom.addWidget(QLabel("选择群系"))
        bottom.addWidget(self.biome_combo)
        b_search = make_ghost("搜索附近")
        b_search.clicked.connect(self._search_nearby)
        bottom.addWidget(b_search)
        b_finder = make_ghost("种子搜索")
        b_finder.clicked.connect(self._open_seed_finder)
        bottom.addWidget(b_finder)
        bottom.addStretch(1)
        note = QLabel("网页地图由 Minecraft Search 提供（更快更清晰）；离线地图由 cubiomes 精确还原（版本 1.18–1.20）。")
        note.setObjectName("HintText")
        bottom.addWidget(note)
        root.addLayout(bottom)

    # ----- 维度 / 结构条 -----
    def _refresh_dim_buttons(self):
        for k, b in self.dim_buttons.items():
            b.setChecked(k == self.dimension)

    def _set_dimension(self, dim):
        if dim == self.dimension:
            return
        self.dimension = dim
        self._refresh_dim_buttons()
        self._rebuild_structure_bar()
        self._recompute_spawn()
        if self._web_mode:
            self._sync_web(0)
        else:
            if dim == "overworld" and self._spawn:
                self.canvas._cx, self.canvas._cz = self._spawn
            else:
                self.canvas._cx, self.canvas._cz = (0, 0)
            self.canvas.regenerate(immediate=True)

    def _rebuild_structure_bar(self):
        # 清空
        while self.struct_bar.count():
            w = self.struct_bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._struct_toggles = {}
        for name in self.STRUCT_GROUPS[self.dimension]:
            cfg = STRUCTURES.get(name, {})
            b = make_ghost(cfg.get("name", name))
            b.setCheckable(True)
            b.setChecked(True)
            b.setMinimumHeight(32)
            b.clicked.connect(lambda c, n=name: self._on_struct_toggle(n, c))
            self._struct_toggles[name] = b
            self.struct_bar.addWidget(b)
        # 出生点（始终显示）
        bsp = make_ghost("出生点")
        bsp.setCheckable(True); bsp.setChecked(True); bsp.setMinimumHeight(32)
        bsp.clicked.connect(lambda c: self._on_struct_toggle("__spawn__", c))
        self._struct_toggles["__spawn__"] = bsp
        self.struct_bar.addWidget(bsp)
        self.struct_bar.addStretch(1)

    def visible_structures(self):
        out = []
        for name, b in self._struct_toggles.items():
            if name == "__spawn__":
                continue
            if b.isChecked():
                out.append(name)
        return out

    def _on_struct_toggle(self, name, checked):
        if name == "__spawn__":
            self.canvas._spawn_visible = checked
        # 网页模式下网站自己处理结构显示；离线模式需重新计算结构标记（子进程内完成）
        if not self._web_mode:
            self.canvas.regenerate(immediate=True)

    def _recompute_spawn(self):
        # 出生点改由 _MapWorker 在【子进程】内计算并通过 _on_done 回填，
        # 主线程不再直接调用 cubiomes（会死锁）。这里仅标记“生成后居中到出生点”。
        self.canvas._recenter_pending = True

    # ----- 种子 / 版本 -----
    def _apply_seed(self):
        self.seed_text = self.seed_edit.text()
        self.seed_int = parse_seed(self.seed_text)
        self._recompute_spawn()
        if self._web_mode:
            self._sync_web(0)
        else:
            if self.dimension == "overworld" and self._spawn:
                self.canvas._cx, self.canvas._cz = self._spawn
            self.canvas.regenerate(immediate=True)

    def _random_seed(self):
        import random as _r
        s = str(_r.randint(-2147483648, 2147483647))
        self.seed_edit.setText(s)
        self._apply_seed()

    def _import_seed(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Minecraft 存档的 level.dat",
            "", "level.dat (level.dat)")
        if not path:
            return
        sd = read_level_seed(path)
        if sd is None:
            QMessageBox.warning(self, "导入失败",
                                "无法从该文件读取种子（不是有效的 level.dat）。")
            return
        self.seed_edit.setText(str(sd))
        self._apply_seed()

    def _on_version(self, text):
        self.version = text
        # 版本影响生物群系/结构生成，重新计算
        self._recompute_spawn()
        if self._web_mode:
            self._sync_web(0)
        else:
            self.canvas.regenerate(immediate=True)

    # ----- 坐标 / 缩放 / 网格 -----
    def _goto(self):
        try:
            x = int(self.x_edit.text())
            z = int(self.z_edit.text())
        except ValueError:
            return
        if self._web_mode:
            return  # 网页模式由网站自带坐标跳转
        self.canvas._cx, self.canvas._cz = x, z
        self.canvas.regenerate(immediate=True)

    def zoom_in(self):
        self.bpp = max(1, self.bpp - 1)
        if not self._web_mode:
            self.canvas.regenerate()

    def zoom_out(self):
        self.bpp = min(64, self.bpp + 1)
        if not self._web_mode:
            self.canvas.regenerate()

    def _set_grid(self, on):
        self.grid_on = on
        self.canvas.update()

    def _copy_link(self):
        link = (f"plainmc://seedmap?seed={self.seed_int}"
                f"&version={self.version}&dim={self.dimension}"
                f"&x={int(self.canvas._cx)}&z={int(self.canvas._cz)}")
        QApplication.clipboard().setText(link)
        QMessageBox.information(self, "已复制", "地图链接已复制到剪贴板。")

    # ----- 网页地图同步 -----
    def _fallback_offline(self, why):
        """WebEngine 不可用时降级为离线（cubiomes）地图。"""
        print("[DBG] 降级为离线地图:", why, flush=True)
        self.web = None
        self._web_mode = False
        if hasattr(self, "web_btn"):
            self.web_btn.setChecked(False)
            self.offline_btn.setChecked(True)
        try:
            ph = getattr(self, "_web_placeholder", None)
            if ph is not None:
                ph.setText("当前环境无法加载网页地图（PyQt6-WebEngine 不可用），\n"
                           "已切换为离线 cubiomes 地图。")
            self.map_stack.setCurrentIndex(1)
        except Exception:
            pass

    def _ensure_web(self):
        """懒加载网页地图：仅在首次需要时才加载 WebEngine 并创建 QWebEngineView。

        目的有二：
        1) 启动时不碰 Chromium / 190MB 的 Qt6WebEngineCore.dll（否则 exe 启动极慢）；
        2) 打包环境下 WebEngine 初始化失败时自动降级为离线模式，绝不让启动器崩溃。
        """
        if getattr(self, "web", None) is not None:
            return
        if not _load_webengine():
            self._fallback_offline("WebEngine 不可用")
            return
        try:
            self.web = QWebEngineView()
            self.web.setUrl(QUrl(_SEEDMAP_WEB_MAP_URL))
            self.web.loadFinished.connect(self._on_web_loaded)
            lay = self.web_page.layout()
            old = lay.itemAt(0)
            if old is not None:
                w = old.widget()
                if w is not None:
                    w.setParent(None)
            lay.addWidget(self.web)
        except Exception as e:
            print("[DBG] _ensure_web FAILED:", repr(e), flush=True)
            self._fallback_offline(repr(e))

    def _set_mode(self, web):
        self._web_mode = web
        if hasattr(self, "web_btn"):
            self.web_btn.setChecked(web)
            self.offline_btn.setChecked(not web)
        if web:
            self.map_stack.setCurrentIndex(0)
            if self.isVisible():
                self._ensure_web()
            self._sync_web(0)
        else:
            self.map_stack.setCurrentIndex(1)
            self.canvas.regenerate(immediate=True)

    def _sync_web(self, retry=0):
        """把启动器的 种子/版本/维度 注入内嵌网页（尽力而为）。"""
        if not getattr(self, "_web_mode", False):
            return
        web = getattr(self, "web", None)
        if web is None:
            return
        import json as _json
        script = (_WEB_SYNC_JS
                  .replace("__SEED__", _json.dumps(self.seed_text))
                  .replace("__VER__", _json.dumps(self.version))
                  .replace("__DIM__", _json.dumps(self.dimension)))
        try:
            web.runJavaScript(script, self._on_web_sync_result)
        except Exception:
            pass

    def _on_web_sync_result(self, ok):
        # ok 为注入脚本返回值：是否成功把种子写入网页输入框
        if not ok:
            self._web_sync_retry = getattr(self, "_web_sync_retry", 0) + 1
            if self._web_sync_retry <= 6:
                QTimer.singleShot(400, lambda: self._sync_web(self._web_sync_retry))
        else:
            self._web_sync_retry = 0

    def _on_web_loaded(self, ok):
        if ok:
            self._web_sync_retry = 0
            self._sync_web(0)

    def _on_marker_clicked(self, m):
        if getattr(self, "_marker_popup", None):
            try:
                self._marker_popup.close()
            except Exception:
                pass
        bx, bz = int(m["bx"]), int(m["bz"])
        tp = (f"/tp @p {bx} 64 {bz}" if m["kind"] != "spawn"
              else f"/tp @p {bx} 64 {bz}")
        popup = QFrame(self.canvas, Qt.WindowType.Popup)
        popup.setObjectName("MarkerPopup")
        popup.setFrameShape(QFrame.Shape.Box)
        lay = QVBoxLayout(popup)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)
        title = QLabel(m["name"])
        title.setObjectName("PopupTitle")
        lay.addWidget(title)
        lay.addWidget(QLabel(f"X {bx}  Z {bz}"))
        btn = make_primary("复制 /tp")
        btn.clicked.connect(lambda _, t=tp: (
            QApplication.clipboard().setText(t), popup.close()))
        lay.addWidget(btn)
        px, py = self.canvas.block_to_pixel(bx, bz)
        popup.move(self.canvas.mapToGlobal(QPoint(int(px), int(py))))
        popup.show()
        self._marker_popup = popup

    def _search_nearby(self):
        biome = self.biome_combo.currentText()
        # 在可见范围内查找最近的若干同类群系（采样）。
        # 该扫描含 cubiomes C 调用，必须放到子进程，故用 _SearchNearbyWorker。
        seed = self.seed_int
        cx, cz = int(self.canvas._cx), int(self.canvas._cz)
        bpp = self.bpp
        w, h = self.canvas.width(), self.canvas.height()
        radius = max(int(w / 2.0 * bpp), int(h / 2.0 * bpp))
        step = max(48, bpp * 6)
        worker = _SearchNearbyWorker(seed, biome, cx, cz, radius, step,
                                     self.dimension, self.version)
        worker.done.connect(
            lambda found: self._on_search_nearby_done(biome, found))
        self._nearby_worker = worker  # 保持引用，避免被 GC
        worker.start()

    def _on_search_nearby_done(self, biome, found):
        if not found:
            QMessageBox.information(self, "搜索附近",
                                    f"当前视野内未找到「{biome}」。")
            return
        cx, cz = int(self.canvas._cx), int(self.canvas._cz)
        found.sort(key=lambda p: (p[0] - cx) ** 2 + (p[1] - cz) ** 2)
        lines = "\n".join(f"X {x}  Z {z}" for x, z in found[:8])
        QMessageBox.information(self, f"附近的「{biome}」",
                               f"找到 {len(found)} 处，最近的 8 个：\n{lines}")

    def _open_seed_finder(self):
        """种子搜索：网页模式直接打开 Minecraft Search 的种子查找器；离线模式用本地对话框。"""
        if self._web_mode:
            self._ensure_web()
            if getattr(self, "web", None) is not None:
                self.map_stack.setCurrentIndex(0)
                self.web.setUrl(QUrl(_SEEDMAP_WEB_FINDER_URL))
            else:
                self._open_seed_finder_dialog()
        else:
            self._open_seed_finder_dialog()

    def _open_seed_finder_dialog(self):
        """MinecraftSearch：扫描大量种子，找出满足『结构/群系靠近出生点』条件的种子。"""
        from PyQt6.QtWidgets import (QDialog, QFormLayout, QSpinBox,
                                     QListWidget, QListWidgetItem)
        from PyQt6.QtCore import Qt
        dlg = QDialog(self)
        dlg.setWindowTitle("种子搜索 · MinecraftSearch")
        dlg.setMinimumWidth(480)
        dlg.setMinimumHeight(440)
        root = QVBoxLayout(dlg)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)
        type_combo = QComboBox()
        type_combo.addItems(["结构靠近出生点", "出生点群系"])
        form.addRow("条件类型", type_combo)

        struct_keys = list(STRUCT_TO_CUBIOMES.keys())
        struct_combo = QComboBox()
        for k in struct_keys:
            struct_combo.addItem(STRUCTURES[k]["name"])
        form.addRow("结构", struct_combo)

        biome_combo = QComboBox()
        biome_combo.addItems(self.BIOMES)
        form.addRow("群系", biome_combo)

        radius = QSpinBox()
        radius.setRange(100, 8000)
        radius.setSingleStep(100)
        radius.setValue(1500)
        form.addRow("半径（方块）", radius)

        count = QSpinBox()
        count.setRange(100, 20000)
        count.setSingleStep(100)
        count.setValue(1500)
        form.addRow("扫描数量", count)
        root.addLayout(form)

        def _sync():
            is_struct = type_combo.currentText() == "结构靠近出生点"
            struct_combo.setVisible(is_struct)
            biome_combo.setVisible(not is_struct)
            radius.setValue(1500 if is_struct else 800)
        type_combo.currentTextChanged.connect(_sync)
        _sync()

        btn_search = make_primary("开始搜索")
        root.addWidget(btn_search)
        status = QLabel("设置条件后点击「开始搜索」。扫描在 cubiomes 子进程内进行。")
        status.setObjectName("HintText")
        root.addWidget(status)
        results = QListWidget()
        results.setMinimumHeight(180)
        root.addWidget(results, 1)
        tip = QLabel("双击结果可将该种子载入地图。")
        tip.setObjectName("HintText")
        root.addWidget(tip)

        def _start():
            is_struct = type_combo.currentText() == "结构靠近出生点"
            if is_struct:
                criterion = {"kind": "structure",
                             "name": struct_keys[struct_combo.currentIndex()],
                             "radius": radius.value()}
            else:
                criterion = {"kind": "biome",
                             "biome": biome_combo.currentText(),
                             "radius": radius.value()}
            status.setText("搜索中…（cubiomes 子进程扫描种子）")
            btn_search.setEnabled(False)
            w = _SeedSearchWorker(criterion, self.version, count.value())
            dlg._worker = w
            w.done.connect(lambda res: _on_done(res))
            w.error.connect(lambda err: _on_error(err))
            w.start()

        def _on_done(res):
            btn_search.setEnabled(True)
            results.clear()
            for seed_u, info in res:
                item = QListWidgetItem(f"{seed_u}   —   {info}")
                item.setData(Qt.ItemDataRole.UserRole, int(seed_u))
                results.addItem(item)
            if not res:
                status.setText(f"未找到符合条件的种子（已扫描 {count.value()} 个）。"
                               f"可放宽半径或增大扫描数量。")
            else:
                status.setText(f"找到 {len(res)} 个符合条件的种子"
                               f"（已扫描 {count.value()} 个）。双击载入。")

        def _on_error(err):
            btn_search.setEnabled(True)
            status.setText("搜索失败：" + err)

        btn_search.clicked.connect(_start)
        results.itemDoubleClicked.connect(
            lambda item: self._load_finder_seed(
                dlg, int(item.data(Qt.ItemDataRole.UserRole))))
        dlg.exec()

    def _load_finder_seed(self, dlg, seed_u):
        self.seed_text = str(seed_u)
        self.seed_int = seed_u
        self.seed_edit.setText(self.seed_text)
        self._apply_seed()
        dlg.accept()

    def showEvent(self, e):
        super().showEvent(e)
        # 网页地图懒加载：首次进入本页时才加载 WebEngine（启动时不碰 Chromium / 190MB DLL）
        if getattr(self, "_web_mode", False) and getattr(self, "web", None) is None:
            self._ensure_web()
        if not getattr(self, "_web_mode", False):
            self.canvas.regenerate(immediate=True)


# -*- coding: utf-8 -*-
"""内存优化模块（插入到 PlainMC.py 中）。

目标：
  1) 游戏退出后清掉残留的 java/javaw 进程（它们常驻 1~2GB 物理内存）
  2) 回收启动器自己持有的图标 / 贴图 / 背景缓存
  3) 用 EmptyWorkingSet 把工作集页归还给系统 —— 让「任务管理器」里
     启动器那一行数字真实下降，而不是只在内部记账

注意：HeapCompact 在 x64 下调用约定不对会直接访问违例，实测已排除，勿用。
"""

_IS_WINDOWS = (os.name == "nt")

if _IS_WINDOWS:
    try:
        import ctypes
        import ctypes.wintypes as _wt

        _SIZE_T = ctypes.c_size_t

        class _PMC_EX(ctypes.Structure):
            """PROCESS_MEMORY_COUNTERS_EX —— x64 下必须含 PrivateUsage 才不错位。"""
            _fields_ = [
                ("cb", _wt.DWORD),
                ("PageFaultCount", _wt.DWORD),
                ("PeakWorkingSetSize", _SIZE_T),
                ("WorkingSetSize", _SIZE_T),
                ("QuotaPeakPagedPoolUsage", _SIZE_T),
                ("QuotaPagedPoolUsage", _SIZE_T),
                ("QuotaPeakNonPagedPoolUsage", _SIZE_T),
                ("QuotaNonPagedPoolUsage", _SIZE_T),
                ("PagefileUsage", _SIZE_T),
                ("PeakPagefileUsage", _SIZE_T),
                ("PrivateUsage", _SIZE_T),
            ]

        _k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        _psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
        _k32.GetCurrentProcess.restype = _wt.HANDLE
        _k32.GetCurrentProcess.argtypes = []
        _psapi.GetProcessMemoryInfo.argtypes = [
            _wt.HANDLE, ctypes.POINTER(_PMC_EX), _wt.DWORD]
        _psapi.GetProcessMemoryInfo.restype = _wt.BOOL
        _psapi.EmptyWorkingSet.argtypes = [_wt.HANDLE]
        _psapi.EmptyWorkingSet.restype = _wt.BOOL
    except Exception:
        _k32 = None
        _psapi = None
else:
    _k32 = None
    _psapi = None


def process_memory_mb():
    """返回当前进程的 (工作集 MB, 私有提交 MB)；非 Windows 或失败返回 (0, 0)。"""
    if not _IS_WINDOWS or _psapi is None:
        return (0.0, 0.0)
    try:
        c = _PMC_EX()
        c.cb = ctypes.sizeof(_PMC_EX)
        h = _k32.GetCurrentProcess()
        if not _psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return (0.0, 0.0)
        return (c.WorkingSetSize / 1048576.0, c.PrivateUsage / 1048576.0)
    except Exception:
        return (0.0, 0.0)


def trim_working_set():
    """把当前进程的工作集页归还给操作系统。

    返回 True 表示调用成功。这一步只改变「物理驻留」状态，不影响任何数据，
    之后访问页面时系统会重新调入 —— 对用户完全无感，但任务管理器里的
    内存数字会真实下降。
    """
    if not _IS_WINDOWS or _psapi is None:
        return False
    try:
        h = _k32.GetCurrentProcess()
        return bool(_psapi.EmptyWorkingSet(h))
    except Exception:
        return False


def _run_text(cmd, timeout=20):
    """执行命令并以文本返回输出。

    Windows 的 tasklist / wmic 输出是**本地代码页（GBK）**，用默认 utf-8
    解码会抛 UnicodeDecodeError，因此这里逐个编码尝试并最终 replace 兜底。
    """
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout,
                             creationflags=_NO_WINDOW)
        raw = res.stdout or b""
        for enc in ("utf-8", "gbk", "mbcs"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_tasklist_csv(text):
    """解析 tasklist /FO CSV /NH 的输出，返回 [(pid, name, ws_mb)]。"""
    import csv
    import io
    rows = []
    try:
        for row in csv.reader(io.StringIO(text or "")):
            if len(row) < 5:
                continue
            name = row[0]
            try:
                pid = int(row[1])
            except ValueError:
                continue
            mem_raw = (row[4] or "").replace(",", "").replace("K", "").strip()
            try:
                ws_mb = int(mem_raw) / 1024.0
            except ValueError:
                ws_mb = 0.0
            rows.append((pid, name, ws_mb))
    except Exception:
        pass
    return rows


def _iter_java_processes():
    """列出除本进程外的 java / javaw 进程 (pid, 名称, 工作集 MB)。"""
    if not _IS_WINDOWS:
        return []
    out = []
    try:
        rows = _parse_tasklist_csv(_run_text(
            ["tasklist", "/FI", "IMAGENAME eq java.exe", "/FO", "CSV", "/NH"]))
        rows += _parse_tasklist_csv(_run_text(
            ["tasklist", "/FI", "IMAGENAME eq javaw.exe", "/FO", "CSV", "/NH"]))
        for pid, name, ws_mb in rows:
            if pid == os.getpid():
                continue
            out.append((pid, name, ws_mb))
    except Exception:
        pass
    return out


def _get_process_commandline(pid):
    """取指定进程的命令行。

    Windows 11 起 **wmic 已被移除**，实测本机就没有 wmic，所以优先走
    PowerShell 的 CIM（Win32_Process）查询；CIM 不可用时再退回 wmic。
    两条路都失败则返回空字符串（此时按「不是游戏进程」处理，宁可漏杀
    也不误杀）。
    """
    # --- 方式 1：PowerShell + CIM（Win32_Process）---
    ps = (
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" "
        "-ErrorAction SilentlyContinue; "
        "if ($p) { Write-Output $p.CommandLine }" % pid
    )
    text = _run_text(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        timeout=25)
    if text.strip():
        return text.strip()

    # --- 方式 2：wmic（老系统兜底）---
    text = _run_text(
        ["wmic", "process", "where", "ProcessId=%d" % pid,
         "get", "CommandLine", "/value"], timeout=20)
    if text.strip():
        # wmic /value 格式： "CommandLine=xxx"
        for line in text.splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                if key.strip().lower() == "commandline":
                    return val.strip()
        return text.strip()
    return ""


def _is_own_game_process(pid):
    """判断该 java 进程是否由游戏目录启动。

    读取命令行 —— 比 tasklist 多一步，但能避免误杀用户自己用别的
    启动器 / IDE 开出来的 Java 程序。
    """
    text = _get_process_commandline(pid)
    if not text.strip():
        return False
    low = text.lower()
    markers = (".minecraft", "/versions/", "\\versions\\",
               "net.minecraft", "minecraft.launcher")
    return any(m in low for m in markers)


def kill_leftover_java(protect_pids=None):
    """结束残留的 java / javaw 进程，回收它们占用的物理内存。

    保护机制：
      · protect_pids 里的进程绝不碰（正在运行的游戏就在里面）
      · 只清理「游戏目录下启动」的进程，避免误杀用户自己开的 Java 程序
    返回 (杀掉的进程数, 释放的 MB 估算)。
    """
    protect = set(int(p) for p in (protect_pids or []))
    killed, freed = 0, 0.0
    if not _IS_WINDOWS:
        return (0, 0.0)
    try:
        for pid, name, ws_mb in _iter_java_processes():
            if pid in protect:
                continue
            if not _is_own_game_process(pid):
                continue
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=20,
                               creationflags=_NO_WINDOW)
                killed += 1
                freed += ws_mb
            except Exception:
                continue
    except Exception:
        pass
    return (killed, freed)


def clear_launcher_caches():
    """回收启动器自己持有的图片缓存，返回释放的字节数（估算）。"""
    freed = 0
    global _ICON_CACHE
    try:
        for data in list(_ICON_CACHE.values()):
            freed += len(data) if data else 0
        _ICON_CACHE = {}
    except Exception:
        pass
    try:
        pix = globals().get("_pix_cache")
        if isinstance(pix, dict):
            for v in list(pix.values()):
                try:
                    freed += v.sizeInBytes() if hasattr(v, "sizeInBytes") else 0
                except Exception:
                    pass
            pix.clear()
    except Exception:
        pass
    try:
        # PyQt6 的 QPixmapCache 只有静态方法（无 cache() 实例入口）
        from PyQt6.QtGui import QPixmapCache
        try:
            freed += QPixmapCache.cacheLimit() * 1024   # 上限（KB）
        except Exception:
            pass
        QPixmapCache.clear()
    except Exception:
        pass
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    return freed


def optimize_memory(app=None, main=None, kill_java=True,
                    clear_caches=True, trim=True, level=None):
    """一键内存优化（向后兼容 v1.1.0 的调用方式）。

    level=None 时按配置里的 mem_opt_level 走；1=轻度 / 2=中度 / 3=深度。
    """
    if level is None:
        try:
            level = int(main.config.get("mem_opt_level", 2)) if main else 2
        except Exception:
            level = 2
    level = max(1, min(3, int(level)))
    res = run_memory_optimize(
        level=level, main=main, kill_java=kill_java,
        clear_caches=clear_caches,
        trim_all=(level >= 2 and trim))
    # 兼容旧字段
    res["trimmed"] = res.get("trimmed", 0)
    res["before_mb"] = res["before"]["used_mb"]
    res["after_mb"] = res["after"]["used_mb"]
    return res


# -*- coding: utf-8 -*-
"""分层系统内存优化模块（插入到 PlainMC.py 中）。

设计目标：对齐 PCL 的「内存优化」——把整机的物理内存占用压低，让游戏能拿到
一块干净、充裕的内存，而不是只清理启动器自己那点缓存。

分层（level 越大越彻底，也越"有代价"）：

  L1 轻度 —— 只动自己
        · 归还启动器工作集（EmptyWorkingSet）
        · 清空启动器的图片缓存
        → 完全无副作用，随时可做

  L2 中度 —— 动全系统的工作集
        · 遍历所有进程，清空它们的工作集（把物理页还给系统）
        → 副作用：这些程序下次访问数据会重新从磁盘读，可能短暂卡顿

  L3 深度 —— 结束占内存的后台进程
        · 在 L2 基础上，结束「后台的、没有可见窗口的、占用大的」进程
        → 副作用：那个程序会被关闭（未保存内容可能丢失）
        → 严格保护：系统关键进程 / 有可见窗口的程序 / 白名单，绝不误杀

  L4 智能 —— 在 L3 基础上，按物理内存自动计算并应用推荐的 -Xmx
        → 让游戏真正能用上该用的内存，而不是固定 2GB

实测依据（12GB 机器，模拟 3 个 1GB 进程占满）：
    清空 132 个进程工作集   → 仅降 71 MB
    结束 3 个占用进程        → 降 3251 MB
  → 可见「真正的大头是结束占内存的进程」，工作集清空只是把内存还给
     系统文件缓存，程序一碰又会占回来。

保护名单的设计原则：宁可漏杀，绝不误杀。
"""

import ctypes
import ctypes.wintypes as _wt

_SIZE_T = ctypes.c_size_t
_DWORD = _wt.DWORD

# ---------------------------------------------------------------- Win32 绑定

_IS_WINDOWS = (os.name == "nt")


class _MEMORYSTATUSEX(ctypes.Structure):
    """MEMORYSTATUSEX —— 读整机物理内存用。"""
    _fields_ = [
        ("dwLength", _DWORD),
        ("dwMemoryLoad", _DWORD),
        ("ullTotalPhys", _SIZE_T),
        ("ullAvailPhys", _SIZE_T),
        ("ullTotalPageFile", _SIZE_T),
        ("ullAvailPageFile", _SIZE_T),
        ("ullTotalVirtual", _SIZE_T),
        ("ullAvailVirtual", _SIZE_T),
        ("ullAvailExtendedVirtual", _SIZE_T),
    ]


class _PMC_EX(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS_EX —— x64 下必须含 PrivateUsage 才不错位。"""
    _fields_ = [
        ("cb", _DWORD),
        ("PageFaultCount", _DWORD),
        ("PeakWorkingSetSize", _SIZE_T),
        ("WorkingSetSize", _SIZE_T),
        ("QuotaPeakPagedPoolUsage", _SIZE_T),
        ("QuotaPagedPoolUsage", _SIZE_T),
        ("QuotaPeakNonPagedPoolUsage", _SIZE_T),
        ("QuotaNonPagedPoolUsage", _SIZE_T),
        ("PagefileUsage", _SIZE_T),
        ("PeakPagefileUsage", _SIZE_T),
        ("PrivateUsage", _SIZE_T),
    ]


if _IS_WINDOWS:
    try:
        _k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        _psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
        _user32 = ctypes.WinDLL("user32.dll", use_last_error=True)

        _k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
        _k32.GlobalMemoryStatusEx.restype = _wt.BOOL
        _k32.OpenProcess.restype = _wt.HANDLE
        _k32.OpenProcess.argtypes = [_DWORD, _wt.BOOL, _DWORD]
        _k32.CloseHandle.argtypes = [_wt.HANDLE]
        _k32.GetCurrentProcess.restype = _wt.HANDLE
        _k32.GetCurrentProcess.argtypes = []
        _k32.QueryFullProcessImageNameW.argtypes = [
            _wt.HANDLE, _DWORD, ctypes.c_wchar_p, ctypes.POINTER(_DWORD)]

        _psapi.EmptyWorkingSet.argtypes = [_wt.HANDLE]
        _psapi.EmptyWorkingSet.restype = _wt.BOOL
        _psapi.GetProcessMemoryInfo.argtypes = [
            _wt.HANDLE, ctypes.POINTER(_PMC_EX), _DWORD]
        _psapi.GetProcessMemoryInfo.restype = _wt.BOOL
        _psapi.EnumProcesses.argtypes = [
            ctypes.POINTER(_DWORD), _DWORD, ctypes.POINTER(_DWORD)]
        _psapi.EnumProcesses.restype = _wt.BOOL

        _user32.EnumWindows.restype = _wt.BOOL
        _user32.IsWindowVisible.argtypes = [_wt.HANDLE]
        _user32.GetWindowThreadProcessId.argtypes = [
            _wt.HANDLE, ctypes.POINTER(_DWORD)]
    except Exception:
        _k32 = _psapi = _user32 = None
else:
    _k32 = _psapi = _user32 = None


# 常量
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_VM_OPERATION = 0x0008
_PROCESS_TERMINATE = 0x0001


# ------------------------------------------------------- 绝对不能动的进程
# 系统关键进程：动了会崩系统
_CRITICAL_PROCESSES = {    "system", "system idle process", "registry", "memory compression",
    "secure system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "fontdrvhost.exe",
    "dwm.exe", "sihost.exe", "ctfmon.exe", "spoolsv.exe", "audiodg.exe",
    "conhost.exe", "wudfhost.exe", "runtimebroker.exe", "searchindexer.exe",
    "searchprotocolhost.exe", "searchfilterhost.exe", "wmiprvse.exe",
    "msmpeng.exe", "nissrv.exe", "avastsvc.exe", "avgsvc.exe",
    "mbamservice.exe", "mbaem.exe", "zhudnsclient.exe", "360tray.exe",
    "360safemon.exe", "huorong.exe", "hipsdaemon.exe", "qqpcmgr.exe",
    "itmongrp.exe", "usoclient.exe", "uninst.exe", "installer.exe",
    "setup.exe", "msiexec.exe", "taskhostw.exe", "backgroundtaskhost.exe",
    "dllhost.exe", "wmiprvse.exe", "msmpeng.exe", "lockapp.exe",
}

# 交互式 / 开发工具：可能是用户正在用的，或结束掉会丢数据
_ALWAYS_KEEP = {
    "explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe",
    "windowsterminal.exe", "conhost.exe", "python.exe", "pythonw.exe",
    "py.exe", "python3.exe", "node.exe", "java.exe", "javaw.exe",
    "code.exe", "code - insiders.exe", "devenv.exe", "idea64.exe",
    "pycharm64.exe", "webstorm64.exe", "notepad++.exe", "git.exe",
    "bash.exe", "sh.exe", "zsh.exe", "vim.exe", "nvim.exe",
    "chrome.exe", "msedge.exe", "firefox.exe", "iexplore.exe",
    "qq.exe", "wechat.exe", "weixin.exe", "tim.exe", "discord.exe",
    "telegram.exe", "steam.exe", "steamwebhelper.exe", "gameoverlayui.exe",
    "obs64.exe", "obs32.exe", "taskmgr.exe", "msconfig.exe",
    "textinputhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "peopleexperiencehost.exe", "searchapp.exe", "searchui.exe",
    # 宿主 / 沙箱 / 自动化工具：名字不固定，但它们承载着用户当前的工作
    "workbuddy.exe", "sandbox-cli.exe", "electron.exe", "code-oss.exe",
    "windowsterminal-preview.exe", "postman.exe", "insomnia.exe",
    "datagrip.exe", "rider64.exe", "clion64.exe", "goland64.exe",
}


# 进程名会变（WorkBuddy / IDE / 各类宿主），光靠名单挡不住。
# 根本解法：把「自己 + 自己的整条祖先链」全部拉黑 —— 杀了父进程，
# 当前进程和它所在的整个会话都会跟着崩，是最危险的一类误杀。
_ancestor_cache = {"pids": None}


def _ancestor_pids():
    """返回当前进程的祖先 pid 集合（含直接父进程，逐级向上）。

    用 Toolhelp32 快照一次性拿全系统的父子关系，比逐个查 NtQueryInformationProcess 快。
    结果缓存 5 秒 —— 父链在进程生命周期内几乎不变，没必要反复快照。
    """
    import time as _t
    cached = _ancestor_cache.get("pids")
    if cached is not None and (_t.time() - _ancestor_cache.get("ts", 0)) < 5:
        return cached
    pids = set()
    if _IS_WINDOWS and _k32 is not None:
        try:
            class _PE32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", _DWORD), ("cntUsage", _DWORD),
                    ("th32ProcessID", _DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", _DWORD), ("cntThreads", _DWORD),
                    ("th32ParentProcessID", _DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", _DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]
            TH32CS_SNAPPROCESS = 0x00000002
            _k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap and snap != ctypes.c_void_p(-1).value:
                parents = {}
                pe = _PE32()
                pe.dwSize = ctypes.sizeof(_PE32)
                if _k32.Process32First(snap, ctypes.byref(pe)):
                    while True:
                        parents[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
                        if not _k32.Process32Next(snap, ctypes.byref(pe)):
                            break
                _k32.CloseHandle(ctypes.c_void_p(snap))
                cur = os.getpid()
                for _ in range(24):          # 最多向上追 24 层
                    par = parents.get(cur, 0)
                    if not par or par in pids or par == cur:
                        break
                    pids.add(par)
                    cur = par
        except Exception:
            pass
    _ancestor_cache["pids"] = pids
    _ancestor_cache["ts"] = _t.time()
    return pids


def _protected_pids(extra=None):
    """汇总「绝对不能结束」的 pid：自己 + 祖先链 + 调用方额外指定。"""
    pids = set(extra or [])
    pids.add(os.getpid())
    pids |= _ancestor_pids()
    return pids


def system_memory():
    """返回整机物理内存字典。

    {'total_mb', 'available_mb', 'used_mb', 'load_percent'}
    """
    if not _IS_WINDOWS or _k32 is None:
        return {"total_mb": 0.0, "available_mb": 0.0, "used_mb": 0.0,
                "load_percent": 0}
    try:
        m = _MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        _k32.GlobalMemoryStatusEx(ctypes.byref(m))
        total = m.ullTotalPhys / 1048576.0
        avail = m.ullAvailPhys / 1048576.0
        return {
            "total_mb": round(total, 1),
            "available_mb": round(avail, 1),
            "used_mb": round(total - avail, 1),
            "load_percent": int(m.dwMemoryLoad),
        }
    except Exception:
        return {"total_mb": 0.0, "available_mb": 0.0, "used_mb": 0.0,
                "load_percent": 0}


def _visible_window_pids():
    """有可见主窗口的进程 PID 集合 —— 用户正在用的程序，绝不结束。"""
    if not _IS_WINDOWS or _user32 is None:
        return set()
    pids = set()
    try:
        @ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
        def _cb(hwnd, _lparam):
            pid = _DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
            return True

        _user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return pids


def _enum_processes():
    """枚举进程，返回 [(pid, name, ws_mb, private_mb, path)]。

    ws_mb      = 工作集（物理驻留，会被 EmptyWorkingSet 清空）
    private_mb = 私有提交（进程真正申请的内存，不会被清工作集影响）
    """
    if not _IS_WINDOWS or _psapi is None:
        return []
    out = []
    try:
        n = 8192
        pids = (_DWORD * n)()
        got = _DWORD()
        if not _psapi.EnumProcesses(pids, ctypes.sizeof(pids),
                                    ctypes.byref(got)):
            return []
        for i in range(got.value // 4):
            pid = pids[i]
            if not pid:
                continue
            h = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
            if not h:
                continue
            buf = ctypes.create_unicode_buffer(260)
            path = ""
            try:
                sz = _DWORD(260)
                _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz))
                path = buf.value or ""
            except Exception:
                path = ""
            c = _PMC_EX()
            c.cb = ctypes.sizeof(_PMC_EX)
            _psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
            _k32.CloseHandle(h)
            name = path.rsplit("\\", 1)[-1] if path else "?"
            out.append((int(pid), name, c.WorkingSetSize / 1048576.0,
                        c.PrivateUsage / 1048576.0, path))
    except Exception:
        pass
    return out


def trim_all_working_sets(exclude_pids=None):
    """清空所有进程的工作集，把物理页还给系统。

    这是 PCL 内存优化的核心动作之一。副作用：这些程序下次访问数据要重新
    从磁盘读，可能有一次短暂卡顿。

    返回 (成功清空的进程数, 释放的 MB 估算)。
    """
    if not _IS_WINDOWS or _psapi is None:
        return (0, 0.0)
    exclude = _protected_pids(exclude_pids)
    trimmed, freed = 0, 0.0
    try:
        n = 8192
        pids = (_DWORD * n)()
        got = _DWORD()
        _psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(got))
        for i in range(got.value // 4):
            pid = int(pids[i])
            if not pid or pid in exclude:
                continue
            h = _k32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_QUERY_INFORMATION, False, pid)
            if not h:
                continue
            try:
                if _psapi.EmptyWorkingSet(h):
                    trimmed += 1
            except Exception:
                pass
            _k32.CloseHandle(h)
    except Exception:
        pass
    # 释放量用整机可用内存的变化来衡量更准
    return (trimmed, freed)


def find_memory_hogs(min_mb=200, exclude_pids=None):
    """找出「可以安全结束」的占内存后台进程。

    判定条件（全部满足才返回）：
      · 不在系统关键进程名单里
      · 不在「始终保留」名单里（浏览器 / 编辑器 / 终端 / Python 等）
      · 没有可见主窗口（不是用户正在用的程序）
      · 不是本启动器自己，也不是它启动的游戏进程
      · **私有提交** >= min_mb

    为什么用「私有提交」而不是「工作集」：工作集是物理驻留量，清空工作集
    (EmptyWorkingSet) 之后它会立刻掉到很小，导致「先清工作集、再找大户」
    这两步互相抵消。私有提交才是进程真正申请的内存量，不受清工作集影响。

    返回 [(pid, name, private_mb, path)]，按占用从大到小排序。
    """
    if not _IS_WINDOWS:
        return []
    exclude = _protected_pids(exclude_pids)
    vis = _visible_window_pids()
    out = []
    for pid, name, ws, private, path in _enum_processes():
        if pid in exclude or pid in vis:
            continue
        low = name.lower()
        if low in _CRITICAL_PROCESSES or low in _ALWAYS_KEEP:
            continue
        if private < min_mb:
            continue
        out.append((pid, name, private, path))
    out.sort(key=lambda x: -x[2])
    return out


def terminate_process(pid):
    """结束指定进程，返回是否成功。"""
    if not _IS_WINDOWS:
        return False
    try:
        res = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True, timeout=20,
            creationflags=_NO_WINDOW)
        return res.returncode == 0
    except Exception:
        return False


def kill_memory_hogs(min_mb=200, exclude_pids=None, dry_run=False):
    """结束占内存的后台进程，回收它们的内存。

    只处理通过 find_memory_hogs 安全判定的进程。
    返回 (结束数, 释放 MB 估算, 列表[(pid,name,private_mb)])。
    """
    exclude = set(exclude_pids or [])
    # 正在运行的游戏进程必须排除
    exclude.add(os.getpid())
    hogs = find_memory_hogs(min_mb=min_mb, exclude_pids=exclude)
    killed, freed = 0, 0.0
    done = []
    for pid, name, private, path in hogs:
        if dry_run:
            done.append((pid, name, private))
            freed += private
            continue
        if terminate_process(pid):
            killed += 1
            freed += private
            done.append((pid, name, private))
    return (killed, freed, done)


def recommend_game_memory(total_mb=None, mod_count=0, reserved_ratio=0.25):
    """按物理内存推荐游戏的 -Xmx（MB）。

    规则（参考 PCL 的自动分配思路）：
      · 留出 reserved_ratio 给系统、驱动、显存交换（默认 25%）
      · 基础配额：总内存的 40%
      · 有 MOD 时额外加：每 20 个 MOD 加 256MB（地图 / 生成类 MOD 才真吃内存，
        但这里做个保守加成）
      · 上下限：512MB ~ 12GB

    注意：可用内存的钳制用的是「整机总内存的 45%」而不是当前可用内存。
    因为清理内存优化会在启动前释放出空间，若按当时的可用内存算，
    优化前会被压低、优化后又放不下 —— 出现「越优化分配越少」的矛盾。
    """
    info = system_memory()
    if total_mb is None:
        total_mb = info.get("total_mb", 0.0)
    if total_mb <= 0:
        return 2048
    budget = total_mb * (1.0 - reserved_ratio)
    base = total_mb * 0.40
    mod_bonus = (mod_count // 20) * 256
    value = int(base + mod_bonus)
    value = max(512, min(value, 12288))
    # 上限按「总内存的 45%」封顶，而不是按当前可用内存 ——
    # 否则内存优化释放出来的空间反而用不上。
    value = min(value, max(512, int(total_mb * 0.45)))
    # 取 256MB 的整数倍
    value = (value // 256) * 256
    return max(512, value)


# ---------------------------------------------------------- 统一优化入口

OPT_LEVELS = {
    1: "轻度（仅释放启动器自身）",
    2: "中度（清空全系统进程工作集）",
    3: "深度（结束占用内存的后台进程）",
}


def run_memory_optimize(level=2, main=None, kill_java=True,
                        clear_caches=True, min_hog_mb=200, trim_all=True):
    """执行一次分层内存优化。

    level:
      1 轻度 —— 只归还启动器工作集 + 清缓存
      2 中度 —— 加上清空全系统进程工作集
      3 深度 —— 加上结束占内存的后台进程

    返回 dict：
      level          使用的优化级别
      before         优化前整机内存 {total, available, used, load}
      after          优化后整机内存
      freed_mb       整机可用内存的净增量（MB）
      trimmed        清空工作集的进程数
      killed         结束的进程数
      killed_list    [(pid, name, mb)]
      cache_freed    启动器缓存释放的字节数
    """
    before = system_memory()

    # --- 保护：正在运行的游戏进程 ---
    protect = []
    proc = getattr(getattr(main, "launch_page", None), "_proc", None)
    if proc is not None:
        try:
            if proc.state() == QProcess.ProcessState.Running:
                protect.append(int(proc.processId()))
        except Exception:
            pass

    cache_freed = 0
    killed = 0
    killed_list = []
    trimmed = 0

    # --- 清理残留 java（各级都做，这是最安全也最有效的一项）---
    if kill_java:
        k, mb = kill_leftover_java(protect)
        killed += k
        if k:
            killed_list.append((0, "残留 Java 进程", mb))

    # --- 清启动器缓存 ---
    if clear_caches:
        cache_freed = clear_launcher_caches()

    # --- L2/L3：全系统工作集 ---
    if level >= 2 and trim_all:
        trimmed, _ = trim_all_working_sets(exclude_pids=protect)

    # --- L3：结束占内存的后台进程 ---
    if level >= 3:
        k, mb, lst = kill_memory_hogs(min_mb=min_hog_mb,
                                      exclude_pids=protect)
        killed += k
        killed_list.extend(lst)

    # --- L1：归还自身工作集（放在最后，此时缓存已清、状态最干净）---
    trim_working_set()

    # 刷新界面，避免刚清掉的缓存立刻重建
    try:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
    except Exception:
        pass

    after = system_memory()
    freed = max(0.0, after["available_mb"] - before["available_mb"])
    return {
        "level": level,
        "before": before,
        "after": after,
        "freed_mb": round(freed, 1),
        "trimmed": trimmed,
        "killed": killed,
        "killed_list": killed_list,
        "cache_freed": cache_freed,
        # 整机已用内存（MB）—— before / after 已是整机字典，这里给出标量便于 UI 显示
        "before_mb": round(before["used_mb"], 1),
        "after_mb": round(after["used_mb"], 1),
    }


class MainWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.config = self._load_config()
        self.accounts = self.config.setdefault("accounts", [])
        self.theme = self.config.get("theme", "light")
        game_dir = self.config.get("game_dir") or os.path.join(PROJECT_ROOT, "game_data")
        mirror = self.config.get("mirror") or DEFAULT_MIRROR
        self.core = MinecraftCore(game_dir, mirror=mirror)

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(880, 600)
        self.resize(1020, 680)

        self._build_ui()
        self._apply_theme()
        # 个性化：背景图 / 窗口不透明度 / 标题栏 / 功能隐藏 / 背景音乐
        self._load_bg_from_config()
        self.apply_window_opacity()
        self.apply_title()
        self.apply_hidden_pages()
        self._setup_music()
        self._switch(1)  # 默认进入“下载”页
        self._center()

    def paintEvent(self, e):
        """无边框圆角窗口：自己绘制背景（WA_TranslucentBackground 下
        QSS 的 QWidget#MainWindow 背景并不可靠，故不依赖它）。支持自定义
        背景图片（自适应方式 / 不透明度 / 模糊 / 叠加彩色背景）。"""
        from PyQt6.QtGui import QPainter, QColor, QPainterPath
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = float(self.width()), float(self.height())
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, W, H, 14.0, 14.0)
        p.fillPath(path, QColor(THEMES[self.theme]["BG"]))

        cache = getattr(self, "_bg_cache", None)
        if cache is not None and not cache.isNull():
            p.save()
            p.setClipPath(path)
            op = float(self.config.get("bg_opacity", 1.0) or 1.0)
            p.setOpacity(max(0.0, min(1.0, op)))
            fit = self.config.get("bg_fit", "auto") or "auto"
            if fit == "stretch":
                p.drawPixmap(0, 0, int(W), int(H), cache)
            elif fit == "center":
                dx = (W - cache.width()) / 2.0
                dy = (H - cache.height()) / 2.0
                p.drawPixmap(int(dx), int(dy), cache)
            elif fit == "tile":
                brush = QBrush(cache)
                p.setBrush(brush)
                p.setBrushOrigin(0, 0)
                p.fillRect(0, 0, int(W), int(H), brush)
            else:  # auto / cover
                scale = max(W / cache.width(), H / cache.height())
                dw = cache.width() * scale
                dh = cache.height() * scale
                p.drawPixmap(int((W - dw) / 2.0), int((H - dh) / 2.0),
                             int(dw), int(dh), cache)
            p.restore()
            if self.config.get("bg_tint"):
                tint = QColor(self.config.get("accent") or THEMES[self.theme]["ACCENT"])
                tint.setAlpha(80)
                p.save()
                p.setClipPath(path)
                p.fillPath(path, tint)
                p.restore()

    def closeEvent(self, e):
        """退出前等待后台线程收尾，避免 QThread 在运行中被销毁
        （会阻塞 UI 并打印 "QThread: Destroyed while thread is still running"）。"""
        import time as _t
        deadline = _t.time() + 3.0
        while _ACTIVE_THREADS and _t.time() < deadline:
            for t in list(_ACTIVE_THREADS):
                t.wait(200)
            if all(not t.isRunning() for t in list(_ACTIVE_THREADS)):
                break
        e.accept()

    # ---------- 配置 ----------
    def _load_config(self):
        defaults = {
            "theme": "light",
            "username": "Steve",
            "java_path": "",
            "memory": 2048,
            "mem_opt_on_exit": True,   # 游戏退出后自动内存优化
            "mem_opt_before_launch": True,  # 启动游戏前先优化一次
            "mem_opt_level": 1,        # 0=轻度 1=中度 2=深度（默认中度）
            "auto_mem_alloc": False,    # 自动按物理内存分配 -Xmx
            "game_dir": "",
            "mirror": DEFAULT_MIRROR,
            # ---- 个性化 ----
            "accent": "",          # 空 = 跟随主题默认强调色
            "accent2": "",
            "window_opacity": 1.0,
            "bg_image_dir": BG_DIR,
            "bg_fit": "auto",      # auto / stretch / center / tile
            "bg_opacity": 1.0,
            "bg_blur": 0,          # 0-30
            "bg_tint": False,      # 叠加彩色背景（用强调色）
            "music_dir": MUSIC_DIR,
            "music_autoplay": False,
            "title_mode": "default",  # none / default / text / image
            "title_text": "PlainMC 启动器",
            "title_logo": "",
            "hidden_pages": [],    # 被隐藏的侧边栏导航 index 列表
            "nav_layout": "vertical",  # vertical（侧边栏）/ horizontal（顶部合并下载）
        }
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    defaults.update(json.load(f))
            except Exception:
                pass
        return defaults

    def save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 主题 ----------
    def _apply_theme(self):
        app = QApplication.instance()
        accent = self.config.get("accent") or None
        accent2 = self.config.get("accent2") or None
        if app:
            app.setStyleSheet(get_stylesheet(self.theme, accent, accent2))
        self.update()  # paintEvent 用主题色画窗口背景，需强制重绘

    # ---------- 个性化：背景图 ----------
    def _load_bg_from_config(self):
        """扫描背景图目录，取第一张图片载入；并按当前模糊值重建缓存。"""
        self._bg_raw = None
        self._bg_cache = None
        d = self.config.get("bg_image_dir") or BG_DIR
        if not os.path.isdir(d):
            return
        exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
        imgs = sorted(f for f in os.listdir(d)
                      if f.lower().endswith(exts))
        if not imgs:
            return
        pm = QPixmap(os.path.join(d, imgs[0]))
        if pm.isNull():
            return
        self._bg_raw = pm
        self._rebuild_bg_cache()

    def _rebuild_bg_cache(self):
        self._bg_cache = None
        raw = getattr(self, "_bg_raw", None)
        if raw is None or raw.isNull():
            return
        w, h = raw.width(), raw.height()
        cap = 1600
        scale = min(1.0, cap / max(w, h)) if max(w, h) else 1.0
        pm = (raw.scaled(int(w * scale), int(h * scale),
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
              if scale < 1 else raw)
        blur = int(self.config.get("bg_blur", 0) or 0)
        if blur > 0:
            pm = blur_pixmap(pm, blur)
        self._bg_cache = pm
        # 启动页大图与主窗口共用同一张背景图
        lp = getattr(self, "launch_page", None)
        if lp is not None and hasattr(lp, "refresh_hero"):
            lp.refresh_hero()

    def apply_bg_settings(self):
        """背景图相关设置（模糊/透明度/叠加色/自适应）变化后调用。"""
        self._rebuild_bg_cache()
        self.update()
        lp = getattr(self, "launch_page", None)
        if lp is not None and hasattr(lp, "refresh_hero"):
            lp.refresh_hero()

    # ---------- 个性化：窗口不透明度 ----------
    def apply_window_opacity(self):
        v = float(self.config.get("window_opacity", 1.0) or 1.0)
        v = max(0.4, min(1.0, v))
        self.setWindowOpacity(v)

    # ---------- 个性化：标题栏 ----------
    def apply_title(self):
        if hasattr(self, "title_bar"):
            self.title_bar.update_title()

    # ---------- 个性化：强调色 ----------
    def set_accent(self, hex_color):
        if hex_color:
            c = QColor(hex_color)
            if not c.isValid():
                return
            self.config["accent"] = c.name()
            self.config["accent2"] = derive_accent2(c.name()) or c.name()
        else:
            self.config["accent"] = ""
            self.config["accent2"] = ""
        self._apply_theme()

    # ---------- 个性化：功能隐藏 ----------
    def apply_hidden_pages(self, reveal=False):
        hidden = set(self.config.get("hidden_pages", []) or [])
        for idx, btn in getattr(self, "nav_buttons", []):
            if reveal:
                btn.setVisible(True)
            else:
                btn.setVisible(idx not in hidden)
        # 横行顶部栏
        for key, btn in getattr(self, "h_nav_buttons", []):
            if isinstance(key, (set, frozenset)):
                # 下载合并入口：仅当全部子项都被隐藏时才隐藏入口
                if reveal:
                    btn.setVisible(True)
                else:
                    btn.setVisible(not key.issubset(hidden))
                # 下拉菜单中禁用被隐藏的子项
                menu = getattr(self, "_dl_menu", None)
                if menu is not None:
                    for a in menu.actions():
                        di = a.data()
                        if isinstance(di, int):
                            a.setEnabled(reveal or di not in hidden)
            else:
                if reveal:
                    btn.setVisible(True)
                else:
                    btn.setVisible(key not in hidden)

    def keyPressEvent(self, e):
        # F12 临时显示被隐藏的功能（再按一次恢复隐藏）
        if e.key() == Qt.Key.Key_F12:
            hidden = set(self.config.get("hidden_pages", []) or [])
            if hidden:
                self._reveal = not getattr(self, "_reveal", False)
                self.apply_hidden_pages(reveal=self._reveal)
            e.accept()
            return
        super().keyPressEvent(e)

    # ---------- 个性化：背景音乐 ----------
    def _setup_music(self):
        self._music_player = None
        self._music_out = None
        self._music_files = []
        self._music_index = 0
        self._scan_music()
        if self.config.get("music_autoplay") and self._music_files:
            self.start_music()

    def _scan_music(self):
        d = self.config.get("music_dir") or MUSIC_DIR
        self._music_files = []
        if os.path.isdir(d):
            exts = (".mp3", ".wav", ".ogg", ".flac", ".m4a")
            self._music_files = sorted(
                os.path.join(d, f) for f in os.listdir(d)
                if f.lower().endswith(exts))
        return self._music_files

    def start_music(self):
        if not _HAVE_MULTIMEDIA:
            return
        files = self._music_files or self._scan_music()
        if not files:
            return
        if self._music_player is None:
            self._music_out = QAudioOutput()
            self._music_player = QMediaPlayer(self)
            self._music_player.setAudioOutput(self._music_out)
            self._music_player.mediaStatusChanged.connect(
                self._on_music_status)
        idx = self._music_index % len(files)
        self._music_player.setSource(QUrl.fromLocalFile(files[idx]))
        self._music_player.play()

    def _on_music_status(self, status):
        from PyQt6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            files = self._music_files
            if files:
                self._music_index = (self._music_index + 1) % len(files)
                self._music_player.setSource(
                    QUrl.fromLocalFile(files[self._music_index]))
                self._music_player.play()

    def stop_music(self):
        if self._music_player:
            self._music_player.stop()

    def set_theme(self, t):
        self.theme = t
        self.config["theme"] = t
        self.save_config()
        self._apply_theme()

    def set_game_root(self, path):
        self.config["game_dir"] = path
        self.core = MinecraftCore(path, mirror=self.config.get("mirror") or DEFAULT_MIRROR)
        self.save_config()

    def set_mirror(self, key):
        self.core.set_mirror(key)
        self.config["mirror"] = key
        self.save_config()

    # ---------- 界面 ----------
    # 下载类页面（横行布局时合并到“下载”一个入口）
    DOWNLOAD_INDICES = frozenset({1, 2, 6, 7, 8, 9})
    SIDEBAR_ORDER = [
        ("🚀", "启动", 0),
        ("⬇", "下载", 1),
        ("🧩", "MOD", 2),
        ("📦", "整合包", 6),
        ("📁", "数据包", 7),
        ("🎨", "资源包", 8),
        ("💡", "光影包", 9),
        ("🌐", "联机", 10),
        ("🎭", "个性化", 11),
        ("🌍", "种子地图", 12),
        ("📑", "配方", 13),
        ("🖌", "皮肤", 14),
        ("👤", "账号", 3),
        ("⚙", "设置", 4),
        ("ℹ", "关于", 5),
    ]
    TOPBAR_ORDER = [
        ("🚀", "启动", 0),
        ("⬇", "下载", "group"),   # 合并入口：内含游戏版本 / MOD / 整合包 / 数据包 / 资源包 / 光影包
        ("🌐", "联机", 10),
        ("🎭", "个性化", 11),
        ("🌍", "种子地图", 12),
        ("📑", "配方", 13),
        ("🖌", "皮肤", 14),
        ("👤", "账号", 3),
        ("⚙", "设置", 4),
        ("ℹ", "关于", 5),
    ]

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)

        # 页面栈（两种布局共用，只创建一次）
        self._build_stack()

        # 导航 + 内容容器：内部结构随布局切换重建
        self.nav_and_content = QWidget()
        root.addWidget(self.nav_and_content, 1)

        self._apply_nav_layout()

    def _build_stack(self):
        import sys as _sys
        print("[DBG] _build_stack: start", flush=True)
        self.stack = QStackedWidget()
        self.launch_page = LaunchPage(self);               print("[DBG] page 0 LaunchPage", flush=True)  # 0
        self.download_page = DownloadPage(self);           print("[DBG] page 1 DownloadPage", flush=True)  # 1
        self.mod_page = ModPage(self);                    print("[DBG] page 2 ModPage", flush=True)  # 2
        self.accounts_page = AccountsPage(self);           print("[DBG] page 3 AccountsPage", flush=True)  # 3
        self.settings_page = SettingsPage(self);           print("[DBG] page 4 SettingsPage", flush=True)  # 4
        self.about_page = AboutPage(self);                 print("[DBG] page 5 AboutPage", flush=True)  # 5
        self.modpack_page = ModpackPage(self);             print("[DBG] page 6 ModpackPage", flush=True)  # 6 整合包
        self.datapack_page = DatapackPage(self);           print("[DBG] page 7 DatapackPage", flush=True)  # 7 数据包
        self.resourcepack_page = ResourcePackPage(self);   print("[DBG] page 8 ResourcePackPage", flush=True)  # 8 资源包
        self.shader_page = ShaderPage(self);               print("[DBG] page 9 ShaderPage", flush=True)  # 9 光影包
        self.mp_page = MultiplayerPage(self);              print("[DBG] page 10 MultiplayerPage", flush=True)  # 10 联机
        self.personal_page = PersonalizationPage(self);    print("[DBG] page 11 PersonalizationPage", flush=True)  # 11 个性化
        self.seedmap_page = SeedMapPage(self);             print("[DBG] page 12 SeedMapPage", flush=True)  # 12 种子地图
        self.recipe_page = RecipeGeneratorPage(self);     print("[DBG] page 13 RecipeGeneratorPage", flush=True)  # 13 配方生成器
        self.skin_page = SkinEditorPage(self);            print("[DBG] page 14 SkinEditorPage", flush=True)  # 14 皮肤编辑器
        for pg in (self.launch_page, self.download_page, self.mod_page,
                   self.accounts_page, self.settings_page, self.about_page,
                   self.modpack_page, self.datapack_page, self.resourcepack_page,
                   self.shader_page, self.mp_page, self.personal_page,
                   self.seedmap_page, self.recipe_page,
                   self.skin_page):
            self.stack.addWidget(pg)

    def _apply_nav_layout(self):
        layout = self.config.get("nav_layout", "vertical")
        # 切换前清空两个导航列表，避免指向已被销毁的旧控件
        self.nav_buttons = []
        self.h_nav_buttons = []
        # 卸下旧布局；stack 复用（取出即可），其余导航控件销毁
        old = self.nav_and_content.layout()
        if old is not None:
            while old.count():
                it = old.takeAt(0)
                w = it.widget()
                if w is not None and w is not self.stack:
                    w.setParent(None)
                    w.deleteLater()
            QWidget().setLayout(old)

        if layout == "horizontal":
            lay = QVBoxLayout(self.nav_and_content)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            lay.addWidget(self._make_topbar())
            lay.addWidget(self.stack, 1)
        else:
            lay = QHBoxLayout(self.nav_and_content)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            lay.addWidget(self._make_sidebar())
            lay.addWidget(self.stack, 1)

        self.apply_hidden_pages()
        self._refresh_nav_checked(self.stack.currentIndex())

    def _make_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName("SideBar")
        sidebar.setFixedWidth(212)
        s_layout = QVBoxLayout(sidebar)
        s_layout.setContentsMargins(14, 16, 14, 16)
        s_layout.setSpacing(10)

        self.btn_launch = QPushButton("🚀  启动")
        self.btn_launch.setObjectName("LaunchNav")
        self.btn_launch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_launch.clicked.connect(lambda: self._switch(0))
        s_layout.addWidget(self.btn_launch)

        self.nav_buttons = []
        for icon, text, idx in self.SIDEBAR_ORDER[1:]:
            b = make_nav_button(icon, text)
            b.clicked.connect(lambda _, i=idx: self._switch(i))
            self.nav_buttons.append((idx, b))
            s_layout.addWidget(b)
        s_layout.addStretch(1)
        return sidebar

    def _make_topbar(self):
        bar = QWidget()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(54)
        t_layout = QHBoxLayout(bar)
        t_layout.setContentsMargins(14, 8, 14, 8)
        t_layout.setSpacing(8)

        self.btn_launch = None
        self.h_nav_buttons = []
        for icon, text, key in self.TOPBAR_ORDER:
            if key == "group":
                btn = self._make_download_group()
                self.h_nav_buttons.append((self.DOWNLOAD_INDICES, btn))
            else:
                if key == 0:
                    btn = QPushButton(f"{icon}  {text}")
                    btn.setObjectName("LaunchNav")
                    btn.setMinimumHeight(42)
                else:
                    btn = make_nav_button(icon, text)
                    btn.setMinimumWidth(84)
                btn.clicked.connect(lambda _, i=key: self._switch(i))
                if key == 0:
                    self.btn_launch = btn
                self.h_nav_buttons.append((key, btn))
            t_layout.addWidget(btn)
        t_layout.addStretch(1)
        return bar

    def _make_download_group(self):
        """横行布局的“下载”合并入口：下拉菜单列出所有下载类页面。"""
        btn = QPushButton("⬇  下载")
        btn.setObjectName("NavButton")
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(42)
        btn.setMinimumWidth(96)
        menu = QMenu(btn)
        menu.setObjectName("NavMenu")
        for icon, text, idx in [
            ("⬇", "游戏版本", 1),
            ("🧩", "MOD", 2),
            ("📦", "整合包", 6),
            ("📁", "数据包", 7),
            ("🎨", "资源包", 8),
            ("💡", "光影包", 9),
        ]:
            a = menu.addAction(f"{icon}  {text}")
            a.setData(idx)
            a.triggered.connect(lambda _, i=idx: self._switch(i))
        btn.setMenu(menu)
        self._dl_menu = menu
        return btn

    def _switch(self, index):
        self.stack.setCurrentIndex(index)
        for idx, b in getattr(self, "nav_buttons", []):
            b.setChecked(idx == index)
        if getattr(self, "btn_launch", None) is not None:
            if self.config.get("nav_layout") == "horizontal":
                self.btn_launch.setChecked(index == 0)
            else:
                self.btn_launch.setProperty("selected", index == 0)
        self._refresh_nav_checked(index)
        if index == 1 and hasattr(self, "download_page"):
            self.download_page._ensure_list()

    def _refresh_nav_checked(self, index):
        for key, b in getattr(self, "h_nav_buttons", []):
            if isinstance(key, (set, frozenset)):
                b.setChecked(index in key)
            else:
                b.setChecked(index == key)

    def set_nav_layout(self, layout):
        if layout not in ("vertical", "horizontal"):
            return
        if self.config.get("nav_layout") == layout:
            return
        self.config["nav_layout"] = layout
        self.save_config()
        self._apply_nav_layout()

    def toggle_max(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _center(self):
        screen = QApplication.primaryScreen()
        if screen:
            r = screen.availableGeometry()
            self.move(
                r.x() + (r.width() - self.width()) // 2,
                r.y() + (r.height() - self.height()) // 2,
            )


# ==========================================================================
# 入口
# ==========================================================================
def main():
    import sys as _sys
    print("[DBG] main: start", flush=True)
    # QtWebEngine 的硬性要求：必须在创建 QApplication 之前设置 AA_ShareOpenGLContexts，
    # 否则 PyQt6 会拒绝「事后」再导入 QtWebEngineWidgets。
    # 设置后就能把 WebEngine 的导入延迟到真正打开「网页地图」时，
    # 避免启动阶段就加载 190MB 的 Qt6WebEngineCore.dll（打包后解压出来的冷文件，
    # 加载要几十秒，表现为「双击 exe 后一两分钟没反应」）。
    try:
        from PyQt6.QtCore import Qt as _Qt
        QApplication.setAttribute(
            _Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    except Exception as _e:
        print("[DBG] 设置 AA_ShareOpenGLContexts 失败:", repr(_e), flush=True)
    # Qt6 默认已启用高 DPI 缩放与高清图片渲染，无需再设置 AA_* 属性
    app = QApplication(_sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(f"{APP_NAME} 启动器")
    print("[DBG] main: QApplication ready", flush=True)

    win = MainWindow()
    print("[DBG] main: MainWindow constructed", flush=True)
    win.show()
    print("[DBG] main: shown", flush=True)

    # 后台预热 Modrinth 缓存（列表 + 图标）：让「MOD / 数据包 / …」页面秒开
    try:
        _warm = ModCacheWarmer()
        _track_worker(_warm)
        QTimer.singleShot(2500, _warm.start)
    except Exception as _e:
        print("[DBG] 缓存预热启动失败:", repr(_e), flush=True)

    _rc = app.exec()
    print(f"[DBG] main: app.exec() returned rc={_rc}", flush=True)
    _sys.exit(_rc)




# =====================================================================
# 以下为内联模块（原 mc_items / mc_textures / seedmap_engine / recipe_editor）
# 已合并进本文件，便于单文件分发。材质维护脚本 _build_texture_map.py 仍可单独使用。
# =====================================================================

# ---- mc_items.py ----
# -*- coding: utf-8 -*-
"""Minecraft Java 版物品数据库（用于配方生成器的物品选择器）。

每个物品：
    id    : 形如 "minecraft:stone" 的完整命名空间 ID
    zh    : 中文名
    cat   : 分类（用于分组与配色）
    color : 图标底色（十六进制），缺省时按 id 哈希生成稳定色

提供：
    search_items(q, limit)  -> 按中文名 / id 模糊搜索
    ITEMS                   -> 全量列表
    CATEGORIES              -> 分类顺序
"""

# 分类 -> 默认配色（用于图标底色，保证同分类视觉统一）
_CAT_COLOR = {
    "方块": "#8A8F98",
    "装饰": "#B58B6A",
    "自然": "#6FAE5A",
    "矿石": "#9AA0A6",
    "工具": "#C9A24B",
    "武器": "#C26B5A",
    "装备": "#7C8BD0",
    "红石": "#C24B4B",
    "食物": "#D98A4B",
    "生物": "#C77FB0",
    "材料": "#B7BFC9",
    "药水": "#9B5BC7",
    "其他": "#8E96A6",
}

# (id, 中文名, 分类, 颜色[可选])
_RAW = [
    # ---- 方块 / 基础 ----
    ("minecraft:stone", "石头", "方块", "#8A8A8A"),
    ("minecraft:granite", "花岗岩", "方块", "#9E6B58"),
    ("minecraft:diorite", "闪长岩", "方块", "#D9D6CE"),
    ("minecraft:andesite", "安山岩", "方块", "#8C8C8C"),
    ("minecraft:deepslate", "深板岩", "方块", "#3F4147"),
    ("minecraft:cobblestone", "圆石", "方块", "#7E7E7E"),
    ("minecraft:sand", "沙子", "方块", "#E3D9A6"),
    ("minecraft:red_sand", "红沙", "方块", "#CB8B5A"),
    ("minecraft:gravel", "沙砾", "方块", "#8C8577"),
    ("minecraft:dirt", "泥土", "方块", "#8B6038"),
    ("minecraft:grass_block", "草方块", "自然", "#6AAE52"),
    ("minecraft:podzol", "灰化土", "自然", "#7A5230"),
    ("minecraft:mycelium", "菌丝体", "自然", "#7E5B8C"),
    ("minecraft:coarse_dirt", "粗泥土", "方块", "#7A5436"),
    ("minecraft:rooted_dirt", "缠根泥土", "方块", "#6E4B30"),
    ("minecraft:clay", "黏土块", "方块", "#9FB6C2"),
    ("minecraft:terracotta", "陶瓦", "装饰", "#A85B43"),
    ("minecraft:white_terracotta", "白色陶瓦", "装饰", "#D6CABA"),
    ("minecraft:red_terracotta", "红色陶瓦", "装饰", "#9C4730"),
    ("minecraft:black_terracotta", "黑色陶瓦", "装饰", "#3B312F"),
    ("minecraft:obsidian", "黑曜石", "方块", "#1B1330"),
    ("minecraft:bedrock", "基岩", "方块", "#2B2B30"),
    ("minecraft:glass", "玻璃", "装饰", "#BBD6E0"),
    ("minecraft:glass_pane", "玻璃板", "装饰", "#BBD6E0"),
    ("minecraft:bookshelf", "书架", "装饰", "#7A5430"),
    # ---- 木板 / 原木 ----
    ("minecraft:oak_log", "橡木原木", "自然", "#6E4B30"),
    ("minecraft:spruce_log", "云杉原木", "自然", "#5A4632"),
    ("minecraft:birch_log", "白桦原木", "自然", "#9A8B6A"),
    ("minecraft:jungle_log", "丛林木原木", "自然", "#6B5232"),
    ("minecraft:acacia_log", "金合欢原木", "自然", "#786049"),
    ("minecraft:dark_oak_log", "深色橡木原木", "自然", "#43332A"),
    ("minecraft:mangrove_log", "红树木原木", "自然", "#6B4A3A"),
    ("minecraft:crimson_stem", "绯红菌柄", "自然", "#9B3B5B"),
    ("minecraft:warped_stem", "诡异菌柄", "自然", "#3B7B7B"),
    ("minecraft:oak_planks", "橡木木板", "方块", "#B5894E"),
    ("minecraft:spruce_planks", "云杉木板", "方块", "#7E5C3A"),
    ("minecraft:birch_planks", "白桦木板", "方块", "#C2B089"),
    ("minecraft:jungle_planks", "丛林木板", "方块", "#9A744A"),
    ("minecraft:acacia_planks", "金合欢木板", "方块", "#A07448"),
    ("minecraft:dark_oak_planks", "深色橡木木板", "方块", "#4E3A2C"),
    ("minecraft:mangrove_planks", "红树木板", "方块", "#8A5A44"),
    ("minecraft:crimson_planks", "绯红木板", "方块", "#B24B68"),
    ("minecraft:warped_planks", "诡异木板", "方块", "#3E8C8C"),
    ("minecraft:oak_wood", "橡木", "自然", "#7A5230"),
    ("minecraft:oak_leaves", "橡树树叶", "自然", "#4E8B3A"),
    ("minecraft:spruce_leaves", "云杉树叶", "自然", "#2F5E2C"),
    ("minecraft:birch_leaves", "白桦树叶", "自然", "#6FAE5A"),
    ("minecraft:azalea_leaves", "杜鹃树叶", "自然", "#4E8B3A"),
    # ---- 石制品 ----
    ("minecraft:stone_bricks", "石砖", "方块", "#8A8F98"),
    ("minecraft:mossy_stone_bricks", "苔石砖", "方块", "#6E7A52"),
    ("minecraft:cracked_stone_bricks", "裂石砖", "方块", "#7E828A"),
    ("minecraft:polished_andesite", "磨制安山岩", "方块", "#9A9A9A"),
    ("minecraft:polished_diorite", "磨制闪长岩", "方块", "#E2DFD7"),
    ("minecraft:polished_granite", "磨制花岗岩", "方块", "#AE7A66"),
    ("minecraft:bricks", "红砖块", "方块", "#A85B43"),
    ("minecraft:nether_bricks", "下界砖块", "方块", "#43271F"),
    ("minecraft:red_nether_bricks", "红色下界砖", "方块", "#7A2A2A"),
    ("minecraft:quartz_block", "石英块", "方块", "#EDEDED"),
    ("minecraft:smooth_quartz", "平滑石英块", "方块", "#F2F2F2"),
    ("minecraft:purpur_block", "紫珀块", "方块", "#9B6BC7"),
    ("minecraft:end_stone", "末地石", "方块", "#D9D2B0"),
    ("minecraft:end_stone_bricks", "末地石砖", "方块", "#CFC8A4"),
    ("minecraft:prismarine", "海晶石", "方块", "#5A8FA0"),
    ("minecraft:dark_prismarine", "暗海晶石", "方块", "#2F5560"),
    ("minecraft:sea_lantern", "海晶灯", "装饰", "#9FD6C2"),
    ("minecraft:sandstone", "砂岩", "方块", "#D9C68A"),
    ("minecraft:red_sandstone", "红砂岩", "方块", "#C0794A"),
    ("minecraft:cut_sandstone", "切制砂岩", "方块", "#D2BC7E"),
    # ---- 台阶 / 楼梯 / 墙 / 栅栏 ----
    ("minecraft:stone_stairs", "石楼梯", "装饰", "#8A8F98"),
    ("minecraft:oak_stairs", "橡木楼梯", "装饰", "#B5894E"),
    ("minecraft:oak_slab", "橡木台阶", "装饰", "#B5894E"),
    ("minecraft:cobblestone_wall", "圆石墙", "装饰", "#7E7E7E"),
    ("minecraft:oak_fence", "橡木栅栏", "装饰", "#B5894E"),
    ("minecraft:oak_fence_gate", "橡木栅栏门", "装饰", "#B5894E"),
    ("minecraft:spruce_fence", "云杉栅栏", "装饰", "#7E5C3A"),
    ("minecraft:iron_bars", "铁栏杆", "装饰", "#B7BFC9"),
    # ---- 彩色混凝土 / 羊毛 ----
    ("minecraft:white_concrete", "白色混凝土", "装饰", "#DFDFDF"),
    ("minecraft:gray_concrete", "灰色混凝土", "装饰", "#8E96A6"),
    ("minecraft:black_concrete", "黑色混凝土", "装饰", "#2B2B30"),
    ("minecraft:red_concrete", "红色混凝土", "装饰", "#B5403A"),
    ("minecraft:blue_concrete", "蓝色混凝土", "装饰", "#3A6FB5"),
    ("minecraft:green_concrete", "绿色混凝土", "装饰", "#4E9A52"),
    ("minecraft:yellow_concrete", "黄色混凝土", "装饰", "#D6B53A"),
    ("minecraft:white_wool", "白色羊毛", "装饰", "#E6E6E6"),
    ("minecraft:red_wool", "红色羊毛", "装饰", "#C24B4B"),
    ("minecraft:blue_wool", "蓝色羊毛", "装饰", "#4B6FC2"),
    ("minecraft:black_wool", "黑色羊毛", "装饰", "#2B2B30"),
    ("minecraft:lime_wool", "黄绿羊毛", "装饰", "#7FC24B"),
    ("minecraft:purple_wool", "紫色羊毛", "装饰", "#8B5BC2"),
    ("minecraft:carpet", "地毯", "装饰", "#C24B4B"),
    # ---- 矿石 / 锭 ----
    ("minecraft:coal", "煤炭", "矿石", "#2B2B30"),
    ("minecraft:charcoal", "木炭", "矿石", "#3A3A3A"),
    ("minecraft:iron_ore", "铁矿石", "矿石", "#B58B6A"),
    ("minecraft:deepslate_iron_ore", "深板岩铁矿石", "矿石", "#5A5560"),
    ("minecraft:gold_ore", "金矿石", "矿石", "#C9A24B"),
    ("minecraft:copper_ore", "铜矿石", "矿石", "#B5703A"),
    ("minecraft:redstone_ore", "红石矿石", "矿石", "#9B3B3B"),
    ("minecraft:lapis_ore", "青金石矿石", "矿石", "#3A5BC2"),
    ("minecraft:diamond_ore", "钻石矿石", "矿石", "#5BC2B5"),
    ("minecraft:emerald_ore", "绿宝石矿石", "矿石", "#3AC26B"),
    ("minecraft:netherite_scrap", "下界合金碎片", "矿石", "#3A3530"),
    ("minecraft:coal_block", "煤炭块", "矿石", "#2B2B30"),
    ("minecraft:iron_block", "铁块", "矿石", "#D6D6D6"),
    ("minecraft:gold_block", "金块", "矿石", "#F2C94C"),
    ("minecraft:diamond_block", "钻石块", "矿石", "#5BE0D0"),
    ("minecraft:emerald_block", "绿宝石块", "矿石", "#3AE07B"),
    ("minecraft:lapis_block", "青金石块", "矿石", "#3A5BE0"),
    ("minecraft:netherite_block", "下界合金块", "矿石", "#3A3330"),
    ("minecraft:iron_ingot", "铁锭", "材料", "#D6D6D6"),
    ("minecraft:gold_ingot", "金锭", "材料", "#F2C94C"),
    ("minecraft:copper_ingot", "铜锭", "材料", "#C77B45"),
    ("minecraft:diamond", "钻石", "材料", "#5BE0D0"),
    ("minecraft:emerald", "绿宝石", "材料", "#3AE07B"),
    ("minecraft:lapis_lazuli", "青金石", "材料", "#3A5BE0"),
    ("minecraft:redstone", "红石", "红石", "#D63A3A"),
    ("minecraft:netherite_ingot", "下界合金锭", "材料", "#3A3330"),
    ("minecraft:raw_iron", "粗铁", "材料", "#B58B6A"),
    ("minecraft:raw_gold", "粗金", "材料", "#C9A24B"),
    ("minecraft:raw_copper", "粗铜", "材料", "#B5703A"),
    ("minecraft:quartz", "石英", "材料", "#EDEDED"),
    ("minecraft:amethyst_shard", "紫水晶碎片", "材料", "#B57BC2"),
    ("minecraft:nether_star", "下界之星", "材料", "#E6E6E6"),
    ("minecraft:prismarine_shard", "海晶碎片", "材料", "#5A8FA0"),
    ("minecraft:prismarine_crystals", "海晶砂砾", "材料", "#7FC2D0"),
    ("minecraft:blaze_rod", "烈焰棒", "材料", "#E0A23A"),
    ("minecraft:ender_pearl", "末影珍珠", "材料", "#C9B6D6"),
    ("minecraft:slime_ball", "黏液球", "生物", "#6FC26B"),
    ("minecraft:ghast_tear", "恶魂之泪", "材料", "#D6E6F2"),
    # ---- 工具 / 武器 / 装备 ----
    ("minecraft:wooden_pickaxe", "木镐", "工具", "#B5894E"),
    ("minecraft:stone_pickaxe", "石镐", "工具", "#8A8F98"),
    ("minecraft:iron_pickaxe", "铁镐", "工具", "#D6D6D6"),
    ("minecraft:golden_pickaxe", "金镐", "工具", "#F2C94C"),
    ("minecraft:diamond_pickaxe", "钻石镐", "工具", "#5BE0D0"),
    ("minecraft:netherite_pickaxe", "下界合金镐", "工具", "#3A3330"),
    ("minecraft:wooden_sword", "木剑", "武器", "#B5894E"),
    ("minecraft:stone_sword", "石剑", "武器", "#8A8F98"),
    ("minecraft:iron_sword", "铁剑", "武器", "#D6D6D6"),
    ("minecraft:diamond_sword", "钻石剑", "武器", "#5BE0D0"),
    ("minecraft:netherite_sword", "下界合金剑", "武器", "#3A3330"),
    ("minecraft:bow", "弓", "武器", "#9A744A"),
    ("minecraft:crossbow", "弩", "武器", "#8A8F98"),
    ("minecraft:trident", "三叉戟", "武器", "#7FC2D0"),
    ("minecraft:shield", "盾牌", "装备", "#B58B6A"),
    ("minecraft:iron_helmet", "铁头盔", "装备", "#D6D6D6"),
    ("minecraft:iron_chestplate", "铁胸甲", "装备", "#D6D6D6"),
    ("minecraft:iron_leggings", "铁护腿", "装备", "#D6D6D6"),
    ("minecraft:iron_boots", "铁靴子", "装备", "#D6D6D6"),
    ("minecraft:diamond_helmet", "钻石头盔", "装备", "#5BE0D0"),
    ("minecraft:diamond_chestplate", "钻石胸甲", "装备", "#5BE0D0"),
    ("minecraft:diamond_leggings", "钻石护腿", "装备", "#5BE0D0"),
    ("minecraft:diamond_boots", "钻石靴子", "装备", "#5BE0D0"),
    ("minecraft:netherite_helmet", "下界合金头盔", "装备", "#3A3330"),
    ("minecraft:netherite_chestplate", "下界合金胸甲", "装备", "#3A3330"),
    ("minecraft:netherite_leggings", "下界合金护腿", "装备", "#3A3330"),
    ("minecraft:netherite_boots", "下界合金靴子", "装备", "#3A3330"),
    ("minecraft:leather_helmet", "皮革头盔", "装备", "#B5703A"),
    ("minecraft:elytra", "鞘翅", "装备", "#9A8B6A"),
    ("minecraft:fishing_rod", "钓鱼竿", "工具", "#9A744A"),
    ("minecraft:shears", "剪刀", "工具", "#B7BFC9"),
    ("minecraft:flint_and_steel", "打火石", "工具", "#8A8F98"),
    ("minecraft:compass", "指南针", "工具", "#C9A24B"),
    ("minecraft:clock", "钟表", "工具", "#C9A24B"),
    # ---- 食物 ----
    ("minecraft:apple", "苹果", "食物", "#C24B4B"),
    ("minecraft:golden_apple", "金苹果", "食物", "#F2C94C"),
    ("minecraft:enchanted_golden_apple", "附魔金苹果", "食物", "#F2C94C"),
    ("minecraft:bread", "面包", "食物", "#C9A24B"),
    ("minecraft:cooked_beef", "熟牛排", "食物", "#8A4B3A"),
    ("minecraft:beef", "生牛肉", "食物", "#C26B5A"),
    ("minecraft:cooked_porkchop", "熟猪排", "食物", "#B5703A"),
    ("minecraft:porkchop", "生猪排", "食物", "#D08B7A"),
    ("minecraft:chicken", "鸡肉", "食物", "#E0D6C0"),
    ("minecraft:cooked_chicken", "熟鸡肉", "食物", "#B58B4B"),
    ("minecraft:cake", "蛋糕", "食物", "#E6C9C0"),
    ("minecraft:cookie", "曲奇", "食物", "#B5894E"),
    ("minecraft:carrot", "胡萝卜", "食物", "#E08A3A"),
    ("minecraft:potato", "土豆", "食物", "#C9A24B"),
    ("minecraft:baked_potato", "烤土豆", "食物", "#B5703A"),
    ("minecraft:pumpkin_pie", "南瓜派", "食物", "#E0A23A"),
    ("minecraft:melon_slice", "西瓜片", "食物", "#C24B4B"),
    ("minecraft:sweet_berries", "甜浆果", "食物", "#9B3B5B"),
    ("minecraft:honey_bottle", "蜂蜜瓶", "食物", "#F0C24B"),
    ("minecraft:milk_bucket", "牛奶桶", "食物", "#E6E6E6"),
    # ---- 红石 / 机械 ----
    ("minecraft:redstone_block", "红石块", "红石", "#D63A3A"),
    ("minecraft:piston", "活塞", "红石", "#B7BFC9"),
    ("minecraft:sticky_piston", "粘性活塞", "红石", "#6FC26B"),
    ("minecraft:dispenser", "发射器", "红石", "#8A8F98"),
    ("minecraft:dropper", "投掷器", "红石", "#8A8F98"),
    ("minecraft:hopper", "漏斗", "红石", "#B7BFC9"),
    ("minecraft:observer", "观察者", "红石", "#8A8F98"),
    ("minecraft:repeater", "红石中继器", "红石", "#C9A24B"),
    ("minecraft:comparator", "红石比较器", "红石", "#C9A24B"),
    ("minecraft:redstone_lamp", "红石灯", "红石", "#C9B6D6"),
    ("minecraft:lever", "拉杆", "红石", "#9A744A"),
    ("minecraft:button", "石按钮", "红石", "#8A8F98"),
    ("minecraft:pressure_plate", "石压力板", "红石", "#8A8F98"),
    ("minecraft:daylight_detector", " daylight 探测器".strip(), "红石", "#B7BFC9"),
    ("minecraft:target", "标靶", "红石", "#C24B4B"),
    ("minecraft:tnt", "TNT", "红石", "#C24B4B"),
    ("minecraft:jukebox", "唱片机", "红石", "#9A744A"),
    ("minecraft:note_block", "音符盒", "红石", "#B5894E"),
    ("minecraft:trapped_chest", "陷阱箱", "红石", "#B5703A"),
    # ---- 生物 / 掉落 / 特殊 ----
    ("minecraft:string", "线", "材料", "#E6E6E6"),
    ("minecraft:feather", "羽毛", "生物", "#E6E6E6"),
    ("minecraft:leather", "皮革", "材料", "#B5703A"),
    ("minecraft:rabbit_hide", "兔子皮", "生物", "#C0A88A"),
    ("minecraft:bone", "骨头", "生物", "#E6E6E6"),
    ("minecraft:gunpowder", "火药", "生物", "#8E96A6"),
    ("minecraft:ender_eye", "末影之眼", "材料", "#C9B64B"),
    ("minecraft:blaze_powder", "烈焰粉", "材料", "#E0A23A"),
    ("minecraft:spider_eye", "蜘蛛眼", "生物", "#6B3A5B"),
    ("minecraft:fermented_spider_eye", "发酵蛛眼", "材料", "#6B3A5B"),
    ("minecraft:rotten_flesh", "腐肉", "生物", "#9A6B5A"),
    ("minecraft:spawn_egg", "刷怪蛋", "生物", "#9B5BC7"),
    ("minecraft:egg", "鸡蛋", "生物", "#E6E6E6"),
    ("minecraft:experience_bottle", "经验瓶", "其他", "#7FC2D0"),
    ("minecraft:totem_of_undying", "不死图腾", "其他", "#E0A23A"),
    ("minecraft:dragon_head", "龙首", "其他", "#4E9A52"),
    ("minecraft:creeper_head", "苦力怕头", "其他", "#6FAE5A"),
    ("minecraft:skeleton_skull", "骷髅头", "其他", "#E6E6E6"),
    # ---- 染料 ----
    ("minecraft:white_dye", "白色染料", "材料", "#E6E6E6"),
    ("minecraft:red_dye", "红色染料", "材料", "#C24B4B"),
    ("minecraft:yellow_dye", "黄色染料", "材料", "#F2C94C"),
    ("minecraft:blue_dye", "蓝色染料", "材料", "#4B6FC2"),
    ("minecraft:green_dye", "绿色染料", "材料", "#4E9A52"),
    ("minecraft:black_dye", "黑色染料", "材料", "#2B2B30"),
    ("minecraft:purple_dye", "紫色染料", "材料", "#8B5BC2"),
    ("minecraft:lapis_dye", "青金石染料", "材料", "#3A5BE0"),
    ("minecraft:cocoa_beans", "可可豆", "材料", "#6B3A2A"),
    ("minecraft:ink_sac", "墨囊", "材料", "#2B2B30"),
    ("minecraft:bone_meal", "骨粉", "材料", "#E6E6E6"),
    # ---- 药水 ----
    ("minecraft:glass_bottle", "玻璃瓶", "药水", "#BBD6E0"),
    ("minecraft:potion", "药水", "药水", "#9B5BC7"),
    ("minecraft:splash_potion", "喷溅药水", "药水", "#9B5BC7"),
    ("minecraft:lingering_potion", "滞留药水", "药水", "#9B5BC7"),
    ("minecraft:brewing_stand", "酿造台", "红石", "#9A744A"),
    ("minecraft:cauldron", "炼药锅", "红石", "#B7BFC9"),
    ("minecraft:nether_wart", "地狱疣", "材料", "#9B3B5B"),
    ("minecraft:blaze_powder", "烈焰粉", "材料", "#E0A23A"),
    # ---- 其他常用 ----
    ("minecraft:paper", "纸", "材料", "#E6E6E6"),
    ("minecraft:book", "书", "材料", "#C9A24B"),
    ("minecraft:writable_book", "可写书", "材料", "#C9A24B"),
    ("minecraft:enchanted_book", "附魔书", "材料", "#7C8BD0"),
    ("minecraft:chest", "箱子", "装饰", "#B5703A"),
    ("minecraft:crafting_table", "工作台", "装饰", "#9A744A"),
    ("minecraft:furnace", "熔炉", "装饰", "#8A8F98"),
    ("minecraft:torch", "火把", "装饰", "#E0A23A"),
    ("minecraft:soul_torch", "灵魂火把", "装饰", "#3A8FE0"),
    ("minecraft:lantern", "灯笼", "装饰", "#E0A23A"),
    ("minecraft:soul_lantern", "灵魂灯笼", "装饰", "#3A8FE0"),
    ("minecraft:ender_chest", "末影箱", "装饰", "#3A5BE0"),
    ("minecraft:anvil", "铁砧", "装饰", "#B7BFC9"),
    ("minecraft:cartography_table", "制图台", "装饰", "#9A744A"),
    ("minecraft:fletching_table", "制箭台", "装饰", "#9A744A"),
    ("minecraft:smithing_table", "锻造台", "装饰", "#8A8F98"),
    ("minecraft:grindstone", "砂轮", "装饰", "#8A8F98"),
    ("minecraft:bell", "钟", "装饰", "#C9A24B"),
    ("minecraft:barrel", "木桶", "装饰", "#9A744A"),
    ("minecraft:loom", "织布机", "装饰", "#9A744A"),
    ("minecraft:composter", "堆肥桶", "装饰", "#9A744A"),
    ("minecraft:honeycomb", "蜂巢", "自然", "#E0A23A"),
    ("minecraft:honeycomb_block", "蜜脾块", "装饰", "#E0A23A"),
    ("minecraft:bee_nest", "蜂巢", "自然", "#9A744A"),
    ("minecraft:ice", "冰", "自然", "#BBD6E0"),
    ("minecraft:packed_ice", "浮冰", "自然", "#9FD6E0"),
    ("minecraft:blue_ice", "蓝冰", "自然", "#5A8FE0"),
    ("minecraft:snow_block", "雪块", "自然", "#E6E6E6"),
    ("minecraft:magma_block", "岩浆块", "自然", "#C24B3A"),
    ("minecraft:soul_sand", "灵魂沙", "自然", "#7A6B5A"),
    ("minecraft:soul_soil", "灵魂土", "自然", "#6B5A4A"),
    ("minecraft:netherrack", "下界岩", "方块", "#5A2A2A"),
    ("minecraft:basalt", "玄武岩", "方块", "#3F3F47"),
    ("minecraft:blackstone", "黑石", "方块", "#2B2B30"),
    ("minecraft:gilded_blackstone", "镶金黑石", "方块", "#5A4A2A"),
    ("minecraft:crying_obsidian", "哭泣的黑曜石", "方块", "#3A2A5A"),
    ("minecraft:ancient_debris", "远古残骸", "矿石", "#3A3530"),
    ("minecraft:slime_block", "黏液块", "自然", "#6FC26B"),
    ("minecraft:honey_block", "蜂蜜块", "其他", "#F0C24B"),
    ("minecraft:lodestone", "磁石", "方块", "#9A6B5A"),
    ("minecraft:respawn_anchor", "重生锚", "方块", "#5A2A4A"),
    ("minecraft:chain", "锁链", "装饰", "#B7BFC9"),
    ("minecraft:pointed_dripstone", "滴水石锥", "自然", "#C9B6A0"),
    ("minecraft:amethyst_block", "紫水晶块", "方块", "#B57BC2"),
    ("minecraft:budding_amethyst", "含芽紫水晶", "方块", "#B57BC2"),
    ("minecraft:calcite", "方解石", "方块", "#E6E0D6"),
    ("minecraft:tuff", "凝灰岩", "方块", "#6B6B6B"),
    ("minecraft:copper_block", "铜块", "方块", "#C77B45"),
    ("minecraft:raw_iron_block", "粗铁块", "方块", "#9A6B4A"),
    ("minecraft:raw_gold_block", "粗金块", "方块", "#B5902A"),
    ("minecraft:raw_copper_block", "粗铜块", "方块", "#9A5A2A"),
    ("minecraft:waxed_copper_block", "涂蜡铜块", "方块", "#C77B45"),
    ("minecraft:lightning_rod", "避雷针", "红石", "#C9A24B"),
    ("minecraft:spyglass", "望远镜", "工具", "#9A744A"),
]


def _color_for(item):
    c = item.get("color")
    if c:
        return c
    # 稳定哈希配色
    s = item["id"].encode("utf-8")
    h = 0
    for b in s:
        h = (h * 31 + b) & 0xFFFFFFFF
    hue = h % 360
    return f"hsl({hue}, 38%, 55%)"


ITEMS = []
for _id, _zh, _cat, *_rest in _RAW:
    _color = _rest[0] if _rest else None
    ITEMS.append({"id": _id, "zh": _zh, "cat": _cat, "color": _color_for({"id": _id, "color": _color})})

# 分类顺序（用于选择器分组）
_CATS = []
for it in ITEMS:
    if it["cat"] not in _CATS:
        _CATS.append(it["cat"])
CATEGORIES = _CATS


def search_items(query, limit=400):
    """按中文名 / id 模糊搜索物品。"""
    q = (query or "").strip().lower()
    if not q:
        return list(ITEMS[:limit])
    out = []
    # 前缀优先
    starts = []
    contains = []
    for it in ITEMS:
        zh = it["zh"].lower()
        iid = it["id"].lower()
        if zh.startswith(q) or iid.endswith(q):
            starts.append(it)
        elif q in zh or q in iid:
            contains.append(it)
    out = starts + contains
    return out[:limit]


def item_label(item):
    """图标上的简短标签（取中文名前 2 字，过长截断）。"""
    zh = item["zh"]
    if len(zh) <= 2:
        return zh
    return zh[:2]


def get_item(item_id):
    for it in ITEMS:
        if it["id"] == item_id:
            return it
    return None



# ---- mc_textures.py ----
# -*- coding: utf-8 -*-
"""运行时材质查询：把 textures/map.json 的映射暴露给 UI。

提供：
    has_texture(item_id) -> bool
    texture_path(item_id) -> 绝对路径或 None
    pixmap(item_id, size) -> 缩放后的 QPixmap 或 None（用于显示真实材质）
材质来自本地 Minecraft 客户端 jar（官方原版），离线可用。
"""
import os
import json

from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt

BASE = _resource_dir()
TEXTURES_DIR = os.path.join(BASE, "textures")
MAP_PATH = os.path.join(TEXTURES_DIR, "map.json")

_map = None
_pix_cache = {}


def _load_map():
    global _map
    if _map is None:
        try:
            with open(MAP_PATH, encoding="utf-8") as f:
                _map = json.load(f)
        except Exception:
            _map = {}
    return _map


def has_texture(item_id):
    rel = _load_map().get(item_id)
    if not rel:
        return False
    return os.path.exists(os.path.join(TEXTURES_DIR, *rel.split("/")))


def texture_path(item_id):
    rel = _load_map().get(item_id)
    if not rel:
        return None
    p = os.path.join(TEXTURES_DIR, *rel.split("/"))
    return p if os.path.exists(p) else None


def pixmap(item_id, size=48, smooth=True):
    """返回缩放后的材质 QPixmap；无材质返回 None。"""
    p = texture_path(item_id)
    if not p:
        return None
    key = (item_id, size, smooth)
    if key in _pix_cache:
        return _pix_cache[key]
    px = QPixmap(p)
    if px.isNull():
        _pix_cache[key] = None
        return None
    mode = (Qt.TransformationMode.SmoothTransformation
            if smooth else Qt.TransformationMode.FastTransformation)
    px = px.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, mode)
    _pix_cache[key] = px
    return px

# ---- seedmap_engine.py ----
# -*- coding: utf-8 -*-
"""种子地图引擎（基于 cubiomes，真实还原 Minecraft 世界生成）。

提供：
  - 生物群系地图生成（主世界 / 下界 / 末地），渲染按种子精确还原
  - 结构定位（village / 沙漠神殿 / 丛林神庙 / 女巫小屋 / 雪屋 / 海底神殿 /
    海底废墟 / 沉船 / 掠夺者前哨 / 林地府邸 / 废墟传送门 / 下界要塞 /
    堡垒遗迹 / 末地城 / 要塞），坐标与游戏内一致
  - 出生点查找（cubiomes 真实出生点）
  - 读取 Minecraft 存档 level.dat 的种子（极简 NBT 解析）
  - 种子解析（与 Minecraft 一致：数字用 long，否则用 String.hashCode）

底层由 cubiomes（C 库）驱动，通过 cubiomespi 调用。生物群系为逐方块
精确还原；由于 cubiomespi 的 get_biome_at 为逐方块调用（每次约 0.25ms），
地图采用「分块网格采样 + 多线程」策略，在保持精度的同时把整图生成控制在
约 1~2 秒（并先出低分辨率预览）。
"""
import math
import os
import random
import threading
import time


def _make_missing_module(mod_name, err):
    """为「加载失败的原生模块」生成占位对象。

    作用：numpy / cubiomespi 都带原生库（.dll），在打包成 exe 时可能因
    依赖缺失、杀软拦截等原因加载失败。若直接 `import` 抛异常，整个启动器
    会在显示窗口之前就崩溃（黑框一闪而过）。这里改为占位对象：导入不再
    致命，只有真正用到该模块的功能才抛出带说明的中文错误。
    """
    class _MissingModule:
        def __getattr__(self, name):
            raise RuntimeError(
                f"{mod_name} 模块不可用（{err!r}）；"
                f"相关功能（离线种子地图）已停用，请改用「🌐 网页地图」。")

    return _MissingModule()


# numpy：原生扩展，打包环境下有可能加载失败
try:
    import numpy as np
except Exception as _e_np:
    np = _make_missing_module("numpy", _e_np)
    print("[DBG] numpy 加载失败:", repr(_e_np), flush=True)

# cubiomespi：内部通过 ctypes 加载 lib.dll，打包环境下有可能加载失败
try:
    import cubiomespi as cp
    _HAVE_CUBIOMES = True
except Exception as _e_cp:
    cp = _make_missing_module("cubiomespi", _e_cp)
    _HAVE_CUBIOMES = False
    print("[DBG] cubiomespi 加载失败:", repr(_e_cp), flush=True)

MASK64 = (1 << 64) - 1

# 内部并行线程数（cubiomes 的 get_biome_at 为 C 调用，ctypes 会释放 GIL，
# 可在多个线程中并发；但嵌套在 Qt 的 QThread 工作线程里时过多线程易死锁，
# 故默认设为 1，通过环境变量 SEEDMAP_THREADS 可调）。
_MAX_THREADS = int(os.environ.get("SEEDMAP_THREADS", "1"))

# --------------------------------------------------------------------------
# 版本 / 维度映射
# --------------------------------------------------------------------------
def _mc_version(vstr):
    """UI 版本字符串 -> cubiomes MCVersion 整数。"""
    if not vstr:
        return cp.MCVersion.MC_1_20
    s = str(vstr)
    if s >= "1.21":
        return cp.MCVersion.MC_1_20          # cubiomespi 最高到 1.20
    if s.startswith("1.20"):
        return cp.MCVersion.MC_1_20
    if s.startswith("1.19"):
        return cp.MCVersion.MC_1_19_2
    if s.startswith("1.18"):
        return cp.MCVersion.MC_1_18_2
    if s.startswith("1.17"):
        return cp.MCVersion.MC_1_17_1
    if s.startswith("1.16"):
        return cp.MCVersion.MC_1_16_5
    return cp.MCVersion.MC_1_20


def _dim_int(dim):
    return {"overworld": cp.Dimension.DIM_OVERWORLD,
            "nether": cp.Dimension.DIM_NETHER,
            "end": cp.Dimension.DIM_END}.get(dim, cp.Dimension.DIM_OVERWORLD)


# --------------------------------------------------------------------------
# Generator 缓存（只读，可跨线程共享）
# --------------------------------------------------------------------------
_GEN_LOCK = threading.Lock()
_GEN_CACHE = {}


def _get_gen(seed, vstr, dim):
    seed_u = int(seed) & MASK64
    vint = _mc_version(vstr)
    dimint = _dim_int(dim)
    key = (seed_u, vint, dimint)
    with _GEN_LOCK:
        g = _GEN_CACHE.get(key)
        if g is None:
            g = cp.Generator(vint, seed_u, dimint)
            _GEN_CACHE[key] = g
        return g


# --------------------------------------------------------------------------
# 生物群系：id -> 名称 / 颜色
# --------------------------------------------------------------------------
try:
    _REV_BIOME = {int(getattr(cp.BiomeID, n)): n
                  for n in dir(cp.BiomeID) if not n.startswith("_")}
except Exception as _e_biome:
    _REV_BIOME = {}
    print("[DBG] 群系名称表初始化失败:", repr(_e_biome), flush=True)


def _style_color(name):
    n = name.lower()
    # 海洋 / 河流
    if "deep" in n and "ocean" in n:
        return (26, 42, 110)
    if "frozen_ocean" in n or n in ("frozenocean",):
        return (120, 160, 200)
    if "ocean" in n:
        return (54, 102, 196)
    if "frozen_river" in n:
        return (150, 200, 230)
    if "river" in n:
        return (70, 150, 220)
    # 沙滩 / 海岸
    if "beach" in n or "shore" in n:
        return (214, 205, 165)
    # 下界
    if "soul_sand" in n:
        return (90, 75, 60)
    if "crimson" in n:
        return (140, 50, 90)
    if "warped" in n:
        return (40, 110, 110)
    if "basalt" in n:
        return (60, 60, 70)
    if "nether" in n or n in ("hell",):
        return (120, 45, 45)
    if "lava" in n:
        return (230, 90, 20)
    # 末地
    if n in ("the_end", "sky", "end_stone"):
        return (190, 190, 185)
    if "end" in n:
        return (160, 170, 165)
    if "void" in n:
        return (20, 20, 28)
    # 雪原
    if "snowy" in n or "ice" in n or n in ("iceplains", "snowy_tundra",
                                           "snowy_plains"):
        return (225, 235, 240)
    if "ice_spikes" in n:
        return (200, 225, 240)
    # 沙漠
    if "desert" in n:
        return (222, 205, 150)
    # 热带草原
    if "savanna" in n:
        return (170, 160, 70)
    # 恶地
    if "badlands" in n or "mesa" in n or "badland" in n:
        return (190, 110, 60)
    # 山地
    if ("mountain" in n or "extremehills" in n or "windswept" in n
            or "gravelly" in n or "stony" in n):
        return (120, 120, 120)
    # 针叶林
    if "taiga" in n or "giant_tree" in n or "mega_taiga" in n or "spruce" in n:
        return (52, 95, 58)
    # 黑森林
    if "dark_forest" in n or "roofedforest" in n:
        return (30, 80, 45)
    # 森林
    if "forest" in n or "birch" in n:
        return (40, 120, 55)
    # 丛林
    if "jungle" in n:
        return (60, 110, 55)
    # 沼泽
    if "swamp" in n or "mangrove" in n:
        return (70, 100, 70)
    # 平原
    if "plains" in n or "meadow" in n or "sunflower" in n:
        return (128, 176, 80)
    # 洞穴
    if "lush" in n:
        return (40, 140, 90)
    if "dripstone" in n:
        return (140, 110, 90)
    if "deep_dark" in n:
        return (50, 50, 70)
    # 其他
    if "grove" in n:
        return (180, 200, 210)
    if "frozen_peaks" in n or "jagged_peaks" in n or "snowy_slopes" in n:
        return (200, 205, 210)
    if "cherry" in n:
        return (230, 160, 190)
    if "mushroom" in n:
        return (150, 120, 200)
    return (150, 150, 150)


def _style_name(name):
    n = name.lower()
    if "deep" in n and "ocean" in n:
        return "深海"
    if "frozen_ocean" in n or n == "frozenocean":
        return "冷冻海洋"
    if "ocean" in n:
        return "海洋"
    if "frozen_river" in n:
        return "冷冻河流"
    if "river" in n:
        return "河流"
    if "beach" in n or "shore" in n:
        return "沙滩"
    if "soul_sand" in n:
        return "灵魂沙峡谷"
    if "crimson" in n:
        return "绯红森林"
    if "warped" in n:
        return "诡异森林"
    if "basalt" in n:
        return "玄武岩三角洲"
    if "nether" in n or n == "hell":
        return "地狱荒野"
    if "lava" in n:
        return "熔岩"
    if n in ("the_end", "sky", "end_stone"):
        return "末地石岛"
    if "end" in n:
        return "末地"
    if "void" in n:
        return "虚空"
    if "ice_spikes" in n:
        return "冰刺"
    if "snowy" in n or "ice" in n or n in ("iceplains", "snowy_tundra",
                                           "snowy_plains"):
        return "雪原"
    if "desert" in n:
        return "沙漠"
    if "savanna" in n:
        return "热带草原"
    if "badlands" in n or "mesa" in n:
        return "恶地"
    if ("mountain" in n or "extremehills" in n or "windswept" in n
            or "gravelly" in n or "stony" in n):
        return "山地"
    if "taiga" in n or "giant_tree" in n or "mega_taiga" in n or "spruce" in n:
        return "针叶林"
    if "dark_forest" in n or "roofedforest" in n:
        return "黑森林"
    if "forest" in n or "birch" in n:
        return "森林"
    if "jungle" in n:
        return "丛林"
    if "swamp" in n or "mangrove" in n:
        return "沼泽"
    if "plains" in n or "meadow" in n or "sunflower" in n:
        return "平原"
    if "lush" in n:
        return "繁茂洞穴"
    if "dripstone" in n:
        return "滴水石洞穴"
    if "deep_dark" in n:
        return "深暗之域"
    if "grove" in n:
        return "树丛"
    if "frozen_peaks" in n or "jagged_peaks" in n or "snowy_slopes" in n:
        return "山峰"
    if "cherry" in n:
        return "樱花树林"
    if "mushroom" in n:
        return "蘑菇岛"
    return "未知群系"


_MAX_BID = 256
try:
    _BIOME_RGB = np.zeros((_MAX_BID, 3), dtype=np.uint8)
    for _b in range(_MAX_BID):
        _BIOME_RGB[_b] = _style_color(_REV_BIOME.get(_b, "unknown"))
except Exception as _e_rgb:
    _BIOME_RGB = None
    print("[DBG] 群系颜色表初始化失败:", repr(_e_rgb), flush=True)


def _biome_color(bid):
    return tuple(int(v) for v in _BIOME_RGB[int(bid) % _MAX_BID])


def _biome_name_cn(bid):
    return _style_name(_REV_BIOME.get(int(bid) % _MAX_BID, "unknown"))


def biome_cn_name(bid):
    """纯 Python：生物群系 id -> 中文名（用于悬停，无需 cubiomes C 调用）。"""
    return _biome_name_cn(int(bid))


# --------------------------------------------------------------------------
# 生物群系地图生成（分块网格采样 + 多线程）
# --------------------------------------------------------------------------
def generate_biome_rgb(seed, cx, cz, W, H, bpp, dimension, version="1.20.1",
                     target=20000):
    """生成 (H, W, 3) uint8 的地图截图（cubiomes 精确还原）。

    seed      : int 种子（已解析为 long）
    cx, cz    : 中心方块坐标
    W, H      : 像素尺寸
    bpp       : 每像素代表多少方块（缩放）
    dimension : 'overworld' / 'nether' / 'end'
    version   : Minecraft 版本字符串
    """
    seed_u = int(seed) & MASK64
    vint = _mc_version(version)
    dimint = _dim_int(dimension)
    y = 64 if dimension == "nether" else 0

    block_w = W * bpp
    block_h = H * bpp
    bx0 = cx - block_w / 2.0
    bz0 = cz - block_h / 2.0

    # 采样步长：让总采样数维持在 ~target，避免逐像素调用过慢
    step = max(4, int(round(bpp * math.sqrt(W * H / max(1, target)))))
    step = max(1, step)

    g = _get_gen(seed_u, version, dimension)
    gx = max(2, int(block_w // step) + 1)
    gz = max(2, int(block_h // step) + 1)

    sx = np.arange(gx, dtype=np.float64) * step + bx0
    sz = np.arange(gz, dtype=np.float64) * step + bz0
    bids = np.empty((gz, gx), dtype=np.int32)

    def work(i0, i1):
        for i in range(i0, i1):
            x = int(round(sx[i]))
            for j in range(gz):
                bids[j, i] = cp.get_biome_at(g, x, y, int(round(sz[j])))

    nthreads = min(_MAX_THREADS, max(1, gx))
    if nthreads <= 1:
        work(0, gx)
    else:
        threads = []
        span = (gx + nthreads - 1) // nthreads
        for t in range(nthreads):
            a = t * span
            b = min(a + span, gx)
            th = threading.Thread(target=work, args=(a, b))
            th.start()
            threads.append(th)
        for th in threads:
            th.join()

    # 像素 -> 采样网格索引
    px = (np.arange(W, dtype=np.float64) - W / 2.0) * bpp + cx
    pz = (np.arange(H, dtype=np.float64) - H / 2.0) * bpp + cz
    ci = np.clip(np.round((px - bx0) / step).astype(np.int32), 0, gx - 1)
    cj = np.clip(np.round((pz - bz0) / step).astype(np.int32), 0, gz - 1)
    ci = np.broadcast_to(ci, (H, W))
    cj = np.broadcast_to(cj[:, None], (H, W))
    flat = bids[cj.ravel(), ci.ravel()]
    rgb = _BIOME_RGB[np.clip(flat, 0, _MAX_BID - 1)].reshape(H, W, 3).copy()
    # 与 rgb 同分辨率的生物群系 id 网格（用于 UI 端悬停查名，避免再调用 cubiomes）
    bid_grid = flat.reshape(H, W).astype(np.int32)
    return rgb, bid_grid


def downscale_rgb(rgb, pw, ph):
    """把 (H, W, 3) 地图按最近邻降采样到 (ph, pw, 3)，用于派生低分辨率预览。

    纯 numpy 操作，无任何 cubiomes C 调用，可安全在 Qt 工作线程中执行。
    """
    h, w = rgb.shape[:2]
    if pw >= w and ph >= h:
        return rgb.copy()
    xs = np.linspace(0, w - 1, pw).round().astype(np.int32)
    ys = np.linspace(0, h - 1, ph).round().astype(np.int32)
    return rgb[ys][:, xs]


def biome_name_at(seed, x, z, dimension, version="1.20.1"):
    """返回某方块坐标处的生物群系中文名（cubiomes 精确）。"""
    seed_u = int(seed) & MASK64
    y = 64 if dimension == "nether" else 0
    g = _get_gen(seed_u, version, dimension)
    bid = cp.get_biome_at(g, int(x), y, int(z))
    return _biome_name_cn(bid)


# --------------------------------------------------------------------------
# 结构定位（cubiomes 精确）
# --------------------------------------------------------------------------
try:
    STRUCT_TO_CUBIOMES = {
        "village":         {"struct": cp.Structure.Village, "dim": "overworld"},
        "desert_pyramid":  {"struct": cp.Structure.Desert_Pyramid, "dim": "overworld"},
        "jungle_temple":   {"struct": cp.Structure.Jungle_Temple, "dim": "overworld"},
        "swamp_hut":       {"struct": cp.Structure.Swamp_Hut, "dim": "overworld"},
        "igloo":           {"struct": cp.Structure.Igloo, "dim": "overworld"},
        "monument":        {"struct": cp.Structure.Monument, "dim": "overworld"},
        "ocean_ruin":      {"struct": cp.Structure.Ocean_Ruin, "dim": "overworld"},
        "shipwreck":       {"struct": cp.Structure.Shipwreck, "dim": "overworld"},
        "pillager":        {"struct": cp.Structure.Outpost, "dim": "overworld"},
        "mansion":         {"struct": cp.Structure.Mansion, "dim": "overworld"},
        "ruined_portal":   {"struct": cp.Structure.Ruined_Portal, "dim": "overworld"},
        "fortress":        {"struct": cp.Structure.Fortress, "dim": "nether"},
        "bastion":         {"struct": cp.Structure.Bastion, "dim": "nether"},
        "end_city":        {"struct": cp.Structure.End_City, "dim": "end"},
    }
except Exception as _e_struct:
    STRUCT_TO_CUBIOMES = {}
    print("[DBG] 结构映射表初始化失败:", repr(_e_struct), flush=True)

# UI 使用的结构字典：键与页面 STRUCT_GROUPS 对应；name/color 用于标记绘制
STRUCTURES = {
    "village":        {"name": "村庄", "color": (224, 196, 70)},
    "desert_pyramid": {"name": "沙漠神殿", "color": (214, 178, 92)},
    "jungle_temple":  {"name": "丛林神庙", "color": (92, 160, 80)},
    "swamp_hut":      {"name": "女巫小屋", "color": (120, 90, 150)},
    "igloo":          {"name": "雪屋", "color": (210, 220, 230)},
    "monument":       {"name": "海底神殿", "color": (60, 160, 200)},
    "ocean_ruin":     {"name": "海底废墟", "color": (110, 140, 170)},
    "shipwreck":      {"name": "沉船", "color": (150, 110, 70)},
    "pillager":       {"name": "掠夺者前哨", "color": (150, 60, 60)},
    "mansion":        {"name": "林地府邸", "color": (120, 80, 170)},
    "ruined_portal":  {"name": "废墟传送门", "color": (170, 60, 200)},
    "stronghold":     {"name": "要塞", "color": (230, 230, 120)},
    "fortress":       {"name": "下界要塞", "color": (200, 70, 40)},
    "bastion":        {"name": "堡垒遗迹", "color": (200, 120, 40)},
    "end_city":       {"name": "末地城", "color": (220, 220, 200)},
}


def structure_positions(seed, name, min_cx, max_cx, min_cz, max_cz,
                        version="1.20.1"):
    """返回某结构在 [min_cx..max_cx]×[min_cz..max_cz] 区块范围内的**方块**坐标列表。

    min_cx..max_cx / min_cz..max_cz 为区块（chunk）坐标。
    """
    info = STRUCT_TO_CUBIOMES.get(name)
    if info is None:
        return []
    seed_u = int(seed) & MASK64
    g = _get_gen(seed_u, version, info["dim"])
    # cubiomes 的 find_structure_in_range 使用 region 坐标（512 方块 = 32 区块）
    rminx = min_cx // 32 - 1
    rmaxx = max_cx // 32 + 1
    rminz = min_cz // 32 - 1
    rmaxz = max_cz // 32 + 1
    res = cp.find_structure_in_range(g, info["struct"], rminx, rminz, rmaxx, rmaxz)
    if not res:
        return []
    # 直接返回（已按 region 扩大一圈搜索）；画布会再用视野边界过滤，
    # 此处不过度裁剪，避免把刚好在视野边缘的结构丢掉。
    return [(int(bx), int(bz)) for (bx, bz) in res]


def stronghold_positions(seed, version="1.20.1", max_count=128):
    """返回全部要塞的方块坐标列表（与游戏内一致）。

    要塞位置对给定种子是确定不变的，故按 (种子, 版本) 缓存，避免每次重绘都
    重新计算 128 个要塞（cubiomes 计算较慢）。
    """
    seed_u = int(seed) & MASK64
    cache_key = (seed_u, _mc_version(version))
    cached = _STRONGHOLD_CACHE.get(cache_key)
    if cached is not None:
        return cached
    g = _get_gen(seed_u, version, "overworld")
    pts = cp.get_stronghold_pos(g, max_count)
    out = [(int(p[0]), int(p[1])) for p in (pts or [])]
    _STRONGHOLD_CACHE[cache_key] = out
    return out


_STRONGHOLD_CACHE = {}


def find_spawn(seed, version="1.20.1"):
    """返回世界出生点方块坐标 (x, z)（cubiomes 真实出生点）。"""
    seed_u = int(seed) & MASK64
    g = _get_gen(seed_u, version, "overworld")
    x, z = cp.get_spawn_pos(g)
    return (int(x), int(z))


def search_biome_near(seed, biome_cn, cx, cz, radius, step, dimension,
                      version="1.20.1"):
    """在 [cx-radius, cx+radius]×[cz-radius, cz+radius] 内逐点采样，返回命中
    目标群系中文名 == biome_cn 的方块坐标列表。

    注意：内含 cubiomes C 调用，只能在子进程内执行（主线程会死锁）。
    """
    seed_u = int(seed) & MASK64
    y = 64 if dimension == "nether" else 0
    g = _get_gen(seed_u, version, dimension)
    found = []
    bx = cx - radius
    while bx <= cx + radius:
        bz = cz - radius
        while bz <= cz + radius:
            bid = cp.get_biome_at(g, int(bx), y, int(bz))
            if _biome_name_cn(bid) == biome_cn:
                found.append((int(bx), int(bz)))
            bz += step
        bx += step
    return found


# --------------------------------------------------------------------------
# 种子搜索（MinecraftSearch）：扫描大量种子，返回满足条件的结果
# --------------------------------------------------------------------------
def _new_gen(seed_u, version, dim):
    """新建一个 Generator（不进缓存，便于大规模扫描时控制内存）。"""
    return cp.Generator(_mc_version(version), int(seed_u) & MASK64, _dim_int(dim))


def _find_seeds_structure(name, version, radius, max_tries, max_results, rng):
    info = STRUCT_TO_CUBIOMES.get(name)
    if info is None:
        return []
    dim, struct = info["dim"], info["struct"]
    out = []
    tries = 0
    while tries < max_tries and len(out) < max_results:
        tries += 1
        seed_u = rng.randrange(0, 1 << 63)
        g = _new_gen(seed_u, version, dim)
        if dim == "overworld":
            sx, sz = cp.get_spawn_pos(g)
        else:
            sx, sz = 0, 0
        rc = int(math.ceil(radius / 512.0)) + 1
        rx, rz = int(sx) // 512, int(sz) // 512
        res = cp.find_structure_in_range(g, struct, rx - rc, rz - rc,
                                         rx + rc, rz + rc)
        best = None
        for (bx, bz) in res:
            d = math.hypot(bx - sx, bz - sz)
            if d <= radius and (best is None or d < best[0]):
                best = (d, bx, bz)
        if best:
            out.append((seed_u,
                        f"{STRUCTURES[name]['name']} @ ({best[1]}, {best[2]})，"
                        f"距出生点 {int(best[0])} 方块"))
    return out


def _find_seeds_biome(cn, version, radius, max_tries, max_results, rng):
    out = []
    tries = 0
    r = int(radius)
    # 仅采样出生点 + 半径为 r 的 8 个方位点（共 9 点），保证扫描够快
    pts = [(0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
           (r, r), (-r, r), (r, -r), (-r, -r)]
    while tries < max_tries and len(out) < max_results:
        tries += 1
        seed_u = rng.randrange(0, 1 << 63)
        g = _new_gen(seed_u, version, "overworld")
        sx, sz = cp.get_spawn_pos(g)
        hit = None
        for (dx, dz) in pts:
            bid = cp.get_biome_at(g, int(sx + dx), 0, int(sz + dz))
            if _biome_name_cn(bid) == cn:
                hit = (int(sx + dx), int(sz + dz))
                break
        if hit:
            out.append((seed_u, f"出生点附近有「{cn}」 @ ({hit[0]}, {hit[1]})"))
    return out


def find_seeds(criterion, version="1.20.1", max_tries=1500, seed_start=None,
               max_results=60):
    """扫描种子，返回满足条件的结果列表 [(seed_int, info_str), ...]。

    criterion:
      - {"kind": "structure", "name": <STRUCT_TO_CUBIOMES 键>, "radius": <方块>}
      - {"kind": "biome", "biome": <中文群系名>, "radius": <方块>}
    随机种子由 seed_start 决定（默认随机）。整个扫描在 cubiomes 子进程内执行。
    """
    rng = random.Random(seed_start if seed_start is not None
                        else int(time.time() * 1000) ^ os.getpid())
    if criterion.get("kind") == "structure":
        return _find_seeds_structure(criterion["name"], version,
                                     int(criterion.get("radius", 1500)),
                                     max_tries, max_results, rng)
    if criterion.get("kind") == "biome":
        return _find_seeds_biome(criterion["biome"], version,
                                 int(criterion.get("radius", 800)),
                                 max_tries, max_results, rng)
    return []


# --------------------------------------------------------------------------
# 种子解析（与 Minecraft 一致）
# --------------------------------------------------------------------------
def parse_seed(text):
    text = (text or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return java_string_hash(text)


def java_string_hash(s):
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h


# --------------------------------------------------------------------------
# 读取 level.dat 的种子（极简 NBT 解析）
# --------------------------------------------------------------------------
def _read_nbt(data, pos=0):
    tag = data[pos]
    pos += 1
    if tag == 0:
        return None, pos
    if tag in (9, 10):
        name_len = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2 + name_len
    return _read_nbt_value(data, tag, pos)


def _read_nbt_value(data, tag, pos):
    if tag == 1:
        return int(data[pos]), pos + 1
    if tag == 2:
        return int.from_bytes(data[pos:pos + 2], "little", signed=True), pos + 2
    if tag == 3:
        return int.from_bytes(data[pos:pos + 4], "little", signed=True), pos + 4
    if tag == 4:
        return int.from_bytes(data[pos:pos + 8], "little", signed=True), pos + 8
    if tag == 5:
        return None, pos + 4
    if tag == 6:
        return None, pos + 8
    if tag == 7:
        n = int.from_bytes(data[pos:pos + 4], "little"); return None, pos + 4 + n
    if tag == 8:
        ln = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
        s = data[pos:pos + ln].decode("utf-8", "replace"); return s, pos + ln
    if tag == 9:
        elem_tag = data[pos]; pos += 1
        n = int.from_bytes(data[pos:pos + 4], "little"); pos += 4
        items = []
        for _ in range(n):
            if elem_tag == 10:
                d, pos = _read_compound(data, pos)
                items.append(d)
            elif elem_tag == 0:
                items.append(None)
            else:
                v, pos = _read_nbt_value(data, elem_tag, pos)
                items.append(v)
        return items, pos
    if tag == 10:
        return _read_compound(data, pos)
    if tag == 11:
        n = int.from_bytes(data[pos:pos + 4], "little"); pos += 4
        return None, pos + 4 + n * 4
    if tag == 12:
        n = int.from_bytes(data[pos:pos + 4], "little"); pos += 4
        return None, pos + 4 + n * 8
    return None, pos


def _read_compound(data, pos):
    d = {}
    while True:
        tag = data[pos]; pos += 1
        if tag == 0:
            break
        name_len = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
        name = data[pos:pos + name_len].decode("utf-8", "replace"); pos += name_len
        if tag == 10:
            v, pos = _read_compound(data, pos)
        elif tag == 9:
            v, pos = _read_nbt_value(data, 9, pos)
        else:
            v, pos = _read_nbt_value(data, tag, pos)
        d[name] = v
    return d, pos


def read_level_seed(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    try:
        root, _ = _read_compound(data, 0)
        data_node = root.get("Data")
        if isinstance(data_node, dict) and "Seed" in data_node:
            return int(data_node["Seed"])
        if "Seed" in root:
            return int(root["Seed"])
    except Exception:
        pass
    return None


# ---- recipe_editor.py ----
# -*- coding: utf-8 -*-
"""Minecraft 配方生成器（可视化合成配方编辑器）。

仿照 AxoMort / Axolotl 等工具的「配方生成器」：
- 左侧：配方列表（新建 / 编辑 / 删除 / 导出数据包）
- 中部：选项（类型 / 分组 / 分类 / 文件名 / 无序 / 2x2 / 精确位置 / 配方通知）
        + 合成格预览（3x3 或 2x2）+ 产物槽 + 实时校验 + 实时 JSON + 保存 JSON
- 右侧：物品选择器（版本 / 物品·标签分页 / 搜索 / 自定义物品 / 物品网格）

所有配方均输出标准 Minecraft recipe JSON，并可一键导出数据包（.zip）。

坐标 / 控件均使用全局 QSS（Card、GhostButton、PrimaryButton 等 objectName），
颜色随主题（明/暗）自适应——需要具体颜色时读取 QApplication 调色板。
"""

import json
import os
import re
import zipfile

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QCheckBox, QSpinBox, QTextEdit, QListWidget, QListWidgetItem,
    QDialog, QFileDialog, QMessageBox, QFrame, QTabWidget, QScrollArea,
    QSizePolicy, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPalette, QPixmap, QClipboard, QFont

# --------------------------------------------------------------------------
# 小工具：复刻 PlainMC 的卡片 / 幽灵按钮 / 主按钮样式（靠全局 QSS 上色）
# --------------------------------------------------------------------------

def _card():
    c = QFrame()
    c.setObjectName("Card")
    return c


def _ghost(text):
    b = QPushButton(text)
    b.setObjectName("GhostButton")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def _primary(text):
    b = QPushButton(text)
    b.setObjectName("PrimaryButton")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def _accent():
    """用于强调色的十六进制（粉/紫），明暗主题都醒目。"""
    return "#FF5FA2"


def _surface_color(role=QPalette.ColorRole.Base):
    app = QApplication.instance()
    if app is None:
        return QColor("#262B3A")
    return app.palette().color(role)


def _text_color():
    app = QApplication.instance()
    if app is None:
        return QColor("#E6E9F2")
    return app.palette().color(QPalette.ColorRole.WindowText)


# --------------------------------------------------------------------------
# 版本 -> 数据包 pack_format 映射（用于导出）
# --------------------------------------------------------------------------

PACK_FORMATS = [
    ("Java 1.21.x", 48),
    ("Java 1.20.5–1.20.6", 32),
    ("Java 1.20.2–1.20.4", 26),
    ("Java 1.20–1.20.1", 18),
    ("Java 1.19.x", 15),
    ("Java 1.18.x", 9),
]

RECIPE_TYPES = [
    ("合成（有序）", "minecraft:crafting_shaped"),
    ("合成（无序）", "minecraft:crafting_shapeless"),
    ("熔炼", "minecraft:smelting"),
    ("高炉", "minecraft:blasting"),
    ("烟熏", "minecraft:smoking"),
    ("营火", "minecraft:campfire_cooking"),
    ("切石机", "minecraft:stonecutting"),
    ("锻造台", "minecraft:smithing"),
]

RECIPE_CATEGORIES = ["building", "redstone", "equipment", "misc", ""]

# 常用标签（标签分页）
COMMON_TAGS = [
    ("minecraft:planks", "木板"),
    ("minecraft:logs", "原木"),
    ("minecraft:wooden_slabs", "木台阶"),
    ("minecraft:wooden_stairs", "木楼梯"),
    ("minecraft:stone_tool_materials", "石质工具材料"),
    ("minecraft:wool", "羊毛"),
    ("minecraft:rails", "铁轨"),
    ("minecraft:saplings", "树苗"),
    ("minecraft:flowers", "花"),
    ("minecraft:sand", "沙子"),
    ("minecraft:coals", "煤炭类"),
    ("minecraft:arrows", "箭"),
]


# --------------------------------------------------------------------------
# 物品图标瓦片（颜色块 + 中文标签）
# --------------------------------------------------------------------------

class ItemTile(QPushButton):
    def __init__(self, item, on_click, parent=None):
        super().__init__(parent)
        self.item = item
        self._on_click = on_click
        self.setFixedSize(60, 60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContentsMargins(0, 0, 0, 0)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(1)
        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setStyleSheet("background:transparent;")
        self._icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._cap = QLabel()
        self._cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cap.setStyleSheet(
            f"color:{_text_color().name()};font-size:9px;background:transparent;")
        self._cap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._icon, 1)
        lay.addWidget(self._cap)
        self._style()
        self.clicked.connect(lambda: self._on_click(self.item))

    def _style(self):
        zh = self.item.get("zh", self.item["id"])
        col = self.item["color"]
        px = pixmap(self.item["id"], 40)
        if px is not None:
            self._icon.setPixmap(px)
            self._cap.setText(zh)
            self._cap.setStyleSheet(
                f"color:{_text_color().name()};font-size:9px;background:transparent;")
            self.setStyleSheet(
                f"QPushButton{{background:transparent;border:1px solid "
                f"rgba(128,128,128,0.30);border-radius:8px;}}"
                f"QPushButton:hover{{border:2px solid {_accent()};}}"
            )
        else:
            self._icon.clear()
            short = zh[:3] if len(zh) > 3 else zh
            self._cap.setText(short)
            self.setStyleSheet(
                f"QPushButton{{background:{col};border-radius:8px;border:1px solid "
                f"rgba(0,0,0,0.18);color:#fff;font-size:11px;font-weight:bold;}}"
                f"QPushButton:hover{{border:2px solid {_accent()};}}"
            )

    def update_theme(self):
        self._style()


# --------------------------------------------------------------------------
# 合成格 / 产物槽
# --------------------------------------------------------------------------

class RecipeSlot(QFrame):
    """一个格子。role 用于写回数据模型：
       ("grid", r, c) | ("result",) | ("ingredient",) |
       ("template",) | ("base",) | ("addition",)
    """
    clicked2 = pyqtSignal(object)

    def __init__(self, role, parent=None):
        super().__init__(parent)
        self.role = role
        self.item_id = None
        self._selected = False
        self._count_value = 1
        self.setFixedSize(56, 56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 图标 + 数量徽标
        self._icon = QLabel(self)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setStyleSheet("background:transparent;")
        self._icon.setGeometry(2, 2, 52, 52)
        self._icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._badge = QLabel(self)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setStyleSheet(
            "color:#fff;font-size:11px;font-weight:bold;background:rgba(0,0,0,0.55);"
            "border-radius:6px;padding:0 3px;")
        self._badge.setGeometry(36, 36, 18, 16)
        self._badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._badge.hide()
        self._draw()

    def _draw(self):
        base = _surface_color().name()
        border = _accent() if self._selected else "rgba(0,0,0,0.18)"
        bw = 3 if self._selected else 1
        if self.item_id:
            it = get_item(self.item_id)
            px = pixmap(self.item_id, 48)
            if px is not None:
                self._icon.setPixmap(px)
                self._icon.show()
                self.setStyleSheet(
                    f"QFrame{{background:transparent;border:{bw}px solid {border};"
                    f"border-radius:8px;}}")
            else:
                col = it["color"] if it else "#444"
                self._icon.clear()
                self._icon.hide()
                self.setStyleSheet(
                    f"QFrame{{background:{col};border:{bw}px solid {border};"
                    f"border-radius:8px;}}")
                # 颜色块上的短标签
                lbl = it["zh"] if it else "?"
                txt = lbl[:3] if len(lbl) > 3 else lbl
                self._icon.setText(txt)
                self._icon.setStyleSheet(
                    "color:#fff;font-size:11px;font-weight:bold;background:transparent;")
                self._icon.show()
        else:
            self._icon.clear()
            self._icon.hide()
            self.setStyleSheet(
                f"QFrame{{background:{base};border:{bw}px dashed rgba(128,128,128,0.5);"
                f"border-radius:8px;}}")
        if self.role == ("result",) and self._count_value > 1 and self.item_id:
            self._badge.setText(str(self._count_value))
            self._badge.show()
        else:
            self._badge.hide()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.clear()
            self.clicked2.emit(self)
        else:
            self.clicked2.emit(self)

    def set_selected(self, sel):
        self._selected = sel
        self._draw()

    def set_item(self, item_id):
        self.item_id = item_id
        self._draw()

    def set_count(self, n):
        self._count_value = int(n)
        self._draw()

    def clear(self):
        self.item_id = None
        self._draw()

    def update_theme(self):
        self._draw()


# --------------------------------------------------------------------------
# 物品选择器（右侧面板）
# --------------------------------------------------------------------------

class ItemPickerPanel(QWidget):
    def __init__(self, on_pick, parent=None):
        super().__init__(parent)
        self.on_pick = on_pick
        self.custom_items = []   # 会话内自定义物品
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel("物品")
        title.setObjectName("PageTitle")
        title.setStyleSheet("font-size:16px;font-weight:bold;")
        lay.addWidget(title)

        # 版本
        ver_row = QHBoxLayout()
        ver_row.addWidget(QLabel("版本"))
        self.version = QComboBox()
        self.version.addItems([v for v, _ in PACK_FORMATS])
        self.version.setCurrentIndex(0)
        self.version.setMinimumHeight(30)
        ver_row.addWidget(self.version, 1)
        self.copyright = QPushButton("版权与来源")
        self.copyright.setObjectName("GhostButton")
        self.copyright.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copyright.clicked.connect(self._show_copyright)
        ver_row.addWidget(self.copyright)
        lay.addLayout(ver_row)

        # 标签页
        self.tabs = QTabWidget()
        self.tabs.setObjectName("Card")
        self.item_tab = QWidget()
        self.tag_tab = QWidget()
        self.tabs.addTab(self.item_tab, "物品")
        self.tabs.addTab(self.tag_tab, "标签")
        self._build_item_tab()
        self._build_tag_tab()
        lay.addWidget(self.tabs, 1)

        self._rebuild_grid()

    def _build_item_tab(self):
        lay = QVBoxLayout(self.item_tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        add = _ghost("＋ 添加自定义物品")
        add.clicked.connect(self._add_custom)
        lay.addWidget(add)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索物品")
        self.search.setMinimumHeight(32)
        self.search.textChanged.connect(self._rebuild_grid)
        lay.addWidget(self.search)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid = None
        self.scroll.setWidget(self.grid_host)
        lay.addWidget(self.scroll, 1)

    def _build_tag_tab(self):
        lay = QVBoxLayout(self.tag_tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        info = QLabel("标签（#）可用于配方 key / 材料，按分类筛选一组物品。")
        info.setWordWrap(True)
        info.setStyleSheet("color:#9AA3B8;font-size:12px;")
        lay.addWidget(info)
        self.tag_list = QListWidget()
        for tid, zh in COMMON_TAGS:
            it = QListWidgetItem(f"#{tid}  ·  {zh}")
            it.setData(Qt.ItemDataRole.UserRole, tid)
            self.tag_list.addItem(it)
        self.tag_list.itemClicked.connect(self._on_tag)
        lay.addWidget(self.tag_list, 1)

    def _on_tag(self, item):
        tid = item.data(Qt.ItemDataRole.UserRole)
        # 标签作为原料：以 tag 形式写回（在父级处理）
        self.on_pick({"id": "#" + tid, "zh": "#" + tid.split(":")[-1], "color": "#8B6CFF"})

    def _rebuild_grid(self):
        q = self.search.text()
        items = search_items(q, limit=600)
        items = items + self.custom_items
        host = self.grid_host
        old = host.layout()
        if old:
            while old.count():
                w = old.takeAt(0).widget()
                if w:
                    w.deleteLater()
            QWidget().setLayout(old)
        from PyQt6.QtWidgets import QGridLayout
        gl = QGridLayout(host)
        gl.setSpacing(6)
        cols = 4
        for i, it in enumerate(items):
            tile = ItemTile(it, self.on_pick)
            gl.addWidget(tile, i // cols, i % cols)
        host.setLayout(gl)
        self.grid = gl

    def _add_custom(self):
        d = QDialog(self)
        d.setWindowTitle("添加自定义物品")
        dl = QVBoxLayout(d)
        f_id = QLineEdit(); f_id.setPlaceholderText("命名空间 ID，如 mod:my_item")
        f_zh = QLineEdit(); f_zh.setPlaceholderText("中文名，如 我的物品")
        f_color = QLineEdit(); f_color.setPlaceholderText("颜色 #RRGGBB（可选）")
        dl.addWidget(QLabel("物品 ID")); dl.addWidget(f_id)
        dl.addWidget(QLabel("中文名")); dl.addWidget(f_zh)
        dl.addWidget(QLabel("颜色")); dl.addWidget(f_color)
        ok = _primary("添加"); ok.clicked.connect(d.accept)
        cancel = _ghost("取消"); cancel.clicked.connect(d.reject)
        row = QHBoxLayout(); row.addWidget(ok); row.addWidget(cancel); dl.addLayout(row)
        if d.exec() == QDialog.DialogCode.Accepted:
            iid = f_id.text().strip() or "minecraft:custom"
            if not iid.startswith("minecraft:") and ":" not in iid:
                iid = "minecraft:" + iid
            zh = f_zh.text().strip() or iid
            col = f_color.text().strip() or "#8B6CFF"
            self.custom_items.append({"id": iid, "zh": zh, "cat": "其他", "color": col})
            self._rebuild_grid()

    def _show_copyright(self):
        QMessageBox.information(
            self, "版权与来源",
            "物品名称与配色整理自 Minecraft Java 版公开资料，仅供本地配方编辑使用。\n"
            "实际游戏内材质版权归 Mojang / Microsoft 所有。"
        )

    def update_theme(self):
        self._rebuild_grid()


# --------------------------------------------------------------------------
# 配方生成器主页面
# --------------------------------------------------------------------------

class RecipeGeneratorPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        import sys as _sys
        print("[DBG] RecipeGeneratorPage.__init__: start", flush=True)
        self.recipes = []          # 配方字典列表
        self.current = None        # 当前配方 dict
        self._selected_slot = None
        self._ns = "crafted"       # 数据包命名空间
        self._build_ui()
        print("[DBG] RecipeGeneratorPage: build_ui done", flush=True)
        self._new_recipe(silent=True)
        print("[DBG] RecipeGeneratorPage.__init__: end", flush=True)

    # ----- 数据模型 -----
    def _blank_recipe(self, n=1):
        return {
            "name": f"新配方 {n}",
            "type": "minecraft:crafting_shaped",
            "group": "",
            "category": "",
            "filename": "",
            "shapeless": False,
            "grid2x2": False,
            "preserve": False,
            "notify": True,
            "grid": [[None] * 3 for _ in range(3)],
            "result": None,
            "result_count": 1,
            "ingredient": None,
            "template": None,
            "base": None,
            "addition": None,
            "experience": 0.1,
            "cookingtime": 200,
        }

    def _new_recipe(self, silent=False):
        n = len(self.recipes) + 1
        r = self._blank_recipe(n)
        self.recipes.append(r)
        self._refresh_recipe_list()
        self._select_recipe(len(self.recipes) - 1)
        if not silent:
            self._status("已新建配方")

    def _refresh_recipe_list(self):
        self.recipe_list.clear()
        for i, r in enumerate(self.recipes):
            tid = self._recipe_id(r)
            item = QListWidgetItem(f"{self._type_zh(r['type'])}  ·  {tid}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.recipe_list.addItem(item)
        # 选中当前
        if self.current in self.recipes:
            idx = self.recipes.index(self.current)
            self.recipe_list.setCurrentRow(idx)

    def _select_recipe(self, idx):
        if 0 <= idx < len(self.recipes):
            self.current = self.recipes[idx]
            self._load_into_editor()

    def _type_zh(self, t):
        for zh, v in RECIPE_TYPES:
            if v == t:
                return zh
        return t

    def _recipe_id(self, r):
        fn = r["filename"].strip() or self._auto_filename(r)
        return f"{self._ns}:{fn}"

    def _auto_filename(self, r):
        res = r.get("result")
        if res:
            base = res.split(":")[-1]
        else:
            base = "recipe"
        return re.sub(r"[^a-z0-9_]", "_", base.lower()) or "recipe"

    # ----- UI 构建 -----
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        head = QLabel("配方生成器")
        head.setObjectName("PageTitle")
        head.setStyleSheet("font-size:22px;font-weight:bold;")
        root.addWidget(head)

        sub = QLabel("可视化编辑 Minecraft 合成 / 熔炼 / 切石 / 锻造配方，实时生成 JSON，一键导出数据包。")
        sub.setObjectName("HintText")
        root.addWidget(sub)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        # 左：配方列表
        left = _card()
        left.setFixedWidth(220)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 12, 12, 12)
        lv.setSpacing(8)
        lv.addWidget(_section_title("配方"))
        b_new = _primary("＋ 新建配方")
        b_new.clicked.connect(lambda: self._new_recipe())
        lv.addWidget(b_new)
        self.recipe_list = QListWidget()
        self.recipe_list.setMinimumHeight(220)
        self.recipe_list.currentRowChanged.connect(self._select_recipe)
        lv.addWidget(self.recipe_list, 1)
        b_export = _ghost("⬇ 导出数据包")
        b_export.clicked.connect(self._export_datapack)
        lv.addWidget(b_export)
        body.addWidget(left)

        # 中：选项 + 预览 + JSON
        mid = QVBoxLayout()
        mid.setSpacing(12)
        body.addLayout(mid, 1)

        # 选项
        opt = _card()
        ol = QVBoxLayout(opt)
        ol.setContentsMargins(14, 14, 14, 14)
        ol.setSpacing(8)
        ol.addWidget(_section_title("选项"))

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        row1.addWidget(self._field("配方类型", self._type_combo()))
        row1.addWidget(self._field("分组", self._line("group", "可选，如 minecraft:building")))
        row1.addWidget(self._field("分类", self._category_combo()))
        row1.addWidget(self._field("文件名", self._line("filename", "自动（留空）")))
        mid.addWidget(opt)

        row2 = QHBoxLayout()
        row2.setSpacing(16)
        self.cb_shapeless = QCheckBox("无序合成")
        self.cb_2x2 = QCheckBox("2×2 合成格")
        self.cb_preserve = QCheckBox("保留精确输入位置")
        self.cb_shapeless.toggled.connect(self._on_option)
        self.cb_2x2.toggled.connect(self._on_2x2)
        self.cb_preserve.toggled.connect(self._on_option)
        row2.addWidget(self.cb_shapeless)
        row2.addWidget(self.cb_2x2)
        row2.addWidget(self.cb_preserve)
        row2.addStretch(1)
        ol.addLayout(row2)

        # 配方通知
        notify_row = QHBoxLayout()
        self.cb_notify = QCheckBox("显示配方通知")
        self.cb_notify.setChecked(True)
        self.cb_notify.toggled.connect(self._on_option)
        self.notify_preview = QLabel()
        self.notify_preview.setObjectName("HintText")
        self.notify_preview.setStyleSheet("color:#9AA3B8;font-size:12px;")
        notify_row.addWidget(self.cb_notify)
        notify_row.addWidget(self.notify_preview, 1)
        ol.addLayout(notify_row)

        # 预览（合成格 + 产物 + 箭头 + 操作 + 校验）
        prev = _card()
        pl = QVBoxLayout(prev)
        pl.setContentsMargins(14, 14, 14, 14)
        pl.setSpacing(10)
        pl.addWidget(_section_title("预览"))
        self.io_area = QWidget()
        self.io_layout = QHBoxLayout(self.io_area)
        self.io_layout.setSpacing(18)
        pl.addWidget(self.io_area, 1)

        op_row = QHBoxLayout()
        b_clear = _ghost("清空槽位")
        b_clear.clicked.connect(self._clear_slots)
        b_copy = _ghost("复制图片")
        b_copy.clicked.connect(self._copy_image)
        op_row.addWidget(b_clear)
        op_row.addWidget(b_copy)
        op_row.addStretch(1)
        pl.addLayout(op_row)

        self.err_box = QFrame()
        self.err_box.setObjectName("Card")
        self.err_label = QLabel()
        self.err_label.setWordWrap(True)
        self.err_label.setStyleSheet("font-size:12px;padding:6px;")
        el = QVBoxLayout(self.err_box)
        el.setContentsMargins(8, 8, 8, 8)
        el.addWidget(self.err_label)
        pl.addWidget(self.err_box)
        mid.addWidget(prev)

        # JSON 输出
        jcard = _card()
        jl = QVBoxLayout(jcard)
        jl.setContentsMargins(14, 14, 14, 14)
        jl.setSpacing(8)
        jhead = QHBoxLayout()
        jhead.addWidget(_section_title("JSON 输出"))
        jhead.addStretch(1)
        b_save = _primary("保存 JSON")
        b_save.clicked.connect(self._save_json)
        jhead.addWidget(b_save)
        jl.addLayout(jhead)
        self.json_view = QTextEdit()
        self.json_view.setReadOnly(True)
        self.json_view.setMinimumHeight(140)
        mono = QFont("Consolas", 11)
        self.json_view.setFont(mono)
        self.json_view.setStyleSheet("background:#0E1018;color:#B9C2D6;border-radius:8px;")
        jl.addWidget(self.json_view, 1)
        mid.addWidget(jcard)

        # 右：物品选择
        right = _card()
        right.setFixedWidth(320)
        rp_layout = QVBoxLayout(right)
        rp_layout.setContentsMargins(0, 0, 0, 0)
        self.picker = ItemPickerPanel(self._on_pick)
        rp_layout.addWidget(self.picker)
        body.addWidget(right)

        self._rebuild_io_area()

    def _field(self, name, widget):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(4)
        lab = QLabel(name)
        lab.setStyleSheet("font-size:12px;color:#9AA3B8;")
        l.addWidget(lab)
        l.addWidget(widget, 1)
        return w

    def _type_combo(self):
        self.type_combo = QComboBox()
        for zh, v in RECIPE_TYPES:
            self.type_combo.addItem(zh, v)
        self.type_combo.setMinimumHeight(32)
        self.type_combo.currentIndexChanged.connect(self._on_type)
        return self.type_combo

    def _category_combo(self):
        self.cat_combo = QComboBox()
        self.cat_combo.addItems(RECIPE_CATEGORIES)
        self.cat_combo.setCurrentIndex(0)
        self.cat_combo.setMinimumHeight(32)
        self.cat_combo.currentTextChanged.connect(self._on_option)
        return self.cat_combo

    def _line(self, key, placeholder):
        le = QLineEdit()
        le.setPlaceholderText(placeholder)
        le.setMinimumHeight(32)
        le.textChanged.connect(self._on_option)
        setattr(self, f"_le_{key}", le)
        return le

    # ----- 选项回调 -----
    def _on_type(self):
        if self.current is None:
            return
        self.current["type"] = self.type_combo.currentData()
        # 切换类型后重建输入区
        self.cb_shapeless.setVisible(self.current["type"] in
                                     ("minecraft:crafting_shaped", "minecraft:crafting_shapeless"))
        self.cb_2x2.setVisible(self.current["type"] == "minecraft:crafting_shaped")
        self.cb_preserve.setVisible(self.current["type"].startswith("minecraft:crafting"))
        self._rebuild_io_area()
        self._recompute()

    def _on_2x2(self, checked):
        if self.current is None:
            return
        self.current["grid2x2"] = checked
        self._rebuild_io_area()
        self._recompute()

    def _on_option(self):
        if self.current is None:
            return
        self.current["shapeless"] = self.cb_shapeless.isChecked()
        self.current["preserve"] = self.cb_preserve.isChecked()
        self.current["notify"] = self.cb_notify.isChecked()
        self.current["group"] = self._le_group.text().strip()
        self.current["category"] = self.cat_combo.currentText()
        self.current["filename"] = self._le_filename.text().strip()
        self._recompute()

    # ----- 输入区（随类型变化） -----
    def _rebuild_io_area(self):
        # 清空旧控件
        while self.io_layout.count():
            w = self.io_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.slots = []
        self.count_spin = None
        t = self.current["type"] if self.current else "minecraft:crafting_shaped"

        if t in ("minecraft:crafting_shaped", "minecraft:crafting_shapeless"):
            grid_w = QWidget()
            gl = QGridLayout(grid_w)
            gl.setSpacing(6)
            size = 2 if (self.current and self.current["grid2x2"]) else 3
            for r in range(size):
                for c in range(size):
                    slot = RecipeSlot(("grid", r, c), self)
                    slot.clicked2.connect(self._on_slot_click)
                    gl.addWidget(slot, r, c)
                    self.slots.append(slot)
            self.io_layout.addWidget(grid_w)
        elif t in ("minecraft:smelting", "minecraft:blasting",
                   "minecraft:smoking", "minecraft:campfire_cooking",
                   "minecraft:stonecutting"):
            ing = RecipeSlot(("ingredient",), self)
            ing.clicked2.connect(self._on_slot_click)
            self.slots.append(ing)
            self.io_layout.addWidget(ing)
        elif t == "minecraft:smithing":
            for role in (("template",), ("base",), ("addition",)):
                s = RecipeSlot(role, self)
                s.clicked2.connect(self._on_slot_click)
                self.slots.append(s)
                self.io_layout.addWidget(s)

        # 箭头
        arrow = QLabel("➜")
        arrow.setStyleSheet("font-size:28px;color:#FF5FA2;font-weight:bold;")
        self.io_layout.addWidget(arrow)

        # 产物
        res = RecipeSlot(("result",), self)
        res.clicked2.connect(self._on_slot_click)
        self.slots.append(res)
        self.io_layout.addWidget(res)

        # 产物数量（仅合成 / 切石 / 熔炼类）
        if t in ("minecraft:crafting_shaped", "minecraft:crafting_shapeless",
                 "minecraft:stonecutting", "minecraft:smelting",
                 "minecraft:blasting", "minecraft:smoking",
                 "minecraft:campfire_cooking"):
            cnt_w = QWidget()
            cl = QVBoxLayout(cnt_w)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(4)
            cl.addWidget(QLabel("数量"))
            self.count_spin = QSpinBox()
            self.count_spin.setRange(1, 64)
            self.count_spin.setMinimumHeight(32)
            self.count_spin.valueChanged.connect(self._on_count)
            cl.addWidget(self.count_spin)
            self.io_layout.addWidget(cnt_w)

        self._load_slots_from_model()
        # 产物数量徽标
        rs = self._result_slot()
        if rs is not None and self.current:
            rs.set_count(self.current.get("result_count", 1))

    def _result_slot(self):
        for s in getattr(self, "slots", []):
            if s.role == ("result",):
                return s
        return None

    def _on_count(self, v):
        if self.current and self.count_spin is not None:
            self.current["result_count"] = v
            rs = self._result_slot()
            if rs is not None:
                rs.set_count(v)
            self._recompute()

    # ----- 槽位交互 -----
    def _on_slot_click(self, slot):
        # 切换选中状态
        if self._selected_slot is slot:
            self._selected_slot.set_selected(False)
            self._selected_slot = None
            self._status("已取消选择槽位")
        else:
            if self._selected_slot:
                self._selected_slot.set_selected(False)
            self._selected_slot = slot
            slot.set_selected(True)
            self._status("已选择槽位，请在右侧点击物品放入")

    def _on_pick(self, item):
        if item is None:
            return
        if self._selected_slot is None:
            self._status("请先在左侧合成格中选择一个槽位，再点击物品")
            return
        self._selected_slot.set_item(item["id"])
        self._write_slot_to_model(self._selected_slot)
        self._recompute()

    def _write_slot_to_model(self, slot):
        r = self.current
        role = slot.role
        if role[0] == "grid":
            r["grid"][role[1]][role[2]] = slot.item_id
        elif role[0] == "result":
            r["result"] = slot.item_id
            # 同步数量控件
            if hasattr(self, "count_spin"):
                self.count_spin.setValue(r.get("result_count", 1))
        elif role[0] == "ingredient":
            r["ingredient"] = slot.item_id
        elif role[0] == "template":
            r["template"] = slot.item_id
        elif role[0] == "base":
            r["base"] = slot.item_id
        elif role[0] == "addition":
            r["addition"] = slot.item_id

    def _clear_slots(self):
        for slot in self.slots:
            slot.clear()
            self._write_slot_to_model(slot)
        self._recompute()
        self._status("已清空槽位")

    def _copy_image(self):
        pix = self.io_area.grab()
        QApplication.clipboard().setPixmap(pix)
        self._status("已复制预览图片到剪贴板")

    # ----- 载入当前配方到控件 -----
    def _load_into_editor(self):
        r = self.current
        # 类型
        idx = self.type_combo.findData(r["type"])
        if idx >= 0:
            self.type_combo.setCurrentIndex(idx)
        self.cb_shapeless.setChecked(r["shapeless"])
        self.cb_2x2.setChecked(r["grid2x2"])
        self.cb_preserve.setChecked(r["preserve"])
        self.cb_notify.setChecked(r["notify"])
        self._le_group.setText(r["group"])
        ci = self.cat_combo.findText(r["category"])
        if ci >= 0:
            self.cat_combo.setCurrentIndex(ci)
        self._le_filename.setText(r["filename"])
        self.cb_shapeless.setVisible(r["type"] in
                                     ("minecraft:crafting_shaped", "minecraft:crafting_shapeless"))
        self.cb_2x2.setVisible(r["type"] == "minecraft:crafting_shaped")
        self.cb_preserve.setVisible(r["type"].startswith("minecraft:crafting"))
        self._rebuild_io_area()
        if self.count_spin is not None:
            self.count_spin.setValue(r.get("result_count", 1))
        rs = self._result_slot()
        if rs is not None:
            rs.set_count(r.get("result_count", 1))
        self._recompute()

    def _load_slots_from_model(self):
        r = self.current
        if r is None:
            return
        for slot in self.slots:
            role = slot.role
            if role[0] == "grid":
                slot.set_item(r["grid"][role[1]][role[2]])
            elif role[0] == "result":
                slot.set_item(r["result"])
            elif role[0] == "ingredient":
                slot.set_item(r["ingredient"])
            elif role[0] == "template":
                slot.set_item(r["template"])
            elif role[0] == "base":
                slot.set_item(r["base"])
            elif role[0] == "addition":
                slot.set_item(r["addition"])

    # ----- JSON 计算 -----
    def _build_json(self, r):
        t = r["type"]
        out = {"type": t}
        if r["group"]:
            out["group"] = r["group"]
        if r["category"] and t.startswith("minecraft:crafting"):
            out["category"] = r["category"]

        if t == "minecraft:crafting_shaped":
            # 分配字母
            letter = {}
            nxt = 65  # 'A'
            grid_ids = [[r["grid"][rr][cc] for cc in range(3)] for rr in range(3)]
            size = 2 if r["grid2x2"] else 3
            pattern = []
            for rr in range(size):
                row = ""
                for cc in range(size):
                    iid = grid_ids[rr][cc]
                    if iid is None:
                        row += " "
                    else:
                        if iid not in letter:
                            letter[iid] = chr(nxt); nxt += 1
                        row += letter[iid]
                pattern.append(row)
            # 修剪首尾全空行/列
            while pattern and pattern[0].strip() == "":
                pattern.pop(0)
            while pattern and pattern[-1].strip() == "":
                pattern.pop()
            if pattern:
                while all(row[0] == " " for row in pattern):
                    pattern = [row[1:] for row in pattern]
                while pattern and all(row[-1] == " " for row in pattern):
                    pattern = [row[:-1] for row in pattern]
            out["pattern"] = pattern
            key = {}
            for iid, ch in letter.items():
                if iid.startswith("#"):
                    key[ch] = {"tag": iid[1:]}
                else:
                    key[ch] = {"item": iid}
            out["key"] = key
            if r["result"]:
                out["result"] = {"item": r["result"], "count": r.get("result_count", 1)}
        elif t == "minecraft:crafting_shapeless":
            ings = []
            for rr in range(3):
                for cc in range(3):
                    iid = r["grid"][rr][cc]
                    if iid is not None:
                        if iid.startswith("#"):
                            ings.append({"tag": iid[1:]})
                        else:
                            ings.append({"item": iid})
            out["ingredients"] = ings
            if r["result"]:
                out["result"] = {"item": r["result"], "count": r.get("result_count", 1)}
        elif t in ("minecraft:smelting", "minecraft:blasting",
                   "minecraft:smoking", "minecraft:campfire_cooking"):
            if r["ingredient"]:
                if r["ingredient"].startswith("#"):
                    out["ingredient"] = {"tag": r["ingredient"][1:]}
                else:
                    out["ingredient"] = {"item": r["ingredient"]}
            if r["result"]:
                out["result"] = {"item": r["result"], "count": r.get("result_count", 1)}
            out["experience"] = r.get("experience", 0.1)
            out["cookingtime"] = r.get("cookingtime", 200)
        elif t == "minecraft:stonecutting":
            if r["ingredient"]:
                if r["ingredient"].startswith("#"):
                    out["ingredient"] = {"tag": r["ingredient"][1:]}
                else:
                    out["ingredient"] = {"item": r["ingredient"]}
            if r["result"]:
                out["result"] = {"item": r["result"], "count": r.get("result_count", 1)}
        elif t == "minecraft:smithing":
            if r["template"]:
                out["template"] = {"item": r["template"]}
            if r["base"]:
                out["base"] = {"item": r["base"]}
            if r["addition"]:
                out["addition"] = {"item": r["addition"]}
            if r["result"]:
                out["result"] = {"item": r["result"], "count": r.get("result_count", 1)}
        return out

    def _validate(self, r):
        problems = []
        t = r["type"]
        if t in ("minecraft:crafting_shaped", "minecraft:crafting_shapeless"):
            has_in = any(r["grid"][rr][cc] for rr in range(3) for cc in range(3))
            if not has_in:
                problems.append("请放置至少一种材料")
            if not r["result"]:
                problems.append("请添加产物物品")
        elif t in ("minecraft:smelting", "minecraft:blasting",
                   "minecraft:smoking", "minecraft:campfire_cooking",
                   "minecraft:stonecutting"):
            if not r["ingredient"]:
                problems.append("请放置输入物品")
            if not r["result"]:
                problems.append("请添加产物物品")
        elif t == "minecraft:smithing":
            if not (r["template"] and r["base"] and r["addition"]):
                problems.append("请放置模板 / 基础 / 附加 三种输入")
            if not r["result"]:
                problems.append("请添加产物物品")
        return problems

    def _recompute(self):
        r = self.current
        if r is None:
            return
        # 通知预览
        if r["notify"]:
            res = r["result"]
            reszh = get_item(res)["zh"] if res and get_item(res) else (res or "产物")
            self.notify_preview.setText(
                f"预览：{reszh} 已合成：{self._recipe_id(r)}")
        else:
            self.notify_preview.setText("")
        # JSON
        data = self._build_json(r)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        self.json_view.setPlainText(text)
        # 校验
        problems = self._validate(r)
        if problems:
            self.err_box.setStyleSheet(
                "background:rgba(194,75,75,0.15);border-radius:8px;"
                "border:1px solid #C24B4B;")
            self.err_label.setStyleSheet("color:#FFB3B3;font-size:12px;")
            self.err_label.setText(f"{len(problems)} 个问题：\n" + "\n".join("• " + p for p in problems))
        else:
            self.err_box.setStyleSheet(
                "background:rgba(47,179,122,0.15);border-radius:8px;"
                "border:1px solid #2FB37A;")
            self.err_label.setStyleSheet("color:#9BE7C4;font-size:12px;")
            self.err_label.setText("✓ 校验通过，可保存 / 导出")

    # ----- 保存 / 导出 -----
    def _save_json(self):
        if not self.current:
            return
        data = self._build_json(self.current)
        fn = self._recipe_id(self.current).replace(":", "_") + ".json"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存配方 JSON", fn, "JSON 文件 (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._status(f"已保存：{os.path.basename(path)}")

    def _export_datapack(self):
        if not self.recipes:
            return
        # 校验全部
        bad = [i for i, r in enumerate(self.recipes) if self._validate(r)]
        if bad:
            QMessageBox.warning(
                self, "无法导出",
                f"有 {len(bad)} 个配方校验未通过（序号 {[b+1 for b in bad]}），请先补全材料与产物。")
            return
        zip_path, _ = QFileDialog.getSaveFileName(
            self, "导出数据包", "recipe_pack.zip", "Zip 文件 (*.zip)")
        if not zip_path:
            return
        pack_ver = self.picker.version.currentText()
        pack_format = dict(PACK_FORMATS).get(pack_ver, 48)
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("pack.mcmeta", json.dumps(
                    {"pack": {"pack_format": pack_format,
                              "description": "由 PlainMC 配方生成器导出"}},
                    indent=2, ensure_ascii=False))
                z.writestr(f"data/{self._ns}/recipe/.keep", "")
                for r in self.recipes:
                    fn = self._recipe_id(r).split(":")[-1]
                    z.writestr(
                        f"data/{self._ns}/recipe/{fn}.json",
                        json.dumps(self._build_json(r), indent=2, ensure_ascii=False))
            self._status(f"已导出数据包：{os.path.basename(zip_path)}（{len(self.recipes)} 个配方）")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e)[:300])

    # ----- 状态提示 -----
    def _status(self, msg):
        self.err_label.setText(msg)
        self.err_box.setStyleSheet(
            "background:rgba(91,124,250,0.12);border-radius:8px;border:1px solid #5B7CFA;")
        self.err_label.setStyleSheet("color:#BBD0FF;font-size:12px;")


def _section_title(t):
    lab = QLabel(t)
    lab.setStyleSheet("font-size:13px;font-weight:bold;color:#9AA3B8;")
    return lab




# ==========================================================================
# 皮肤编辑器（像素绘制 + 纸娃娃预览 + 一键同步到游戏）
# 原 skin_editor.py，已内联进本文件以保持单文件分发
# ==========================================================================
# # -*- coding: utf-8 -*-
# """Minecraft 皮肤编辑器：像素级绘制 + 纸娃娃预览 + 一键同步到游戏。
# 
# UV 布局（64x64）已用官方 Steve / Alex 皮肤逐块验证：
#   头部区域 (0,0) 32x16 —— 上排(8,0)=顶 (16,0)=底；下排(0,8)=右 (8,8)=正
#                         (16,8)=左 (24,8)=后
#   身体区域 (16,16) 24x16 —— 上排(20,16)=顶 (28,16)=底；下排(16,20)=右
#                           (20,20)=正 (28,20)=左 (32,20)=后
#   四肢区域 16x16 —— 上排(+4)=顶 (+8)=底；下排(+0)=右 (+4)=正 (+8)=左 (+12)=后
#   slim（细臂）模型只有手臂不同：手臂宽 3 像素，贴图里
#   侧面变成 [右4][正3][左4][后3] = 14 像素，顶部/底部 3 像素宽。
# """

import json
import os
import zipfile

from PyQt6.QtCore import Qt, QPointF, QRectF, QBuffer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QImage, QPainter, QPen, QPixmap,
                         QPolygonF, QTransform)
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog,
                             QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QMessageBox, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

# ==========================================================================
# 皮肤几何：每个部位每个面在 64x64 贴图中的 (x, y, w, h)
# ==========================================================================
SKIN_SIZE = 64

# (部位, 层) -> (top, bottom, right, front, left, back)
SKIN_FACES = {
    ("head", "base"): ((8, 0, 8, 8), (16, 0, 8, 8), (0, 8, 8, 8),
                       (8, 8, 8, 8), (16, 8, 8, 8), (24, 8, 8, 8)),
    ("head", "over"): ((40, 0, 8, 8), (48, 0, 8, 8), (32, 8, 8, 8),
                       (40, 8, 8, 8), (48, 8, 8, 8), (56, 8, 8, 8)),
    ("body", "base"): ((20, 16, 8, 4), (28, 16, 8, 4), (16, 20, 4, 12),
                       (20, 20, 8, 12), (28, 20, 4, 12), (32, 20, 8, 12)),
    ("body", "over"): ((20, 32, 8, 4), (28, 32, 8, 4), (16, 36, 4, 12),
                       (20, 36, 8, 12), (28, 36, 4, 12), (32, 36, 8, 12)),
    ("rarm", "base"): ((44, 16, 4, 4), (48, 16, 4, 4), (40, 20, 4, 12),
                       (44, 20, 4, 12), (48, 20, 4, 12), (52, 20, 4, 12)),
    ("rarm", "over"): ((44, 32, 4, 4), (48, 32, 4, 4), (40, 36, 4, 12),
                       (44, 36, 4, 12), (48, 36, 4, 12), (52, 36, 4, 12)),
    ("rleg", "base"): ((4, 16, 4, 4), (8, 16, 4, 4), (0, 20, 4, 12),
                       (4, 20, 4, 12), (8, 20, 4, 12), (12, 20, 4, 12)),
    ("rleg", "over"): ((4, 32, 4, 4), (8, 32, 4, 4), (0, 36, 4, 12),
                       (4, 36, 4, 12), (8, 36, 4, 12), (12, 36, 4, 12)),
    ("larm", "base"): ((36, 48, 4, 4), (40, 48, 4, 4), (32, 52, 4, 12),
                       (36, 52, 4, 12), (40, 52, 4, 12), (44, 52, 4, 12)),
    ("larm", "over"): ((52, 48, 4, 4), (56, 48, 4, 4), (48, 52, 4, 12),
                       (52, 52, 4, 12), (56, 52, 4, 12), (60, 52, 4, 12)),
    ("lleg", "base"): ((20, 48, 4, 4), (24, 48, 4, 4), (16, 52, 4, 12),
                       (20, 52, 4, 12), (24, 52, 4, 12), (28, 52, 4, 12)),
    ("lleg", "over"): ((4, 48, 4, 4), (8, 48, 4, 4), (0, 52, 4, 12),
                       (4, 52, 4, 12), (8, 52, 4, 12), (12, 52, 4, 12)),
}

# 部位 -> 中文名 / 覆盖层中文名
SKIN_PART_CN = {
    "head": "头部", "body": "身体",
    "rarm": "右臂", "larm": "左臂",
    "rleg": "右腿", "lleg": "左腿",
}
# 覆盖层（帽子/外套/袖子/裤子第二层）
SKIN_OVER_CN = {
    "head": "帽子", "body": "外套",
    "rarm": "右袖", "larm": "左袖",
    "rleg": "右裤", "lleg": "左裤",
}

# ---------------------------------------------------------------------------
# slim（细臂 / Alex）模型的布局
# ---------------------------------------------------------------------------
# slim 模型的手臂只有 3 像素宽（wide 是 4），贴图里相应地：
#   · 前后两面由 4 像素宽缩成 3 像素宽
#   · 左右两面仍是 4 像素宽（手臂深度没变，仍是 4）
# 于是侧面区变成 [右4][正3][左4][后3] = 14 像素，顶部/底部 3 像素宽。
# 头 / 身体 / 腿 的布局与 wide 完全一致，无需转换。
SLIM_ARM_FACES = {
    "rarm": ((4, 0, 3, 4), (7, 0, 3, 4), (0, 4, 4, 12),
             (4, 4, 3, 12), (7, 4, 4, 12), (11, 4, 3, 12)),
    "larm": ((4, 0, 3, 4), (7, 0, 3, 4), (0, 4, 4, 12),
             (4, 4, 3, 12), (7, 4, 4, 12), (11, 4, 3, 12)),
}
# slim 下各部位各层区域在 64x64 贴图中的位置（与 wide 相同，但覆盖层位置不同）
SLIM_PART_ORIGIN = {
    ("rarm", "base"): (40, 16), ("rarm", "over"): (40, 32),
    ("larm", "base"): (32, 48), ("larm", "over"): (48, 48),
}

# 每个部位的包围盒（用于在画布上画分区线与标签）
SKIN_PART_BOX = {
    "head": (0, 0, 64, 16),
    "body": (16, 16, 24, 32),
    "rarm": (40, 16, 16, 32),
    "rleg": (0, 16, 16, 32),
    "larm": (32, 48, 32, 16),
    "lleg": (0, 48, 32, 16),
}

# 常用色板
SKIN_PALETTE = [
    "#ffffff", "#c6c6c6", "#8f8f8f", "#555555", "#2b2b2b", "#000000",
    "#f5d7b5", "#e0ac69", "#c68642", "#8d5524", "#5c3317",
    "#b03a48", "#e74c3c", "#f39c12", "#f7dc6f", "#f2e394",
    "#1abc9c", "#48c9b0", "#2ecc71", "#58d68d", "#28b463",
    "#3498db", "#5dade2", "#85c1e9", "#2874a6", "#1f618d",
    "#8e44ad", "#af7ac5", "#d2b4de", "#6c3483", "#4a235a",
    "#d35400", "#e67e22", "#f0b27a", "#af601a", "#7e5109",
]


# ==========================================================================
# 画布
# ==========================================================================
class SkinCanvas(QWidget):
    """皮肤画布：在 64x64 贴图上逐像素绘制。"""

    changed = pyqtSignal()
    picked = pyqtSignal(QColor)

    TOOL_PENCIL = "pencil"
    TOOL_ERASER = "eraser"
    TOOL_BUCKET = "bucket"
    TOOL_PICKER = "picker"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage(SKIN_SIZE, SKIN_SIZE, QImage.Format.Format_ARGB32)
        self.image.fill(QColor(0, 0, 0, 0))
        self.tool = self.TOOL_PENCIL
        self.color = QColor("#ffffff")
        self.brush = 1
        self.zoom = 8
        self.grid = True
        self.overlay = True
        self._drawing = False
        self._undo = []
        self._redo = []
        self.setFixedSize(SKIN_SIZE * self.zoom + 2, SKIN_SIZE * self.zoom + 2)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ---------- 基本操作 ----------
    def set_zoom(self, z):
        self.zoom = max(2, min(16, int(z)))
        self.setFixedSize(SKIN_SIZE * self.zoom + 2, SKIN_SIZE * self.zoom + 2)
        self.update()

    def snapshot(self):
        """记录一次可撤销的状态。"""
        self._undo.append(self.image.copy())
        if len(self._undo) > 40:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return
        self._redo.append(self.image.copy())
        self.image = self._undo.pop()
        self.update()
        self.changed.emit()

    def redo(self):
        if not self._redo:
            return
        self._undo.append(self.image.copy())
        self.image = self._redo.pop()
        self.update()
        self.changed.emit()

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def clear_all(self):
        self.snapshot()
        self.image.fill(QColor(0, 0, 0, 0))
        self.update()
        self.changed.emit()

    def load_image(self, img):
        """载入一张皮肤（非 64x64 会自动缩放）。"""
        self.snapshot()
        if img.width() == SKIN_SIZE and img.height() == SKIN_SIZE:
            self.image = img.convertToFormat(QImage.Format.Format_ARGB32)
        else:
            scaled = img.scaled(SKIN_SIZE, SKIN_SIZE,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.FastTransformation)
            self.image = scaled.convertToFormat(QImage.Format.Format_ARGB32)
        self.update()
        self.changed.emit()

    def to_png_bytes(self):
        """导出 PNG 字节（保留透明通道）。"""
        buf = QBuffer()
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        self.image.save(buf, "PNG")
        return bytes(buf.data())

    # ---------- 绘制 ----------
    def paintEvent(self, e):
        p = QPainter(self)
        z = self.zoom
        # 棋盘底（表示透明）
        p.fillRect(self.rect(), QColor("#2b2f3a"))
        c = QColor("#3a3f4d")
        p.setPen(Qt.PenStyle.NoPen)
        for yy in range(0, self.height(), z):
            for xx in range(0, self.width(), z):
                if ((xx // z) + (yy // z)) % 2 == 0:
                    p.fillRect(xx, yy, z, z, c)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.drawImage(QRectF(1, 1, SKIN_SIZE * z, SKIN_SIZE * z), self.image)
        # 像素网格
        if self.grid and z >= 4:
            p.setPen(QPen(QColor(0, 0, 0, 70), 1))
            for i in range(SKIN_SIZE + 1):
                pos = 1 + i * z
                p.drawLine(pos, 1, pos, 1 + SKIN_SIZE * z)
                p.drawLine(1, pos, 1 + SKIN_SIZE * z, pos)
        # 部位分区线 + 标签
        if self.overlay:
            p.setPen(QPen(QColor(255, 255, 255, 110), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for _part, box in SKIN_PART_BOX.items():
                x, y, w, h = box
                p.drawRect(QRectF(1 + x * z, 1 + y * z, w * z, h * z))
            p.setPen(QColor(255, 255, 255, 190))
            f = p.font()
            f.setPixelSize(max(8, min(13, z)))
            p.setFont(f)
            for (part, layer), faces in SKIN_FACES.items():
                if layer != "base":
                    continue
                fx, fy, fw, fh = faces[3]     # 正面
                if fw * z < 34 or fh * z < 14:
                    continue
                cn = SKIN_PART_CN.get(part, part)
                p.drawText(QRectF(1 + fx * z, 1 + fy * z, fw * z, fh * z),
                           Qt.AlignmentFlag.AlignTop
                           | Qt.AlignmentFlag.AlignHCenter, cn)
        p.end()

    # ---------- 鼠标 ----------
    def _pixel_at(self, pos):
        x = (pos.x() - 1) // self.zoom
        y = (pos.y() - 1) // self.zoom
        if 0 <= x < SKIN_SIZE and 0 <= y < SKIN_SIZE:
            return x, y
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        px = self._pixel_at(e.position().toPoint())
        if px is None:
            return
        if self.tool == self.TOOL_PICKER:
            c = self.image.pixelColor(px[0], px[1])
            if c.alpha() > 0:
                self.color = QColor(c)
                self.picked.emit(c)
            return
        self.snapshot()
        self._drawing = True
        self._paint(px)

    def mouseMoveEvent(self, e):
        if not self._drawing:
            return
        px = self._pixel_at(e.position().toPoint())
        if px is not None:
            self._paint(px)

    def mouseReleaseEvent(self, e):
        if self._drawing:
            self._drawing = False
            self.changed.emit()

    def _paint(self, px):
        x, y = px
        if self.tool == self.TOOL_ERASER:
            self.image.setPixelColor(x, y, QColor(0, 0, 0, 0))
        elif self.tool == self.TOOL_BUCKET:
            self._flood_fill(x, y, self.color)
        else:
            for dy in range(self.brush):
                for dx in range(self.brush):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < SKIN_SIZE and 0 <= ny < SKIN_SIZE:
                        self.image.setPixelColor(nx, ny, self.color)
        self.update()

    def _flood_fill(self, x, y, color):
        target = self.image.pixelColor(x, y)
        if target == color:
            return
        stack = [(x, y)]
        while stack:
            cx, cy = stack.pop()
            if self.image.pixelColor(cx, cy) != target:
                continue
            self.image.setPixelColor(cx, cy, color)
            for nx, ny in ((cx - 1, cy), (cx + 1, cy),
                           (cx, cy - 1), (cx, cy + 1)):
                if 0 <= nx < SKIN_SIZE and 0 <= ny < SKIN_SIZE:
                    if self.image.pixelColor(nx, ny) == target:
                        stack.append((nx, ny))


# ==========================================================================
# 纸娃娃预览（正面 + 背面平视图）
# ==========================================================================
class SkinDoll(QWidget):
    """纸娃娃预览：正面 + 背面两个平视图。

    只用「正面/背面」贴图拼装，不做 3D 投影 —— 100% 准确，
    不会出现某个面盖住另一个面的问题。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage(SKIN_SIZE, SKIN_SIZE, QImage.Format.Format_ARGB32)
        self.image.fill(QColor(0, 0, 0, 0))
        self.setMinimumHeight(250)
        self.setMinimumWidth(250)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Expanding)

    def set_image(self, img):
        self.image = img
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 0))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        pix = QPixmap.fromImage(self.image)

        # 正面：角色右手臂显示在观者左侧
        front = [
            ("head", 3, 8, 0), ("body", 3, 8, 8),
            ("rarm", 3, 4, 8), ("larm", 3, 16, 8),
            ("rleg", 3, 8, 20), ("lleg", 3, 12, 20),
        ]
        # 背面：角色右手臂显示在观者右侧
        back = [
            ("head", 5, 8, 0), ("body", 5, 8, 8),
            ("rarm", 5, 16, 8), ("larm", 5, 4, 8),
            ("rleg", 5, 12, 20), ("lleg", 5, 8, 20),
        ]

        scale = max(1, min((self.width() - 56) // 44,
                            (self.height() - 34) // 32))
        vw, vh = 20 * scale, 32 * scale
        gap = 10
        x0 = max(0, (self.width() - (vw * 2 + gap)) // 2)
        y0 = max(0, (self.height() - vh) // 2)

        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        for i, layout in enumerate((front, back)):
            ox = x0 + i * (vw + gap)
            p.setBrush(QColor(255, 255, 255, 14))
            p.drawRect(QRectF(ox - 2, y0 - 2, vw + 4, vh + 4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for part, idx, dx, dy in layout:
                rect = SKIN_FACES[(part, "base")][idx]
                sx, sy, sw, sh = rect
                tgt_x = ox + (dx if i == 0 else (20 - dx - sw)) * scale
                tgt_y = y0 + dy * scale
                p.drawPixmap(QRectF(tgt_x, tgt_y, sw * scale, sh * scale),
                             pix, QRectF(sx, sy, sw, sh))

        f = p.font()
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 160))
        p.drawText(QRectF(x0, y0 - 16, vw, 14),
                   Qt.AlignmentFlag.AlignCenter, "正面")
        p.drawText(QRectF(x0 + vw + gap, y0 - 16, vw, 14),
                   Qt.AlignmentFlag.AlignCenter, "背面")
        p.end()


# ==========================================================================
# 同步到游戏
# ==========================================================================
SKIN_PACK_NAME = "plainmc_skin"


def skin_to_slim(png_bytes):
    """把 64×64 的「宽体」皮肤转换成「细体」(slim / Alex) 皮肤。

    两者只有两条手臂不同：slim 手臂宽 3 像素（wide 是 4），
    贴图里前/后面各少 1 像素，左右侧面不变；头/身体/腿布局完全一致。
    离线玩家约有一半会被分配成 slim 模型，所以两种都要提供。
    """
    img = QImage()
    if not img.loadFromData(png_bytes):
        return png_bytes
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    if img.width() != SKIN_SIZE or img.height() != SKIN_SIZE:
        return png_bytes
    out = img.copy()
    for part in ("rarm", "larm"):
        for layer in ("base", "over"):
            ox, oy = SLIM_PART_ORIGIN[(part, layer)]
            wfaces = SKIN_FACES[(part, layer)]
            sfaces = SLIM_ARM_FACES[part]
            # 先清空该层的手臂区域（wide 的手臂占 16 列，slim 只占 14 列，
            # 残留的宽体像素必须清掉，否则会多出两条杂边）
            for j in range(16):
                for i in range(16):
                    out.setPixelColor(ox + i, oy + j, QColor(0, 0, 0, 0))
            for k in range(6):
                wx, wy, ww, wh = wfaces[k]
                sx, sy, sw, sh = sfaces[k]
                for j in range(sh):
                    for i in range(sw):
                        # 宽面缩窄时取前几列（4->3）
                        src_i = wx + (i if i < ww else ww - 1)
                        out.setPixelColor(ox + sx + i, oy + sy + j,
                                          img.pixelColor(src_i, wy + j))
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    out.save(buf, "PNG")
    return bytes(buf.data())


def _skin_pack_format(version_id):
    """资源包格式号。

    客户端只接受 pack_format <= 自己支持的版本（新的会被拒绝），
    所以这里保守地取一个偏小的值；老版本(<=1.12)用旧格式 3。
    """
    import re as _re
    m = _re.match(r"(\d+)\.(\d+)", str(version_id or ""))
    if not m:
        return 6
    major, minor = int(m.group(1)), int(m.group(2))
    if (major, minor) <= (1, 12):
        return 3
    if (major, minor) <= (1, 14):
        return 4
    if (major, minor) <= (1, 16):
        return 5
    if (major, minor) <= (1, 18):
        return 7
    if (major, minor) <= (1, 19):
        return 8
    if (major, minor) <= (1, 20):
        return 10
    if major == 1 and minor == 21:
        return 34
    return 6


def skin_pack_write(game_root, png_bytes, version_id=None):
    """生成资源包覆盖默认皮肤，并写进 options.txt 使其生效。

    Minecraft 的默认皮肤在客户端 jar 的
    assets/minecraft/textures/entity/player/{wide,slim}/{steve,alex}.png，
    资源包优先级高于 jar，所以**离线账号也能直接换皮肤**。
    wide 与 slim 两套都写：离线玩家约一半会被分配 slim 模型。
    返回 (zip 路径, 是否改写了 options.txt)。
    """
    rp_dir = os.path.join(game_root, "resourcepacks")
    os.makedirs(rp_dir, exist_ok=True)
    zip_path = os.path.join(rp_dir, SKIN_PACK_NAME + ".zip")
    slim_bytes = skin_to_slim(png_bytes)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pack.mcmeta", json.dumps({
            "pack": {
                "pack_format": _skin_pack_format(version_id),
                "description": "PlainMC 自定义皮肤",
            }
        }, ensure_ascii=False))
        for name in ("wide/steve.png", "wide/alex.png"):
            z.writestr(f"assets/minecraft/textures/entity/player/{name}",
                       png_bytes)
        for name in ("slim/steve.png", "slim/alex.png"):
            z.writestr(f"assets/minecraft/textures/entity/player/{name}",
                       slim_bytes)
    changed = _options_enable_pack(game_root, SKIN_PACK_NAME)
    return zip_path, changed


def _options_enable_pack(game_root, pack_name):
    """把资源包加入 options.txt 的 resourcePacks 列表（保留其它设置）。"""
    opt = os.path.join(game_root, "options.txt")
    lines = []
    if os.path.isfile(opt):
        try:
            with open(opt, encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except Exception:
            lines = []
    entry = "file/" + pack_name
    out = []
    replaced = False
    for ln in lines:
        if ln.startswith("resourcePacks:"):
            items = []
            try:
                val = ln.split(":", 1)[1].strip()
                if val.startswith("["):
                    items = json.loads(val)
            except Exception:
                items = []
            if entry not in items:
                items.append(entry)
            out.append("resourcePacks:" + json.dumps(items))
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append("resourcePacks:" + json.dumps([entry]))
    try:
        with open(opt, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        return True
    except Exception:
        return False


def skin_save_to_game(game_root, png_bytes, name="skin"):
    """把皮肤另存一份到 game_root/skins/ 便于备份。"""
    d = os.path.join(game_root, "skins")
    os.makedirs(d, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "skin"
    path = os.path.join(d, safe + ".png")
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


def skin_upload_mojang(access_token, png_bytes, variant="classic"):
    """把皮肤上传到正版账号（Mojang profile API），联机时别人也能看到。"""
    import urllib.request
    boundary = "----PlainMCSkinBoundary7MA4YWxkTrZu0gW"
    parts = []
    parts.append(f"--{boundary}\r\n"
                 'Content-Disposition: form-data; name="variant"\r\n\r\n'
                 f"{variant}\r\n".encode())
    parts.append(f"--{boundary}\r\n"
                 'Content-Disposition: form-data; name="file"; filename="skin.png"\r\n'
                 "Content-Type: image/png\r\n\r\n".encode())
    body = b"".join(parts) + png_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "https://api.minecraftservices.com/minecraft/profile/skins",
        data=body, method="PUT",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


# ==========================================================================
# 页面
# ==========================================================================
class SkinEditorPage(QWidget):
    """皮肤编辑器页：左边工具、中间画布、右边预览与同步。"""

    def __init__(self, main=None, parent=None):
        super().__init__(parent)
        self.main = main
        self._build_ui()
        self._new_skin()

    # ---------- 界面 ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QLabel("皮肤编辑器")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        hint = QLabel("在中间的 64×64 贴图上直接绘制（方格即像素）。"
                      "画完后点右侧「同步到游戏」，皮肤会自动替换到游戏里。")
        hint.setObjectName("HintText")
        hint.setWordWrap(True)
        root.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        # 画布要先建好：工具面板的撤销/重做按钮要连接到它
        self.canvas = SkinCanvas()

        body.addWidget(self._build_tools(), 0)

        center = QVBoxLayout()
        center.setSpacing(8)
        wrap = QScrollArea()
        wrap.setWidget(self.canvas)
        wrap.setWidgetResizable(False)
        wrap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center.addWidget(wrap, 1)
        body.addLayout(center, 1)

        body.addWidget(self._build_side(), 0)

    def _build_tools(self):
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        t = QLabel("工具")
        t.setObjectName("SubTitle")
        lay.addWidget(t)

        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        for name, text in (("pencil", "✏️ 画笔"), ("eraser", "🧽 橡皮"),
                           ("bucket", "🪣 油漆桶"), ("picker", "💧 取色器")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setFixedWidth(112)
            b.clicked.connect(lambda _, n=name: self._set_tool(n))
            self.tool_group.addButton(b)
            lay.addWidget(b)
            if name == "pencil":
                b.setChecked(True)

        lay.addSpacing(6)
        cl = QLabel("颜色")
        cl.setObjectName("SubTitle")
        lay.addWidget(cl)

        grid = QGridLayout()
        grid.setSpacing(2)
        for i, hexc in enumerate(SKIN_PALETTE):
            b = QPushButton()
            b.setFixedSize(20, 20)
            b.setStyleSheet(
                f"background:{hexc};border:1px solid rgba(0,0,0,0.3);"
                f"border-radius:3px;")
            b.clicked.connect(lambda _, c=hexc: self._set_color(QColor(c)))
            grid.addWidget(b, i // 8, i % 8)
        lay.addLayout(grid)

        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(112, 30)
        self.color_btn.setText("自定义颜色")
        self.color_btn.clicked.connect(self._pick_color)
        lay.addWidget(self.color_btn)

        self.swatch = QLabel()
        self.swatch.setFixedHeight(18)
        lay.addWidget(self.swatch)

        lay.addSpacing(6)
        bl = QLabel("笔刷粗细")
        bl.setObjectName("SubTitle")
        lay.addWidget(bl)
        self.brush_combo = QComboBox()
        self.brush_combo.addItems(["1 像素", "2 像素", "3 像素"])
        self.brush_combo.currentIndexChanged.connect(
            lambda i: setattr(self.canvas, "brush", i + 1))
        lay.addWidget(self.brush_combo)

        lay.addSpacing(6)
        sl = QLabel("缩放")
        sl.setObjectName("SubTitle")
        lay.addWidget(sl)
        self.zoom_combo = QComboBox()
        for z in (4, 6, 8, 12, 16):
            self.zoom_combo.addItem(f"{z}×", z)
        self.zoom_combo.setCurrentIndex(2)
        self.zoom_combo.currentIndexChanged.connect(
            lambda i: self.canvas.set_zoom(self.zoom_combo.itemData(i)))
        lay.addWidget(self.zoom_combo)

        self.grid_chk = QCheckBox("显示像素网格")
        self.grid_chk.setChecked(True)
        self.grid_chk.toggled.connect(
            lambda v: (setattr(self.canvas, "grid", v), self.canvas.update()))
        lay.addWidget(self.grid_chk)

        self.ovl_chk = QCheckBox("显示部位分区")
        self.ovl_chk.setChecked(True)
        self.ovl_chk.toggled.connect(
            lambda v: (setattr(self.canvas, "overlay", v), self.canvas.update()))
        lay.addWidget(self.ovl_chk)

        lay.addSpacing(6)
        hb = QHBoxLayout()
        self.undo_btn = QPushButton("↶ 撤销")
        self.undo_btn.clicked.connect(self.canvas.undo)
        self.redo_btn = QPushButton("↷ 重做")
        self.redo_btn.clicked.connect(self.canvas.redo)
        hb.addWidget(self.undo_btn)
        hb.addWidget(self.redo_btn)
        lay.addLayout(hb)

        lay.addSpacing(6)
        b_new = QPushButton("新建空白皮肤")
        b_new.clicked.connect(self._new_skin)
        lay.addWidget(b_new)
        b_imp = QPushButton("导入皮肤 PNG")
        b_imp.clicked.connect(self._import_png)
        lay.addWidget(b_imp)
        b_tpl = QPushButton("载入示例皮肤")
        b_tpl.clicked.connect(self._load_template)
        lay.addWidget(b_tpl)
        b_clr = QPushButton("清空画布")
        b_clr.clicked.connect(self.canvas.clear_all)
        lay.addWidget(b_clr)

        lay.addStretch(1)
        self.canvas.picked.connect(self._set_color)
        self.canvas.changed.connect(self._on_changed)
        return card

    def _build_side(self):
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        t = QLabel("预览")
        t.setObjectName("SubTitle")
        lay.addWidget(t)

        self.doll = SkinDoll()
        lay.addWidget(self.doll, 1)

        self.status = QLabel("就绪")
        self.status.setObjectName("HintText")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.acct_combo = QComboBox()
        self.acct_combo.setToolTip("选择要同步到哪个账号")
        lay.addWidget(self.acct_combo)

        b_save = QPushButton("💾 保存 PNG")
        b_save.clicked.connect(self._save_png)
        lay.addWidget(b_save)

        b_sync = QPushButton("✅ 同步到游戏")
        b_sync.setObjectName("PrimaryButton")
        b_sync.clicked.connect(self._sync_to_game)
        lay.addWidget(b_sync)

        note = QLabel("提示：同步后需要重启游戏才会生效。")
        note.setObjectName("HintText")
        note.setWordWrap(True)
        lay.addWidget(note)
        return card

    # ---------- 状态 ----------
    def _on_changed(self):
        self.doll.set_image(self.canvas.image)
        self.undo_btn.setEnabled(self.canvas.can_undo())
        self.redo_btn.setEnabled(self.canvas.can_redo())

    def _set_tool(self, name):
        self.canvas.tool = name

    def _set_color(self, c):
        self.canvas.color = QColor(c)
        self.swatch.setStyleSheet(
            f"background:{self.canvas.color.name()};border:1px solid rgba(0,0,0,0.3);"
            f"border-radius:3px;")

    def _pick_color(self):
        from PyQt6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(self.canvas.color, self, "选择颜色")
        if c.isValid():
            self._set_color(c)

    def _new_skin(self):
        self.canvas.snapshot()
        self.canvas.image.fill(QColor(0, 0, 0, 0))
        self.canvas.update()
        self._on_changed()
        self.status.setText("已新建空白皮肤（透明 = 未绘制）")

    def _import_png(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择皮肤 PNG", "", "PNG 图片 (*.png)")
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, "导入失败", "无法读取该图片。")
            return
        self.canvas.load_image(img)
        self.status.setText(f"已导入：{os.path.basename(path)}")

    def _load_template(self):
        """载入示例皮肤：优先用本地游戏资源里的 Steve，否则画一个简易人。"""
        img = self._find_ste()
        if img is not None:
            self.canvas.load_image(img)
            self.status.setText("已载入示例皮肤（Steve）")
        else:
            self.canvas.snapshot()
            im = self.canvas.image
            im.fill(QColor(0, 0, 0, 0))
            skin = QColor("#e0ac69")
            shirt = QColor("#3aa0d8")
            pants = QColor("#3b4b9c")
            for part, col in (("head", skin), ("body", shirt),
                              ("rarm", shirt), ("larm", shirt),
                              ("rleg", pants), ("lleg", pants)):
                for f in SKIN_FACES[(part, "base")]:
                    x, y, w, h = f
                    for j in range(h):
                        for i in range(w):
                            im.setPixelColor(x + i, y + j, col)
            for i in range(2):
                im.setPixelColor(10 + i * 3, 11, QColor("#ffffff"))
                im.setPixelColor(11 + i * 3, 11, QColor("#3b2a1a"))
            self.canvas.update()
            self._on_changed()
            self.status.setText("已载入内置示例皮肤")

    def _find_ste(self):
        """在本机游戏资源里找默认皮肤（当模板用）。"""
        roots = []
        try:
            roots.append(os.path.join(self.main.core.game_root, "versions"))
        except Exception:
            pass
        for base in roots:
            if not os.path.isdir(base):
                continue
            for dirpath, _dirs, files in os.walk(base):
                for fn in files:
                    if not fn.endswith(".jar"):
                        continue
                    try:
                        import zipfile
                        with zipfile.ZipFile(os.path.join(dirpath, fn)) as z:
                            data = z.read(
                                "assets/minecraft/textures/entity/player/"
                                "wide/steve.png")
                    except Exception:
                        continue
                    img = QImage()
                    img.loadFromData(data)
                    if not img.isNull():
                        return img.convertToFormat(QImage.Format.Format_ARGB32)
        return None

    def _save_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存皮肤", "skin.png", "PNG 图片 (*.png)")
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(self.canvas.to_png_bytes())
            self.status.setText(f"已保存：{path}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    # ---------- 同步 ----------
    def _accounts(self):
        out = []
        try:
            for a in self.main.accounts:
                out.append(a)
        except Exception:
            pass
        return out

    def _refresh_accounts(self):
        self.acct_combo.clear()
        for a in self._accounts():
            kind = "正版" if a.get("type") == "microsoft" else "离线"
            self.acct_combo.addItem(f"{a.get('name', '?')}（{kind}）",
                                    a.get("uuid", ""))

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh_accounts()

    def _target_version(self):
        """目标 MC 版本：取已安装的最新版本（用于资源包 pack_format）。"""
        try:
            vs = [v.get("id") for v in self.main.core.list_installed()
                  if v.get("id")]
            if vs:
                vs.sort(key=lambda s: [int(x) for x in
                                       s.replace("-", ".").split(".")[:3]
                                       if x.isdigit()] or [0])
                return vs[-1]
        except Exception:
            pass
        return None

    def _sync_to_game(self):
        try:
            game_root = self.main.core.game_root
        except Exception:
            QMessageBox.warning(self, "同步失败", "无法获取游戏目录。")
            return
        png = self.canvas.to_png_bytes()

        # 1) 备份一份到 skins/
        acc = self._current_account()
        try:
            skin_save_to_game(game_root, png,
                              acc.get("name") if acc else "skin")
        except Exception:
            pass

        # 2) 资源包覆盖默认皮肤（离线账号也生效）
        try:
            zip_path, _changed = skin_pack_write(game_root, png,
                                                 self._target_version())
        except Exception as e:
            QMessageBox.warning(self, "同步失败", f"写入资源包出错：{e}")
            return

        msg = [f"✅ 已写入资源包：\n{zip_path}",
               f"✅ 已备份到：{os.path.join(game_root, 'skins')}"]

        # 3) 正版账号：顺便上传到 Mojang，联机时别人也能看到
        if acc and acc.get("type") == "microsoft" and acc.get("access_token"):
            try:
                tok = self.main._ensure_account_token(acc)
                if tok:
                    skin_upload_mojang(tok["access_token"], png)
                    msg.append("✅ 已上传到正版账号（联机时其他玩家也能看到）")
                else:
                    msg.append("⚠️ 正版账号令牌已过期，上传失败（本地已生效）")
            except Exception as e:
                msg.append(f"⚠️ 上传到正版账号失败：{e}（本地已生效）")
        else:
            msg.append("ℹ️ 当前为离线账号：皮肤通过资源包生效，"
                       "联机时其他玩家看不到。")

        msg.append("请重启游戏后查看效果。")
        self.status.setText(msg[0])
        QMessageBox.information(self, "同步完成", "\n".join(msg))

    def _current_account(self):
        uid = self.acct_combo.currentData()
        for a in self._accounts():
            if a.get("uuid") == uid:
                return a
        return None


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()  # PyInstaller --onefile 下子进程(spawn)必须
    main()
