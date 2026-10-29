# -*- coding: utf-8 -*-
"""从本地客户端 jar 解析每个物品的真实材质路径，并抽取到 textures/。

解析顺序（权威）：
  1) 直接纹理 textures/item/<name>.png / textures/block/<name>.png
  2) 物品模型 models/item/<name>.json 的 layer0
  3) 方块模型 models/block/<name>.json 的 particle（物品栏图标用 particle 纹理）
  4) 方块模型任意纹理
生成 textures/map.json 供运行时查询，并把命中的 PNG 复制到 textures/。
"""
import json
import os
import sys
import zipfile

# 项目根目录（本脚本位于 tools/ 下）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = PROJECT_ROOT
JAR = os.path.join(BASE, "game_data", "versions", "1.21.11", "1.21.11.jar")
TEXTURES_DIR = os.path.join(BASE, "textures")
MAP_PATH = os.path.join(TEXTURES_DIR, "map.json")


def tex_rel(value):
    """'minecraft:item/apple' -> 'item/apple.png'（相对 assets/minecraft/textures/）。"""
    if not value:
        return None
    v = value.split(":", 1)[-1]          # 去掉命名空间
    if not v.endswith(".png"):
        v += ".png"
    return v


def resolve_model(zf, jar_path, wanted):
    """沿 parent 链合并 textures，返回 wanted 键对应的相对纹理路径。"""
    seen = set()
    merged = {}
    cur = jar_path
    while cur and cur not in seen:
        seen.add(cur)
        try:
            data = json.loads(zf.read(cur))
        except Exception:
            break
        tex = data.get("textures") or {}
        for k, v in tex.items():
            merged.setdefault(k, v)
        cur = None
        parent = data.get("parent")
        if parent:
            p = parent.split(":", 1)[-1]      # block/xxx 或 item/xxx
            cur = "assets/minecraft/models/" + p + ".json"
    # 解析 #变量 引用（如 "#side" -> merged["side"]），最多迭代几次
    def resolve_val(v, depth=0):
        if not isinstance(v, str) or not v.startswith("#") or depth > 6:
            return v
        ref = v[1:]
        if ref in merged:
            return resolve_val(merged[ref], depth + 1)
        return v

    # 偏好顺序
    for key in wanted:
        if key in merged:
            r = tex_rel(resolve_val(merged[key]))
            if r:
                return r
    # 退而求其次：任意纹理值（已展开变量）
    for v in merged.values():
        rv = resolve_val(v)
        r = tex_rel(rv)
        if r:
            return r
    return None


# id -> (子目录, 文件名) 显式覆盖（少数 item 是多帧/3D 模型，取代表性一帧）
OVERRIDES = {
    "minecraft:grass_block": ("block", "grass_block_side.png"),
    "minecraft:compass": ("item", "compass_00.png"),
    "minecraft:clock": ("item", "clock_00.png"),
    "minecraft:spawn_egg": ("item", "pig_spawn_egg.png"),
    "minecraft:bee_nest": ("block", "bee_nest_side.png"),
    "minecraft:respawn_anchor": ("block", "respawn_anchor_side0.png"),
    "minecraft:chain": ("item", "exposed_copper_chain.png"),
}


def main():
    sys.path.insert(0, BASE)
    import mc_items as mi

    with zipfile.ZipFile(JAR) as zf:
        names = set(zf.namelist())

        def exists(p):
            return p in names

        def copy_rel(rel):
            if not rel:
                return False
            src = "assets/minecraft/textures/" + rel
            if src not in names:
                # 尝试在 jar 内直接按 basename 找（兼容子目录）
                base = rel.rsplit("/", 1)[-1]
                cand = [n for n in names if n.endswith("/" + base)
                        and n.startswith("assets/minecraft/textures/")]
                if not cand:
                    return False
                src = cand[0]
            dst = os.path.join(TEXTURES_DIR, *src.split("assets/minecraft/textures/")[1].split("/"))
            if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(zf.read(src))
            return True

        mapping = {}
        covered = 0
        for it in mi.ITEMS:
            iid = it["id"]
            name = iid.split(":")[-1]
            rel = None
            # 0) 显式覆盖
            if iid in OVERRIDES:
                sub, fn = OVERRIDES[iid]
                cand = f"assets/minecraft/textures/{sub}/{fn}"
                if cand in names:
                    rel = f"{sub}/{fn}"
            # 1) 直接
            if rel is None and f"assets/minecraft/textures/item/{name}.png" in names:
                rel = f"item/{name}.png"
            elif f"assets/minecraft/textures/block/{name}.png" in names:
                rel = f"block/{name}.png"
            # 2) 物品模型 layer0
            if rel is None:
                mp = f"assets/minecraft/models/item/{name}.json"
                if exists(mp):
                    rel = resolve_model(zf, mp, ["layer0", "layer1", "layer2", "particle"])
            # 3) 方块模型
            if rel is None:
                bp = f"assets/minecraft/models/block/{name}.json"
                if exists(bp):
                    rel = resolve_model(zf, bp, ["particle", "all", "texture", "0", "side",
                                                "up", "down", "north", "south", "east", "west",
                                                "top", "bottom", "front", "back"])
            if rel:
                if copy_rel(rel):
                    mapping[iid] = rel
                    covered += 1

    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    missing = [it["id"] for it in mi.ITEMS if it["id"] not in mapping]
    print(f"覆盖 {covered}/{len(mi.ITEMS)} 个，写入 {MAP_PATH}")
    print(f"未覆盖 {len(missing)} 个:", missing)


if __name__ == "__main__":
    main()
