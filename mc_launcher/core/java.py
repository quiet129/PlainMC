"""自动探测系统中可用的 Java 运行时。"""
import os
import re
import shutil
import subprocess


def find_java():
    """返回 [(显示名, java可执行文件路径), ...]，按常见顺序排列。"""
    found = []

    # 1. 系统 PATH 中的 java
    try:
        p = shutil.which("java")
        if p:
            found.append(("PATH", p))
    except Exception:
        pass

    # 2. 常见的 Java 安装目录
    roots = [
        r"C:/Program Files/Java",
        r"C:/Program Files (x86)/Java",
        r"C:/Program Files/AdoptOpenJDK",
        r"C:/Program Files/Eclipse Adoptium",
        r"C:/Program Files/Zulu",
    ]
    for r in roots:
        if os.path.isdir(r):
            for d in sorted(os.listdir(r)):
                cand = os.path.join(r, d, "bin", "java.exe")
                if os.path.isfile(cand):
                    found.append((d, cand))

    # 去重，保持顺序
    seen = set()
    out = []
    for name, path in found:
        if path not in seen:
            seen.add(path)
            out.append((name, path))
    return out


def java_version(path):
    """返回 java -version 解析出的版本字符串，失败返回 None。"""
    try:
        proc = subprocess.run(
            [path, "-version"],
            capture_output=True, text=True, timeout=10,
        )
        s = (proc.stdout + proc.stderr).strip()
        m = re.search(r'version "([^"]+)"', s)
        if m:
            return m.group(1)
        first = s.split("\n")[0].strip()
        return first or None
    except Exception:
        return None


if __name__ == "__main__":
    for name, path in find_java():
        print(name, "->", path, "|", java_version(path))
