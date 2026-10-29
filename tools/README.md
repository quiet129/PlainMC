# 开发工具脚本

这些是**开发期辅助脚本**，不是启动器运行时的一部分。
`PlainMC.py` 已把其中的核心逻辑整合进主文件，日常开发改主文件即可。

| 脚本 | 用途 |
|---|---|
| `_build_texture_map.py` | 从本地客户端 jar 解析每个物品的真实材质路径，抽取贴图并生成 `textures/map.json` |
| `mc_textures.py` | 材质查询辅助 |
| `mc_items.py` | 物品清单数据 |
| `recipe_editor.py` | 配方编辑器的独立版本（已整合进 `PlainMC.py` 的「配方」页） |
| `seedmap_engine.py` | 种子地图引擎的独立版本（已整合进 `PlainMC.py` 的「种子地图」页） |
| `test_memory_optimize.py` | **内存优化实测**：验证各优化级别的真实释放量与误杀防护 |

## 内存优化实测

```bash
python tools/test_memory_optimize.py
```

会依次执行 L1 / L2 / L3 三级优化并打印实测数据，同时校验：

- 系统关键进程、浏览器/编辑器等交互程序**不会**出现在候选列表里
- 自己的**整条父进程链**已被排除（这一条是 v1.2.0 修复的重点）

> ⚠️ 脚本会**真实执行** L3（结束后台进程）。测试前请保存重要工作。

## 使用前提

这些脚本依赖**本地已下载的客户端 jar**（Mojang 版权资源，不随仓库分发）：

```
game_data/versions/1.21.11/1.21.11.jar
```

先用启动器的「下载」页把对应版本下下来，或手动放到上述路径，再运行：

```bash
python tools/_build_texture_map.py
```

生成的贴图与 `map.json` 位于项目根的 `textures/`，供「种子地图」页运行时查询材质。
