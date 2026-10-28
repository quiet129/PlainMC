"""后台工作线程：下载、拉取版本清单、扫描 Java。"""
from PyQt6.QtCore import QThread, pyqtSignal

from mc_launcher.core.minecraft import MinecraftCore
from mc_launcher.core import java as java_mod


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

    def __init__(self):
        super().__init__()

    def run(self):
        try:
            result = java_mod.find_java()
        except Exception:
            result = []
        self.finished.emit(result)
