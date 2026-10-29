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

# 项目根目录（本脚本位于 tools/ 下）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = PROJECT_ROOT
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
