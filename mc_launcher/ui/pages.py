"""各功能页面：下载、启动、设置、关于。"""
import os
import subprocess

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QListWidget, QListWidgetItem, QPushButton, QFrame, QSpinBox,
    QTextEdit, QFileDialog, QMessageBox, QSizePolicy, QScrollArea, QLayout,
)
from PyQt6.QtCore import Qt, pyqtSignal, QProcess

from mc_launcher.ui import widgets
from mc_launcher.ui.worker import DownloadWorker, FetchVersionsWorker, JavaScanWorker
from mc_launcher.core.minecraft import MIRRORS


# --------------------------------------------------------------------------
# 下载页
# --------------------------------------------------------------------------
class VersionRow(QFrame):
    download_requested = pyqtSignal(str)

    def __init__(self, version, installed):
        super().__init__()
        self.setObjectName("VersionRow")
        self.vid = version["id"]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)

        info = QVBoxLayout()
        info.setSpacing(3)
        title = QLabel(version["id"])
        title.setStyleSheet("font-size:15px;font-weight:bold;")
        sub = QLabel(f"{version['type'].upper()}  ·  {version.get('releaseTime','')[:10]}")
        sub.setStyleSheet("color:#7A8295;font-size:11px;")
        info.addWidget(title)
        info.addWidget(sub)

        layout.addLayout(info)
        layout.addStretch(1)
        layout.addWidget(widgets.make_badge(version["type"]))

        btn = QPushButton("重新下载" if installed else "下载")
        btn.setObjectName("GhostButton")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self.download_requested.emit(self.vid))
        self.btn = btn
        layout.addWidget(btn)


class DownloadPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self._all_versions = []
        self._latest = {}
        self._downloading = False
        self._build_ui()
        self._fetch_versions()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        head = QLabel("下载游戏")
        head.setObjectName("PageTitle")
        self.source_label = QLabel()
        self.source_label.setObjectName("SubTitle")
        self.refresh_source()
        root.addWidget(head)
        root.addWidget(self.source_label)

        # 搜索 / 筛选栏
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("SearchEdit")
        self.search.setPlaceholderText("搜索版本，例如 1.20.1")
        self.search.textChanged.connect(self._apply_filter)
        self.filter = QComboBox()
        self.filter.addItems(["全部", "正式版", "快照", "旧版"])
        self.filter.setMinimumWidth(110)
        self.filter.currentTextChanged.connect(self._apply_filter)
        refresh = widgets.make_ghost("刷新列表")
        refresh.clicked.connect(self._fetch_versions)
        bar.addWidget(self.search, 3)
        bar.addWidget(self.filter, 1)
        bar.addWidget(refresh)
        root.addLayout(bar)

        # 版本列表
        self.list = QListWidget()
        self.list.setSpacing(8)
        root.addWidget(self.list, 1)

        # 进度面板
        self.progress_card = widgets.make_card()
        self.progress_card.setVisible(False)
        pcard = QVBoxLayout(self.progress_card)
        pcard.setContentsMargins(16, 14, 16, 14)
        pcard.setSpacing(8)
        self.p_status = QLabel("准备中…")
        self.p_status.setObjectName("HintText")
        self.p_task = QLabel("")
        self.p_task.setStyleSheet("font-size:13px;font-weight:bold;")
        self.p_bar = _make_progress()
        pcard.addWidget(self.p_status)
        pcard.addWidget(self.p_task)
        pcard.addWidget(self.p_bar)
        root.addWidget(self.progress_card)

        self._worker = None

    def _fetch_versions(self):
        self.list.clear()
        self.search.setEnabled(False)
        hint = QListWidgetItem(f"正在从「{self.main.core.mirror_name()}」获取版本清单…")
        self.list.addItem(hint)
        self._fw = FetchVersionsWorker(self.main.core)
        self._fw.finished.connect(self._on_versions)
        self._fw.error.connect(lambda e: self._show_error(e))
        self._fw.start()

    def refresh_source(self):
        self.source_label.setText(
            f"通过「{self.main.core.mirror_name()}」获取《我的世界》各版本"
            f"（含依赖库与资源文件）")

    def _on_versions(self, latest, versions):
        self._latest = latest
        self._all_versions = versions
        self.search.setEnabled(True)
        self._apply_filter()

    def _apply_filter(self):
        text = self.search.text().strip().lower()
        ftype = self.filter.currentText()
        self.list.clear()
        installed = {v["id"] for v in self.main.core.list_installed()}
        shown = 0
        for v in self._all_versions:
            if text and text not in v["id"].lower():
                continue
            if ftype == "正式版" and v["type"] != "release":
                continue
            if ftype == "快照" and v["type"] != "snapshot":
                continue
            if ftype == "旧版" and v["type"] not in ("old_alpha", "old_beta"):
                continue
            row = VersionRow(v, v["id"] in installed)
            row.download_requested.connect(self._start_download)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            shown += 1
        if shown == 0:
            self.list.addItem("没有匹配的版本")

    def _start_download(self, vid):
        if self._downloading:
            return
        self._downloading = True
        self.progress_card.setVisible(True)
        self.p_status.setText(f"开始下载 {vid} …")
        self.p_task.setText("")
        self.p_bar.setValue(0)
        self.p_bar.setFormat("准备中 %p%")

        self._worker = DownloadWorker(self.main.core, vid, with_assets=True)
        w = self._worker
        w.status.connect(self.p_status.setText)
        w.task.connect(self._on_task)
        w.progress.connect(self._on_progress)
        w.taskdone.connect(lambda n: None)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_done)
        w.start()

    def _on_task(self, name, current, total):
        self.p_task.setText(f"{name}  ({current}/{total})")
        if total:
            self.p_bar.setMaximum(total)
            self.p_bar.setValue(current)
            self.p_bar.setFormat(f"{name} %p%")

    def _on_progress(self, name, downloaded, total):
        if total:
            self.p_bar.setMaximum(100)
            self.p_bar.setValue(int(downloaded * 100 / total))
            self.p_bar.setFormat(f"{name}  {_fmt(downloaded)}/{_fmt(total)}")

    def _on_error(self, msg):
        self.p_status.setText("下载出错：" + msg)
        self._downloading = False

    def _on_done(self, info):
        self.p_status.setText(f"✅ {info.get('id')} 下载完成，可到『启动』页游玩")
        self.p_bar.setMaximum(100)
        self.p_bar.setValue(100)
        self.p_bar.setFormat("完成 100%")
        self._downloading = False
        self._apply_filter()  # 刷新“已下载”状态
        if hasattr(self.main, "launch_page"):
            self.main.launch_page.refresh_installed()

    def _show_error(self, msg):
        self.list.clear()
        self.list.addItem("获取版本失败：" + msg)


# --------------------------------------------------------------------------
# 启动页
# --------------------------------------------------------------------------
class LaunchPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self._process = None
        self._build_ui()
        self.refresh_installed()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        head = QLabel("启动游戏")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        card = widgets.make_card()
        form = QVBoxLayout(card)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(14)

        # 版本
        form.addLayout(self._field("游戏版本", self._version_combo()))
        # 玩家名
        self.username = QLineEdit(self.main.config.get("username", "Steve"))
        self.username.setObjectName("LineEdit")
        self.username.setPlaceholderText("离线玩家名")
        form.addLayout(self._field("玩家名（离线）", self.username))
        # Java
        java_row = QHBoxLayout()
        self.java_combo = QComboBox()
        self.java_combo.setMinimumHeight(36)
        self._browse_java = widgets.make_ghost("浏览…")
        self._browse_java.clicked.connect(self._on_browse_java)
        java_row.addWidget(self.java_combo, 1)
        java_row.addWidget(self._browse_java)
        form.addLayout(self._field("Java 路径", java_row))
        # 内存
        self.memory = QSpinBox()
        self.memory.setRange(512, 16384)
        self.memory.setSingleStep(256)
        self.memory.setSuffix(" MB")
        self.memory.setValue(int(self.main.config.get("memory", 2048)))
        self.memory.setMinimumHeight(36)
        form.addLayout(self._field("分配内存", self.memory))

        root.addWidget(card)

        # 启动按钮
        self.launch_btn = widgets.make_primary("▶  启动游戏")
        self.launch_btn.setMinimumHeight(46)
        self.launch_btn.clicked.connect(self._do_launch)
        root.addWidget(self.launch_btn)

        # 控制台
        console_card = widgets.make_card()
        cv = QVBoxLayout(console_card)
        cv.setContentsMargins(0, 0, 0, 0)
        self.console = QTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(160)
        cv.addWidget(self.console)
        root.addWidget(console_card, 1)

        folder_btn = widgets.make_ghost("打开游戏文件夹")
        folder_btn.clicked.connect(self._open_folder)
        root.addWidget(folder_btn)

        self._scan_java()

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

    def _version_combo(self):
        self.version_combo = QComboBox()
        self.version_combo.setMinimumHeight(36)
        return self.version_combo

    def _scan_java(self):
        self._jw = JavaScanWorker()
        self._jw.finished.connect(self._on_java)
        self._jw.start()

    def _on_java(self, items):
        self.java_combo.clear()
        cfg = self.main.config.get("java_path", "")
        for name, path in items:
            from mc_launcher.core import java as jm
            ver = jm.java_version(path) or ""
            label = f"{name}  ({ver})" if ver else name
            self.java_combo.addItem(label, path)
        if cfg:
            # 尝试选中已保存的 java
            idx = self.java_combo.findData(cfg)
            if idx >= 0:
                self.java_combo.setCurrentIndex(idx)

    def _on_browse_java(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 java.exe", "", "Java 可执行文件 (java.exe)")
        if path:
            self.java_combo.addItem(os.path.basename(os.path.dirname(path)), path)
            self.java_combo.setCurrentIndex(self.java_combo.count() - 1)

    def refresh_installed(self):
        self.version_combo.clear()
        installed = self.main.core.list_installed()
        if not installed:
            self.version_combo.addItem("（请先在『下载』页下载版本）")
            self.launch_btn.setEnabled(False)
            return
        self.launch_btn.setEnabled(True)
        for v in installed:
            self.version_combo.addItem(f"{v['id']}  [{v['type']}]", v["id"])

    def _do_launch(self):
        if getattr(self, "_proc", None) and self._proc.state() == QProcess.ProcessState.Running:
            return
        vid = self.version_combo.currentData()
        if not vid:
            QMessageBox.information(self, "提示", "请先下载一个游戏版本。")
            return
        java_path = self.java_combo.currentData()
        if not java_path or not os.path.isfile(java_path):
            QMessageBox.warning(self, "缺少 Java", "请先选择有效的 Java 路径。")
            return
        username = self.username.text().strip() or "Steve"
        mem = self.memory.value()

        # 保存配置
        self.main.config["username"] = username
        self.main.config["java_path"] = java_path
        self.main.config["memory"] = mem
        self.main.save_config()

        try:
            cmd, opts = self.main.core.build_launch_command(
                vid, username, java_path, max_memory=mem)
        except Exception as e:
            QMessageBox.critical(self, "启动失败", str(e))
            return

        self.console.clear()
        self.console.append("▶ 启动命令：")
        self.console.append("  " + " ".join(cmd))
        self.console.append("—" * 30)

        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._proc.setWorkingDirectory(opts.get("cwd", self.main.core.game_root))
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.start(cmd[0], cmd[1:])

    def _on_output(self):
        data = self._proc.readAllStandardOutput().data().decode("utf-8", "replace")
        self.console.append(data.rstrip("\n"))

    def _on_finished(self, code, status):
        self.console.append("—" * 30)
        self.console.append(f"游戏进程结束，退出码 {code}")

    def _open_folder(self):
        path = self.main.core.game_root
        if os.path.isdir(path):
            os.startfile(path) if os.name == "nt" else subprocess.run(["xdg-open", path])


# --------------------------------------------------------------------------
# 设置页
# --------------------------------------------------------------------------
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

        card = widgets.make_card()
        form = QVBoxLayout(card)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(14)

        # 游戏目录
        gdir_row = QHBoxLayout()
        self.gdir = QLineEdit(self.main.core.game_root)
        self.gdir.setObjectName("LineEdit")
        self.gdir.setReadOnly(True)
        browse = widgets.make_ghost("更改…")
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

        # 默认内存
        self.mem = QSpinBox()
        self.mem.setRange(512, 16384)
        self.mem.setSingleStep(256)
        self.mem.setSuffix(" MB")
        self.mem.setValue(int(self.main.config.get("memory", 2048)))
        self.mem.setMinimumHeight(36)
        form.addLayout(self._field("默认分配内存", self.mem))

        root.addWidget(card)
        root.addStretch(1)

        save = widgets.make_primary("保存设置")
        save.clicked.connect(self._on_save)
        root.addWidget(save)

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

    def _on_save(self):
        self.main.config["memory"] = self.mem.value()
        new_dir = self.gdir.text()
        self.main.save_config()
        if new_dir != self.main.core.game_root:
            self.main.set_game_root(new_dir)
            if hasattr(self.main, "launch_page"):
                self.main.launch_page.refresh_installed()
        QMessageBox.information(self, "已保存", "设置已保存。")


# --------------------------------------------------------------------------
# 关于页
# --------------------------------------------------------------------------
class AboutPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        from mc_launcher import __app_name__, __version__
        head = QLabel("关于")
        head.setObjectName("PageTitle")
        root.addWidget(head)

        card = widgets.make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(8)
        box.addWidget(QLabel(f"{__app_name__}  v{__version__}"))
        box.addWidget(QLabel("一个仿 PCL2 美术风格的《我的世界》启动器（Python + PyQt6）。"))
        box.addWidget(QLabel("· 支持从官方源 / 国内镜像(BMCLAPI) 下载正式版 / 快照 / 旧版"))
        box.addWidget(QLabel("· 自动处理依赖库、原生库与资源文件"))
        box.addWidget(QLabel("· 离线账号即可游玩（正版验证需接入微软账号，后续扩展）"))
        root.addWidget(card)
        root.addStretch(1)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _make_progress():
    from PyQt6.QtWidgets import QProgressBar
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
