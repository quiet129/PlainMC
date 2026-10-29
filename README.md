# PlainMC

> 仿 [PCL2](https://github.com/HMCL-dev/HMCL) 美术风格的《我的世界》Python 启动器。
> 单文件、零包结构依赖，`python PlainMC.py` 即可运行。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![PyQt6](https://img.shields.io/badge/GUI-PyQt6-41CD52.svg)](https://pypi.org/project/PyQt6/)

---

## ✨ 功能一览

| 模块 | 说明 |
|---|---|
| 🚀 **启动** | 离线账号 / 正版微软账号登录，自动扫描并推荐合适的 Java 版本 |
| ⬇️ **下载** | 从官方源或国内镜像（BMCLAPI）下载各版本客户端、依赖库、原生库、资源；镜像缺文件时自动回退官方源并校验 SHA1 |
| 🧩 **MOD** | 浏览 / 搜索 / 下载 Modrinth 上的 MOD，可安装到指定版本 |
| 📦 **整合包** | Modrinth 整合包浏览与一键安装 |
| 📁 **数据包** · 🎨 **资源包** · 💡 **光影包** | Modrinth 对应类型资源浏览与下载 |
| 🌐 **联机** | 局域网联机相关功能 |
| 🎭 **个性化** | 主题、背景、透明度、标题栏自定义 |
| 🌍 **种子地图** | 种子地图生成（网页版 / 离线 cubiomes 双模式） |
| 📑 **配方** | 配方编辑器 |
| 🖌 **皮肤** | **像素级皮肤编辑器**，画完一键同步进游戏 |
| 🧹 **内存优化** | **一键释放内存**：清理残留 Java 进程 + 回收缓存 + 归还工作集 |
| 👤 **账号** | 离线 / 微软账号管理 |
| ⚙️ **设置** | 内存分配、Java 路径、镜像源、主题等 |

### 支持的加载器

`vanilla` · `fabric` · `forge` · `neoforge` · `quilt` · `optifine`

---

## 🚀 快速开始

### 1. 环境要求

- **Python 3.11 或更高**（`cubiomespi` 需要 3.11+）
- Windows / macOS / Linux

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> **国内网络提示**：`cubiomespi` 在国内镜像常常缺失，装不上就用官方源：
> ```bash
> pip install cubiomespi numpy -i https://pypi.org/simple
> ```

### 3. 运行

```bash
python PlainMC.py
```

Windows 用户也可以直接双击 `run.bat`（它会自动挑选已装好依赖的解释器）。

---

## 🎨 皮肤编辑器

新增的像素级皮肤编辑功能，画完可**一键同步到游戏**：

- 画笔 / 橡皮 / 油漆桶 / 取色器，笔刷 1–3 像素
- 40 色常用色板 + 自定义颜色
- 4×–16× 缩放、像素网格、部位分区线与中文标签
- 撤销 / 重做、导入 PNG、载入示例皮肤、清空
- 实时**正面 + 背面**预览，一眼看出画得对不对

### 同步原理

Minecraft 的默认皮肤位于客户端 jar 内：

```
assets/minecraft/textures/entity/player/{wide,slim}/{steve,alex}.png
```

由于**资源包优先级高于 jar**，启动器会生成一个 `plainmc_skin.zip` 覆盖默认皮肤，并写入 `options.txt` 的 `resourcePacks`，因此**离线账号也能直接换上自己的皮肤**。

同步时会：

1. 写入资源包（同时包含 `wide` 与 `slim` 两套贴图）
2. 修改 `options.txt` 启用该资源包（保留你的其它设置，重复点击不会重复添加）
3. 在 `game_root/skins/账号名.png` 留一份备份
4. 若是**正版账号**，额外上传到 Mojang —— 这样**联机时其它玩家也能看到**

> 离线账号按 UUID 约一半会被分配为 slim（Alex 细臂）模型，因此启动器内置了 wide → slim 的自动转换。
> 修改皮肤后需**重启游戏**才能生效（资源包在启动时加载）。

---

## 🧹 内存优化

**把内存真正还给系统** —— 任务管理器里的数字会真实下降，不是在内部记账。

### 一键释放内存

启动页左下角的「🧹 释放内存」按钮，执行三件事：

| 动作 | 说明 | 实测效果 |
|---|---|---|
| **清理残留 Java 进程** | 游戏退出后残留的 `java.exe` / `javaw.exe` 常驻 1~2GB 物理内存 | 单个残留进程释放 **343 MB** |
| **回收启动器缓存** | Modrinth 图标字节缓存、配方贴图 QPixmap、Qt 全局 PixmapCache | 释放 **110 MB** |
| **归还工作集** | `EmptyWorkingSet` 把进程驻留页还给操作系统 | **170.3 MB → 0.9 MB** |

点击后弹窗报告清理了几个进程、释放多少 MB、当前占用多少。

### 游戏退出后自动优化

默认开启（设置页可关）。游戏退出 800ms 后自动执行一次，状态栏提示
「游戏已退出 · 清理 1 个残留进程，释放 343 MB」。

### 设置页

新增独立卡片：实时占用显示（工作集 / 私有提交）、「立即释放内存」、「刷新占用」，
以及「游戏退出后自动执行内存优化」开关。

### 绝不误杀

清理残留进程最怕误杀用户自己开的 Java 程序。三层防护：

1. **命令行特征匹配** —— 只清理含 `.minecraft` / `versions` / `net.minecraft` 的进程
2. **白名单保护** —— 正在运行的游戏 PID 显式排除
3. **宁漏不误** —— 拿不到命令行一律视为非游戏进程

> Windows 11 起 **wmic 已被系统移除**，故命令行读取改为**优先走 PowerShell CIM**
> （`Win32_Process`），wmic 仅作老系统兜底；`tasklist` 输出是 GBK 代码页，
> 代码里统一做了多编码容错解码。

---

## ⚡ 国内网络优化

针对 Modrinth 在国内的访问状况，启动器内置了多项优化：

| 项目 | 优化前 | 优化后 |
|---|---|---|
| 30 张图标加载 | ~33.5 秒 | **~3.2 秒**（16 路并发） |
| 下载 1.8MB 文件 | ~62–137 秒 | **~10.8 秒**（HTTP Range 分块并行） |
| 重复搜索同一条件 | ~16 秒 | **~0.02 秒**（磁盘缓存） |

- **磁盘缓存**：搜索 10 分钟 / 项目 1 天 / 版本 1 小时 / 图标 7 天，缓存位于 `cache/modrinth/`
- **后台预热**：启动后自动预取各页面首屏数据
- **超时 12 秒 + 自动重试 2 次**，失败快速反馈而不是干等 60 秒

---

## 📦 打包成 EXE

项目提供两个 PyInstaller 配置文件：

```bash
# 单文件版（便于分发，但首次启动需解压，耗时较长）
pyinstaller PlainMC.spec

# 文件夹版（不压缩不解压，启动瞬间，推荐）
pyinstaller PlainMC_onedir.spec
```

产物位置：

- 单文件版：`dist/我的世界启动器.exe`
- 文件夹版：`dist/我的世界启动器/我的世界启动器.exe`（**整个文件夹一起拷贝**）

打包配置已做裁剪：剔除 Qt 调试资源与 DevTools 资源（节省约 234 个文件）。

---

## 📁 项目结构

```
MC_Launcher/
├── PlainMC.py            # ★ 主程序（单文件版，功能完整）
├── PlainMC.spec          # PyInstaller 打包配置（单文件）
├── PlainMC_onedir.spec   # PyInstaller 打包配置（文件夹）
├── launch.py             # 模块化入口（历史版本，仍可运行）
├── mc_launcher/          # 模块化实现（core / ui 分层）
├── requirements.txt
├── run.bat               # Windows 便捷启动
├── tools/                # 开发期辅助脚本（贴图映射 / 配方 / 种子地图引擎独立版）
├── RELEASE_NOTES*.md     # 各版本发布说明
├── RELEASE_BODY*.md      # Release 页面粘贴用正文
└── ...
```

> 日常开发以 `PlainMC.py` 为准 —— 它把 `mc_launcher/` 里的模块全部整合进了单个文件，方便分发和二次修改。

---

## ⚠️ 版权与免责声明

本项目**不包含**任何 Mojang / Microsoft 的资源文件。以下内容均被 `.gitignore` 排除，需由用户自行获取：

- Minecraft 客户端 jar、依赖库、资源文件（`game_data/`）
- 材质贴图（`textures/`，由 `tools/_build_texture_map.py` 从本地 jar 抽取）
- Minecraft 的商标、材质、音频等

Minecraft 是 Mojang Studios 的商标，本项目与 Mojang Studios / Microsoft 无任何关联。

启动器仅作为**第三方工具**使用，游戏本体请前往 [minecraft.net](https://www.minecraft.net/) 购买。

---

## 📄 开源协议

[MIT](LICENSE) © PlainMC Contributors

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。提交前请确保：

- 不要提交 `game_data/`、`textures/`、`dist/`、`venv/` 等目录（`.gitignore` 已处理）
- 不要提交 `.plainmc_config.json`（**含你的账号与本地路径**）
- 改动后跑一次启动器确认无异常
