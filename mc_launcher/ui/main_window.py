"""主窗口：无边框 + 渐变标题栏 + 左侧导航 + 页面栈。"""
import json
import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QApplication,
)
from PyQt6.QtCore import Qt, QPoint

from mc_launcher import __app_name__
from mc_launcher.core.minecraft import MinecraftCore, DEFAULT_MIRROR
from mc_launcher.ui import theme
from mc_launcher.ui import widgets
from mc_launcher.ui.pages import DownloadPage, LaunchPage, SettingsPage, AboutPage

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, ".plainmc_config.json")


class TitleBar(QWidget):
    def __init__(self, main):
        super().__init__()
        self.setObjectName("TitleBar")
        self.setFixedHeight(40)
        self._drag = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.setSpacing(4)

        title = QLabel(f"{__app_name__} 启动器")
        title.setObjectName("TitleBarText")
        layout.addWidget(title)
        layout.addStretch(1)

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


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.config = self._load_config()
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
        self._switch(1)  # 默认进入“下载”页
        self._center()

    # ---------- 配置 ----------
    def _load_config(self):
        defaults = {
            "theme": "light",
            "username": "Steve",
            "java_path": "",
            "memory": 2048,
            "game_dir": "",
            "mirror": DEFAULT_MIRROR,
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
        if app:
            app.setStyleSheet(theme.get_stylesheet(self.theme))

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
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # 侧边栏
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
        for icon, text, idx in [
            ("⬇", "下载", 1),
            ("⚙", "设置", 2),
            ("ℹ", "关于", 3),
        ]:
            b = widgets.make_nav_button(icon, text)
            b.clicked.connect(lambda _, i=idx: self._switch(i))
            self.nav_buttons.append((idx, b))
            s_layout.addWidget(b)
        s_layout.addStretch(1)

        body.addWidget(sidebar)

        # 页面栈
        self.stack = QStackedWidget()
        self.launch_page = LaunchPage(self)
        self.download_page = DownloadPage(self)
        self.settings_page = SettingsPage(self)
        self.about_page = AboutPage(self)
        self.stack.addWidget(self.launch_page)    # 0
        self.stack.addWidget(self.download_page)  # 1
        self.stack.addWidget(self.settings_page)  # 2
        self.stack.addWidget(self.about_page)     # 3
        body.addWidget(self.stack, 1)

        root.addLayout(body)

    def _switch(self, index):
        self.stack.setCurrentIndex(index)
        for idx, b in self.nav_buttons:
            b.setChecked(idx == index)
        # 启动页也算“选中”，让下载等按钮取消选中
        if index != 0:
            self.btn_launch.setProperty("selected", False)

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
