"""可复用的小部件与样式辅助。"""
from PyQt6.QtWidgets import QPushButton, QLabel, QFrame
from PyQt6.QtCore import Qt


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
