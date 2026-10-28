"""PCL2 风格的主题样式（QSS）。

通过占位符替换注入配色，避免 QSS 花括号与 f-string 冲突。
提供 light / dark 两套主题。
"""
from mc_launcher import __app_name__

# 两套主题的配色
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
QWidget {
    font-family: "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
    color: %%TEXT%%;
    background: %%BG%%;
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

/* 控制台 */
QTextEdit#Console {
    background: %%CONSOLE_BG%%;
    color: %%CONSOLE_TEXT%%;
    border: 1px solid %%CARD_BORDER%%;
    border-radius: 10px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
}
QScrollBar:vertical { width: 9px; background: transparent; }
QScrollBar::handle:vertical {
    background: %%CARD_BORDER%%; border-radius: 5px;
}
QScrollArea { border: none; background: transparent; }
"""


def get_stylesheet(theme="light"):
    colors = THEMES.get(theme, THEMES["light"])
    sheet = _TEMPLATE
    for token, value in colors.items():
        sheet = sheet.replace("%%" + token + "%%", value)
    return sheet


def theme_colors(theme="light"):
    return THEMES.get(theme, THEMES["light"])
