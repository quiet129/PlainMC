# -*- coding: utf-8 -*-
"""分层内存优化实测（安全版）。

用法：  py tools/test_memory_optimize.py

关键：L3 候选先做保护校验，测试阶段不会误杀宿主进程
（v1.1.0 曾因未排除父进程链误杀 WorkBuddy 宿主导致会话中断）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 项目根目录（本脚本位于 tools/ 下）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import PlainMC as pm
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
app = QApplication(sys.argv)

print("=" * 74)
print("【祖先链保护】")
anc = pm._ancestor_pids()
print(f"  受保护的祖先 pid 数: {len(anc)}")
for p in sorted(anc):
    print(f"    pid={p}")

print()
print("=" * 74)
print("【基础 API】")
info = pm.system_memory()
print(f"  整机: 已用 {info['used_mb']:.0f} MB | 可用 {info['available_mb']:.0f} MB "
      f"| 总 {info['total_mb']:.0f} MB | 占用 {info['load_percent']}%")
print(f"  启动器自身: 工作集 {pm.process_memory_mb()[0]:.0f} MB / "
      f"私有 {pm.process_memory_mb()[1]:.0f} MB")
print(f"  推荐游戏内存: {pm.recommend_game_memory():.0f} MB")
print(f"  推荐(60 mods): {pm.recommend_game_memory(mod_count=60):.0f} MB")

print()
print("=" * 74)
print("【L3 候选】")
hogs = pm.find_memory_hogs(min_mb=150)
for pid, name, private, path in hogs[:10]:
    print(f"  {pid:>7}  {private:>7.0f} MB  {name}")
print(f"  共 {len(hogs)} 个候选")

print()
print("=" * 74)
print("【保护校验】关键 / 宿主 / 祖先 都不该出现在候选里")
names = {n.lower() for _p, n, _w, _path in hogs}
pids = {p for p, _n, _w, _path in hogs}
bad = 0
for critical in ("explorer.exe", "svchost.exe", "cmd.exe", "msedge.exe",
                 "chrome.exe", "system", "workbuddy.exe", "sandbox-cli.exe",
                 "bash.exe", "python.exe"):
    if critical in names:
        print(f"  x {critical:18s} 出现在候选里！")
        bad += 1
print(f"  名单类检查: {'全部通过' if not bad else f'{bad} 项失败'}")
overlap = pids & anc
print(f"  祖先链重叠: {len(overlap)} 个 "
      f"{'（正确，已被排除）' if not overlap else 'x 仍被包含！'}")
if overlap:
    bad += 1

print()
print("=" * 74)
print("【L1 轻度】")
r1 = pm.run_memory_optimize(level=1, main=None)
print(f"  整机可用 {r1['before']['available_mb']:.0f} -> "
      f"{r1['after']['available_mb']:.0f} MB  (净增 {r1['freed_mb']:.0f} MB)")
print(f"  启动器 {r1['before_mb']:.0f} -> {r1['after_mb']:.0f} MB")

print()
print("=" * 74)
print("【L2 中度】清空全系统工作集")
r2 = pm.run_memory_optimize(level=2, main=None)
print(f"  清空工作集进程数: {r2['trimmed']}")
print(f"  整机可用 {r2['before']['available_mb']:.0f} -> "
      f"{r2['after']['available_mb']:.0f} MB  (净增 {r2['freed_mb']:.0f} MB)")
print(f"  整机占用 {r2['before']['load_percent']}% -> {r2['after']['load_percent']}%")

print()
print("=" * 74)
print("【L3 深度】真实执行")
r3 = pm.run_memory_optimize(level=3, main=None, min_hog_mb=150)
print(f"  清空工作集: {r3['trimmed']} 个进程")
print(f"  结束进程  : {r3['killed']} 个")
for pid, name, mb in r3["killed_list"]:
    print(f"      pid={pid} {name} {mb:.0f} MB")
print(f"  整机可用 {r3['before']['available_mb']:.0f} -> "
      f"{r3['after']['available_mb']:.0f} MB  (净增 {r3['freed_mb']:.0f} MB)")
print(f"  整机占用 {r3['before']['load_percent']}% -> {r3['after']['load_percent']}%")

print()
print("=" * 74)
if bad:
    print(f"[T] 存在 {bad} 项保护失效 —— 不可发布")
    sys.exit(1)
print("[T] 分层优化全部通过，保护机制有效")
