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

import mc_items as mi
import mc_textures as mt

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

CATEGORIES = ["building", "redstone", "equipment", "misc", ""]

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
        px = mt.pixmap(self.item["id"], 40)
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
            it = mi.get_item(self.item_id)
            px = mt.pixmap(self.item_id, 48)
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
        items = mi.search_items(q, limit=600)
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
        self.recipes = []          # 配方字典列表
        self.current = None        # 当前配方 dict
        self._selected_slot = None
        self._ns = "crafted"       # 数据包命名空间
        self._build_ui()
        self._new_recipe(silent=True)

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
        self.cat_combo.addItems(CATEGORIES)
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
            reszh = mi.get_item(res)["zh"] if res and mi.get_item(res) else (res or "产物")
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
