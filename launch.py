"""PlainMC 启动器入口。

运行方式：
    cd MC_Launcher
    python launch.py
（首次运行会自动用自带的 venv 安装依赖；也可先 pip install -r requirements.txt）
"""
import os
import sys

# 让解释器能找到 mc_launcher 包
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from PyQt6.QtWidgets import QApplication

from mc_launcher.ui.main_window import MainWindow


def main():
    # Qt6 默认已启用高 DPI 缩放与高清图片渲染，无需再设置 AA_* 属性
    app = QApplication(sys.argv)
    app.setApplicationName("PlainMC")
    app.setApplicationDisplayName("PlainMC 启动器")

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
