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
