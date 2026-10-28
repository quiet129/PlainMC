"""《我的世界》官方版本下载与启动核心。

数据布局（仿官方 .minecraft）：
    <game_root>/
        versions/<id>/<id>.json   版本元数据
        versions/<id>/<id>.jar    客户端
        libraries/...             依赖库
        assets/indexes/<index>.json
        assets/objects/<xx>/<hash>
        versions/<id>/natives/   解包后的原生库

进度回调 cb(event) 的事件格式：
    {"type":"status",   "msg": str}
    {"type":"task",     "name": str, "current": int, "total": int}
    {"type":"progress", "name": str, "downloaded": int, "total": int}
    {"type":"taskdone", "name": str}
    {"type":"error",    "msg": str}
    {"type":"done",     "info": dict}
"""
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
import uuid
import zipfile

LIB_BASE = "https://libraries.minecraft.net/"
ASSET_BASE = "https://resources.download.minecraft.net/"
USER_AGENT = "PlainMC/1.0 (+https://github.com/plainmc)"

# 我们不支持任何可选特性（演示模式 / 自定义分辨率 / quickPlay 等）
SUPPORTED_FEATURES = set()

# --------------------------------------------------------------------------
# 下载镜像源
# --------------------------------------------------------------------------
# 每个镜像提供：
#   name     —— 界面展示名
#   manifest —— 版本清单地址
#   rewrite  —— 官方 URL 前缀 → 镜像 URL 前缀 的映射（None 表示不重写）
# BMCLAPI 实测：launchermeta/launcher/piston-meta/piston-data 仅替换主机、
# 路径保持不变；resources→/assets/、libraries→/maven/ 为带前缀映射。
MIRRORS = {
    "official": {
        "name": "官方源 (Mojang)",
        "manifest": "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json",
        "rewrite": None,
    },
    "bmclapi": {
        "name": "国内镜像 (BMCLAPI)",
        "manifest": "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json",
        "rewrite": {
            "https://launchermeta.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "http://launchermeta.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "https://launcher.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "http://launcher.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "https://piston-meta.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "http://piston-meta.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "https://piston-data.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "http://piston-data.mojang.com/": "https://bmclapi2.bangbang93.com/",
            "https://resources.download.minecraft.net/": "https://bmclapi2.bangbang93.com/assets/",
            "http://resources.download.minecraft.net/": "https://bmclapi2.bangbang93.com/assets/",
            "https://libraries.minecraft.net/": "https://bmclapi2.bangbang93.com/maven/",
            "http://libraries.minecraft.net/": "https://bmclapi2.bangbang93.com/maven/",
            "https://textures.minecraft.net/": "https://bmclapi2.bangbang93.com/textures/",
            "http://textures.minecraft.net/": "https://bmclapi2.bangbang93.com/textures/",
        },
    },
}

DEFAULT_MIRROR = "bmclapi"


# --------------------------------------------------------------------------
# 网络工具
# --------------------------------------------------------------------------
def _http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_file(url, dest, cb=None, name="文件"):
    """流式下载到 dest，通过 cb 上报字节进度。文件已存在则跳过。"""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        if cb:
            cb({"type": "progress", "name": name,
                "downloaded": os.path.getsize(dest), "total": os.path.getsize(dest)})
        return dest

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", "0") or "0")
            downloaded = 0
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if cb:
                        cb({"type": "progress", "name": name,
                            "downloaded": downloaded, "total": total})
    except urllib.error.HTTPError as e:
        # 报错带上具体 URL，便于定位是哪个文件失败
        raise RuntimeError(f"HTTP {e.code}（{name}）: {url}") from e
    shutil.move(tmp, dest)
    return dest


# --------------------------------------------------------------------------
# 版本 JSON 解析（含 inheritsFrom 继承）
# --------------------------------------------------------------------------
def _library_allowed(lib):
    """根据 rules 判断库是否适用于 Windows。"""
    rules = lib.get("rules")
    if not rules:
        return True
    allow = False
    for rule in rules:
        ok = True
        if "os" in rule:
            if rule["os"].get("name") != "windows":
                ok = False
        if "features" in rule:
            for feat, val in rule["features"].items():
                if val is True and feat not in SUPPORTED_FEATURES:
                    ok = False
        if ok:
            allow = (rule.get("action") == "allow")
    return allow


def _lib_maven_path(name):
    """由 maven 坐标 group:artifact:version[:classifier] 计算相对路径。"""
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) >= 4 else ""
    ext = parts[4] if len(parts) >= 5 else "jar"
    group_path = group.replace(".", "/")
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{ext}"
    return os.path.join(group_path, artifact, version, filename)


class MinecraftCore:
    def __init__(self, game_root, mirror=None):
        self.game_root = os.path.abspath(game_root)
        self.versions_dir = os.path.join(self.game_root, "versions")
        self.libraries_dir = os.path.join(self.game_root, "libraries")
        self.assets_dir = os.path.join(self.game_root, "assets")
        self._manifest = None
        self.set_mirror(mirror or DEFAULT_MIRROR)
        for d in (self.versions_dir, self.libraries_dir, self.assets_dir):
            os.makedirs(d, exist_ok=True)

    # ---- 镜像 ----
    def set_mirror(self, key):
        """切换下载镜像源（'official' / 'bmclapi' 等）。会清空已缓存的清单。"""
        self._mirror = MIRRORS.get(key, MIRRORS[DEFAULT_MIRROR])
        self._mirror_key = key if key in MIRRORS else DEFAULT_MIRROR
        self._manifest = None

    @property
    def mirror_key(self):
        return self._mirror_key

    def mirror_name(self):
        return self._mirror["name"]

    def _rewrite(self, url):
        """将官方下载 URL 按当前镜像重写；官方源则原样返回。"""
        rules = self._mirror.get("rewrite")
        if not rules or not url:
            return url
        # 取最长匹配前缀，避免短前缀误命中
        best = None
        for prefix, target in rules.items():
            if url.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, target)
        if best:
            return best[1] + url[len(best[0]):]
        return url

    # ---- 带回退的下载 / 取 JSON ----
    @staticmethod
    def _sha1_ok(path, sha1):
        if not sha1:
            return True
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest() == sha1

    def _download(self, official_url, dest, cb=None, name="文件", sha1=None):
        """优先走当前镜像下载；镜像缺文件（404 等）或校验失败时自动回退官方源。"""
        mirrored = self._rewrite(official_url)
        if mirrored == official_url:
            _download_file(official_url, dest, cb, name)
            if not self._sha1_ok(dest, sha1):
                raise RuntimeError(f"SHA1 校验失败（{name}）: {official_url}")
            return dest
        try:
            _download_file(mirrored, dest, cb, name)
            if self._sha1_ok(dest, sha1):
                return dest
            if cb:
                cb({"type": "status", "msg": f"镜像文件校验失败「{name}」，改用官方源…"})
        except Exception:
            if cb:
                cb({"type": "status", "msg": f"镜像不可用「{name}」，改用官方源…"})
        os.path.exists(dest) and os.remove(dest)
        _download_file(official_url, dest, cb, name)
        if not self._sha1_ok(dest, sha1):
            raise RuntimeError(f"SHA1 校验失败（{name}）: {official_url}")
        return dest

    def _fetch_json(self, url):
        """优先走镜像取 JSON；失败时回退官方源。"""
        mirrored = self._rewrite(url)
        try:
            return _http_get_json(mirrored)
        except Exception:
            if mirrored != url:
                return _http_get_json(url)
            raise

    # ---- 版本清单 ----
    def get_manifest(self):
        if self._manifest is None:
            self._manifest = _http_get_json(self._mirror["manifest"])
        return self._manifest

    def get_remote_versions(self):
        """返回 (latest, [版本列表])，版本列表含 id/type/releaseTime。"""
        m = self.get_manifest()
        versions = [
            {"id": v["id"], "type": v["type"], "releaseTime": v.get("releaseTime", "")}
            for v in m["versions"]
        ]
        return m["latest"], versions

    def _version_url(self, vid):
        m = self.get_manifest()
        for v in m["versions"]:
            if v["id"] == vid:
                return v["url"], v["type"]
        raise ValueError(f"清单中未找到版本 {vid}")

    # ---- 解析完整版本 JSON（处理继承） ----
    def resolve_version_json(self, vid):
        """下载并合并整个继承链，返回最终版本 JSON（dict）。"""
        chain = []
        visited = set()
        current = vid
        while current and current not in visited:
            visited.add(current)
            vj = self._load_or_fetch_version_json(current)
            chain.append(vj)
            current = vj.get("inheritsFrom")

        merged = {}
        for vj in reversed(chain):  # 父在前，子在后（子覆盖标量）
            for k, v in vj.items():
                if k == "libraries":
                    merged.setdefault("libraries", [])
                    merged["libraries"].extend(v)
                elif k == "arguments":
                    merged.setdefault("arguments", {"game": [], "jvm": []})
                    for side in ("game", "jvm"):
                        if side in v:
                            merged["arguments"][side].extend(v[side])
                else:
                    merged[k] = v
        return merged

    def _load_or_fetch_version_json(self, vid):
        path = os.path.join(self.versions_dir, vid, f"{vid}.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        url, _ = self._version_url(vid)
        vj = self._fetch_json(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(vj, f, ensure_ascii=False, indent=2)
        return vj

    # ---- 下载 ----
    def download_version(self, vid, with_assets=True, cb=None):
        """下载某个版本的全部内容。cb 用于进度上报。"""
        def emit(evt):
            if cb:
                cb(evt)

        try:
            emit({"type": "status", "msg": f"正在定位版本 {vid} …"})
            vj = self.resolve_version_json(vid)
            vdir = os.path.join(self.versions_dir, vid)
            os.makedirs(vdir, exist_ok=True)

            # 1. 客户端 jar
            client = vj.get("downloads", {}).get("client")
            if client and client.get("url"):
                emit({"type": "task", "name": "客户端", "current": 0, "total": 1})
                dest = os.path.join(vdir, f"{vid}.jar")
                self._download(client["url"], dest, cb, name="客户端",
                               sha1=client.get("sha1"))
                emit({"type": "taskdone", "name": "客户端"})

            # 2. 依赖库 + natives
            libs = [l for l in vj.get("libraries", []) if _library_allowed(l)]
            emit({"type": "task", "name": "依赖库", "current": 0, "total": len(libs)})
            for i, lib in enumerate(libs, 1):
                emit({"type": "task", "name": f"依赖库 {i}/{len(libs)}",
                      "current": i, "total": len(libs)})
                # 普通 artifact
                art = lib.get("downloads", {}).get("artifact")
                if art and art.get("url"):
                    rel = art.get("path") or _lib_maven_path(lib["name"])
                    self._download(art["url"],
                                  os.path.join(self.libraries_dir, rel),
                                  cb, name=f"库 {i}/{len(libs)}",
                                  sha1=art.get("sha1"))
                elif not art:  # 无 downloads，按 maven 坐标拼 URL
                    rel = _lib_maven_path(lib["name"])
                    self._download(LIB_BASE + rel.replace(os.sep, "/"),
                                  os.path.join(self.libraries_dir, rel),
                                  cb, name=f"库 {i}/{len(libs)}")
                # natives
                natives = lib.get("natives", {})
                classifiers = lib.get("downloads", {}).get("classifiers", {})
                cls = natives.get("windows")
                if cls and cls in classifiers:
                    c = classifiers[cls]
                    if c.get("url"):
                        base_name = lib["name"].split(":")[1]
                        version = lib["name"].split(":")[2]
                        rel = f"{base_name}-{version}-{cls}.jar"
                        gp = lib["name"].split(":")[0].replace(".", "/")
                        rel_path = c.get("path") or os.path.join(gp, base_name, version, rel)
                        self._download(c["url"],
                                      os.path.join(self.libraries_dir, rel_path),
                                      cb, name=f"原生库 {i}/{len(libs)}",
                                      sha1=c.get("sha1"))
            emit({"type": "taskdone", "name": "依赖库"})

            # 3. 资源文件
            if with_assets and vj.get("assetIndex") and vj["assetIndex"].get("url"):
                emit({"type": "status", "msg": "正在下载资源索引…"})
                idx = self._fetch_json(vj["assetIndex"]["url"])
                idx_name = vj.get("assets", "legacy")
                idx_dest = os.path.join(self.assets_dir, "indexes", f"{idx_name}.json")
                os.makedirs(os.path.dirname(idx_dest), exist_ok=True)
                with open(idx_dest, "w", encoding="utf-8") as f:
                    json.dump(idx, f, ensure_ascii=False)
                objects = idx.get("objects", {})
                keys = list(objects.keys())
                emit({"type": "task", "name": "资源", "current": 0, "total": len(keys)})
                done = 0
                for key in keys:
                    done += 1
                    obj = objects[key]
                    h = obj["hash"]
                    sub = h[:2]
                    url = f"{ASSET_BASE}{sub}/{h}"
                    dest = os.path.join(self.assets_dir, "objects", sub, h)
                    self._download(url, dest, cb, name=f"资源 {done}/{len(keys)}",
                                   sha1=h)
                    if done % 25 == 0:
                        emit({"type": "task", "name": f"资源 {done}/{len(keys)}",
                              "current": done, "total": len(keys)})
                emit({"type": "taskdone", "name": "资源"})

            # 写启动信息
            info = {
                "id": vid,
                "type": vj.get("type", "release"),
                "mainClass": vj.get("mainClass"),
                "assets": vj.get("assets", "legacy"),
                "releaseTime": vj.get("releaseTime", ""),
                "downloadedAt": __import__("datetime").datetime.now().isoformat(),
                "withAssets": with_assets,
            }
            with open(os.path.join(vdir, "launch.info.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)

            emit({"type": "done", "msg": f"版本 {vid} 下载完成", "info": info})
            return info
        except Exception as e:
            emit({"type": "error", "msg": str(e)})
            raise

    # ---- 已安装版本 ----
    def list_installed(self):
        out = []
        if not os.path.isdir(self.versions_dir):
            return out
        for d in sorted(os.listdir(self.versions_dir)):
            jar = os.path.join(self.versions_dir, d, f"{d}.jar")
            info_path = os.path.join(self.versions_dir, d, "launch.info.json")
            if os.path.isfile(jar):
                info = {}
                if os.path.isfile(info_path):
                    try:
                        with open(info_path, "r", encoding="utf-8") as f:
                            info = json.load(f)
                    except Exception:
                        pass
                out.append({
                    "id": d,
                    "type": info.get("type", "release"),
                    "mainClass": info.get("mainClass"),
                    "assets": info.get("assets", "legacy"),
                })
        return out

    # ---- 离线账号 UUID ----
    @staticmethod
    def offline_uuid(username):
        h = hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest()
        b = bytearray(h)
        b[6] = (b[6] & 0x0F) | 0x30  # version 3
        b[8] = (b[8] & 0x3F) | 0x80  # variant
        return str(uuid.UUID(bytes=bytes(b)))

    # ---- 构建启动命令 ----
    def build_launch_command(self, vid, username, java_path,
                             max_memory=2048, min_memory=None):
        vj = self.resolve_version_json(vid)
        vdir = os.path.join(self.versions_dir, vid)
        natives_dir = os.path.join(vdir, "natives")
        os.makedirs(natives_dir, exist_ok=True)

        # 收集 classpath（客户端 + 普通库），并解包 natives
        classpath = [os.path.join(vdir, f"{vid}.jar")]
        for lib in vj.get("libraries", []):
            if not _library_allowed(lib):
                continue
            art = lib.get("downloads", {}).get("artifact")
            if art and art.get("url"):
                rel = art.get("path") or _lib_maven_path(lib["name"])
            else:
                rel = _lib_maven_path(lib["name"])
            jar_path = os.path.join(self.libraries_dir, rel)
            if os.path.isfile(jar_path):
                classpath.append(jar_path)
            # 解包原生库
            natives = lib.get("natives", {})
            classifiers = lib.get("downloads", {}).get("classifiers", {})
            cls = natives.get("windows")
            if cls and cls in classifiers and classifiers[cls].get("url"):
                base_name = lib["name"].split(":")[1]
                version = lib["name"].split(":")[2]
                rel2 = f"{base_name}-{version}-{cls}.jar"
                gp = lib["name"].split(":")[0].replace(".", "/")
                native_jar = os.path.join(self.libraries_dir, gp, base_name, version, rel2)
                if os.path.isfile(native_jar):
                    self._extract_natives(native_jar, natives_dir)

        main_class = vj.get("mainClass")
        if not main_class:
            raise ValueError("版本 JSON 缺少 mainClass")

        # JVM 参数
        jvm_args = []
        if "arguments" in vj and "jvm" in vj["arguments"]:
            for a in vj["arguments"]["jvm"]:
                if isinstance(a, str):
                    jvm_args.append(a)
                else:
                    if self._rule_allows(a.get("rules")):
                        val = a["value"]
                        jvm_args.extend(val if isinstance(val, list) else [val])
        else:
            jvm_args = ["-XX:HeapDumpPath=MojangTricksIntelDontCallThisDirectly",
                        "-Djava.library.path=${natives_directory}",
                        "-cp", "${classpath}"]

        # 游戏参数
        game_args = []
        if "arguments" in vj and "game" in vj["arguments"]:
            for a in vj["arguments"]["game"]:
                if isinstance(a, str):
                    game_args.append(a)
                else:
                    if self._rule_allows(a.get("rules")):
                        val = a["value"]
                        game_args.extend(val if isinstance(val, list) else [val])
        else:
            game_args = (vj.get("minecraftArguments", "") or "").split()

        # 占位符替换
        uid = self.offline_uuid(username)
        jvm_tokens = {
            "natives_directory": natives_dir,
            "launcher_name": "PlainMC",
            "launcher_version": "1.0.0",
            "classpath": os.pathsep.join(classpath),
            "classpath_separator": os.pathsep,
            "library_directory": self.libraries_dir,
            "version_name": vid,
        }
        jvm_args = [self._replace(t, jvm_tokens) for t in jvm_args]

        game_tokens = {
            "auth_player_name": username,
            "version_name": vid,
            "game_directory": self.game_root,
            "assets_root": self.assets_dir,
            "assets_index_name": vj.get("assets", "legacy"),
            "auth_uuid": uid,
            "auth_access_token": uid,
            "auth_xuid": "",
            "clientid": "00000000-0000-0000-0000-000000000000",
            "user_type": "mojang",
            "version_type": vj.get("type", "release"),
        }
        game_args = [self._replace(t, game_tokens) for t in game_args]
        # 兜底：清除任何未被识别的占位符（避免新版引入的字段导致 Java 报错）
        game_args = [self._clear_tokens(a) for a in game_args]
        jvm_args = [self._clear_tokens(a) for a in jvm_args]

        # 内存
        min_memory = min_memory or min(512, max_memory)
        jvm_args.insert(0, f"-Xmx{max_memory}m")
        jvm_args.insert(0, f"-Xms{min_memory}m")

        cmd = [java_path] + jvm_args + [main_class] + game_args
        return cmd, {"cwd": self.game_root}

    @staticmethod
    def _rule_allows(rules):
        if not rules:
            return True
        allow = False
        for rule in rules:
            ok = True
            if "os" in rule and rule["os"].get("name") != "windows":
                ok = False
            if "features" in rule:
                for feat, val in rule["features"].items():
                    if val is True and feat not in SUPPORTED_FEATURES:
                        ok = False
            if ok:
                allow = (rule.get("action") == "allow")
        return allow

    @staticmethod
    def _replace(token, mapping):
        for k, v in mapping.items():
            token = token.replace("${" + k + "}", str(v))
        return token

    @staticmethod
    def _clear_tokens(token):
        """清除任何残留的 ${...} 占位符（替换为空字符串）。"""
        return re.sub(r"\$\{[^}]+\}", "", token)

    @staticmethod
    def _extract_natives(jar_path, natives_dir):
        try:
            with zipfile.ZipFile(jar_path) as z:
                for name in z.namelist():
                    if name.endswith(".dll") or name.endswith(".so") or name.endswith(".dylib"):
                        z.extract(name, natives_dir)
        except Exception:
            pass


if __name__ == "__main__":
    core = MinecraftCore(os.path.join(os.path.dirname(__file__), "..", "..", "game_data"))
    latest, versions = core.get_remote_versions()
    print("latest:", latest)
    print("total:", len(versions))
    print(versions[0])
