# -*- coding: utf-8 -*-
"""种子地图引擎（基于 cubiomes，真实还原 Minecraft 世界生成）。

提供：
  - 生物群系地图生成（主世界 / 下界 / 末地），渲染按种子精确还原
  - 结构定位（village / 沙漠神殿 / 丛林神庙 / 女巫小屋 / 雪屋 / 海底神殿 /
    海底废墟 / 沉船 / 掠夺者前哨 / 林地府邸 / 废墟传送门 / 下界要塞 /
    堡垒遗迹 / 末地城 / 要塞），坐标与游戏内一致
  - 出生点查找（cubiomes 真实出生点）
  - 读取 Minecraft 存档 level.dat 的种子（极简 NBT 解析）
  - 种子解析（与 Minecraft 一致：数字用 long，否则用 String.hashCode）

底层由 cubiomes（C 库）驱动，通过 cubiomespi 调用。生物群系为逐方块
精确还原；由于 cubiomespi 的 get_biome_at 为逐方块调用（每次约 0.25ms），
地图采用「分块网格采样 + 多线程」策略，在保持精度的同时把整图生成控制在
约 1~2 秒（并先出低分辨率预览）。
"""
import math
import os
import random
import threading
import time
import numpy as np
import cubiomespi as cp

MASK64 = (1 << 64) - 1

# 内部并行线程数（cubiomes 的 get_biome_at 为 C 调用，ctypes 会释放 GIL，
# 可在多个线程中并发；但嵌套在 Qt 的 QThread 工作线程里时过多线程易死锁，
# 故默认设为 1，通过环境变量 SEEDMAP_THREADS 可调）。
_MAX_THREADS = int(os.environ.get("SEEDMAP_THREADS", "1"))

# --------------------------------------------------------------------------
# 版本 / 维度映射
# --------------------------------------------------------------------------
def _mc_version(vstr):
    """UI 版本字符串 -> cubiomes MCVersion 整数。"""
    if not vstr:
        return cp.MCVersion.MC_1_20
    s = str(vstr)
    if s >= "1.21":
        return cp.MCVersion.MC_1_20          # cubiomespi 最高到 1.20
    if s.startswith("1.20"):
        return cp.MCVersion.MC_1_20
    if s.startswith("1.19"):
        return cp.MCVersion.MC_1_19_2
    if s.startswith("1.18"):
        return cp.MCVersion.MC_1_18_2
    if s.startswith("1.17"):
        return cp.MCVersion.MC_1_17_1
    if s.startswith("1.16"):
        return cp.MCVersion.MC_1_16_5
    return cp.MCVersion.MC_1_20


def _dim_int(dim):
    return {"overworld": cp.Dimension.DIM_OVERWORLD,
            "nether": cp.Dimension.DIM_NETHER,
            "end": cp.Dimension.DIM_END}.get(dim, cp.Dimension.DIM_OVERWORLD)


# --------------------------------------------------------------------------
# Generator 缓存（只读，可跨线程共享）
# --------------------------------------------------------------------------
_GEN_LOCK = threading.Lock()
_GEN_CACHE = {}


def _get_gen(seed, vstr, dim):
    seed_u = int(seed) & MASK64
    vint = _mc_version(vstr)
    dimint = _dim_int(dim)
    key = (seed_u, vint, dimint)
    with _GEN_LOCK:
        g = _GEN_CACHE.get(key)
        if g is None:
            g = cp.Generator(vint, seed_u, dimint)
            _GEN_CACHE[key] = g
        return g


# --------------------------------------------------------------------------
# 生物群系：id -> 名称 / 颜色
# --------------------------------------------------------------------------
_REV_BIOME = {int(getattr(cp.BiomeID, n)): n
              for n in dir(cp.BiomeID) if not n.startswith("_")}


def _style_color(name):
    n = name.lower()
    # 海洋 / 河流
    if "deep" in n and "ocean" in n:
        return (26, 42, 110)
    if "frozen_ocean" in n or n in ("frozenocean",):
        return (120, 160, 200)
    if "ocean" in n:
        return (54, 102, 196)
    if "frozen_river" in n:
        return (150, 200, 230)
    if "river" in n:
        return (70, 150, 220)
    # 沙滩 / 海岸
    if "beach" in n or "shore" in n:
        return (214, 205, 165)
    # 下界
    if "soul_sand" in n:
        return (90, 75, 60)
    if "crimson" in n:
        return (140, 50, 90)
    if "warped" in n:
        return (40, 110, 110)
    if "basalt" in n:
        return (60, 60, 70)
    if "nether" in n or n in ("hell",):
        return (120, 45, 45)
    if "lava" in n:
        return (230, 90, 20)
    # 末地
    if n in ("the_end", "sky", "end_stone"):
        return (190, 190, 185)
    if "end" in n:
        return (160, 170, 165)
    if "void" in n:
        return (20, 20, 28)
    # 雪原
    if "snowy" in n or "ice" in n or n in ("iceplains", "snowy_tundra",
                                           "snowy_plains"):
        return (225, 235, 240)
    if "ice_spikes" in n:
        return (200, 225, 240)
    # 沙漠
    if "desert" in n:
        return (222, 205, 150)
    # 热带草原
    if "savanna" in n:
        return (170, 160, 70)
    # 恶地
    if "badlands" in n or "mesa" in n or "badland" in n:
        return (190, 110, 60)
    # 山地
    if ("mountain" in n or "extremehills" in n or "windswept" in n
            or "gravelly" in n or "stony" in n):
        return (120, 120, 120)
    # 针叶林
    if "taiga" in n or "giant_tree" in n or "mega_taiga" in n or "spruce" in n:
        return (52, 95, 58)
    # 黑森林
    if "dark_forest" in n or "roofedforest" in n:
        return (30, 80, 45)
    # 森林
    if "forest" in n or "birch" in n:
        return (40, 120, 55)
    # 丛林
    if "jungle" in n:
        return (60, 110, 55)
    # 沼泽
    if "swamp" in n or "mangrove" in n:
        return (70, 100, 70)
    # 平原
    if "plains" in n or "meadow" in n or "sunflower" in n:
        return (128, 176, 80)
    # 洞穴
    if "lush" in n:
        return (40, 140, 90)
    if "dripstone" in n:
        return (140, 110, 90)
    if "deep_dark" in n:
        return (50, 50, 70)
    # 其他
    if "grove" in n:
        return (180, 200, 210)
    if "frozen_peaks" in n or "jagged_peaks" in n or "snowy_slopes" in n:
        return (200, 205, 210)
    if "cherry" in n:
        return (230, 160, 190)
    if "mushroom" in n:
        return (150, 120, 200)
    return (150, 150, 150)


def _style_name(name):
    n = name.lower()
    if "deep" in n and "ocean" in n:
        return "深海"
    if "frozen_ocean" in n or n == "frozenocean":
        return "冷冻海洋"
    if "ocean" in n:
        return "海洋"
    if "frozen_river" in n:
        return "冷冻河流"
    if "river" in n:
        return "河流"
    if "beach" in n or "shore" in n:
        return "沙滩"
    if "soul_sand" in n:
        return "灵魂沙峡谷"
    if "crimson" in n:
        return "绯红森林"
    if "warped" in n:
        return "诡异森林"
    if "basalt" in n:
        return "玄武岩三角洲"
    if "nether" in n or n == "hell":
        return "地狱荒野"
    if "lava" in n:
        return "熔岩"
    if n in ("the_end", "sky", "end_stone"):
        return "末地石岛"
    if "end" in n:
        return "末地"
    if "void" in n:
        return "虚空"
    if "ice_spikes" in n:
        return "冰刺"
    if "snowy" in n or "ice" in n or n in ("iceplains", "snowy_tundra",
                                           "snowy_plains"):
        return "雪原"
    if "desert" in n:
        return "沙漠"
    if "savanna" in n:
        return "热带草原"
    if "badlands" in n or "mesa" in n:
        return "恶地"
    if ("mountain" in n or "extremehills" in n or "windswept" in n
            or "gravelly" in n or "stony" in n):
        return "山地"
    if "taiga" in n or "giant_tree" in n or "mega_taiga" in n or "spruce" in n:
        return "针叶林"
    if "dark_forest" in n or "roofedforest" in n:
        return "黑森林"
    if "forest" in n or "birch" in n:
        return "森林"
    if "jungle" in n:
        return "丛林"
    if "swamp" in n or "mangrove" in n:
        return "沼泽"
    if "plains" in n or "meadow" in n or "sunflower" in n:
        return "平原"
    if "lush" in n:
        return "繁茂洞穴"
    if "dripstone" in n:
        return "滴水石洞穴"
    if "deep_dark" in n:
        return "深暗之域"
    if "grove" in n:
        return "树丛"
    if "frozen_peaks" in n or "jagged_peaks" in n or "snowy_slopes" in n:
        return "山峰"
    if "cherry" in n:
        return "樱花树林"
    if "mushroom" in n:
        return "蘑菇岛"
    return "未知群系"


_MAX_BID = 256
_BIOME_RGB = np.zeros((_MAX_BID, 3), dtype=np.uint8)
for _b in range(_MAX_BID):
    _BIOME_RGB[_b] = _style_color(_REV_BIOME.get(_b, "unknown"))


def _biome_color(bid):
    return tuple(int(v) for v in _BIOME_RGB[int(bid) % _MAX_BID])


def _biome_name_cn(bid):
    return _style_name(_REV_BIOME.get(int(bid) % _MAX_BID, "unknown"))


def biome_cn_name(bid):
    """纯 Python：生物群系 id -> 中文名（用于悬停，无需 cubiomes C 调用）。"""
    return _biome_name_cn(int(bid))


# --------------------------------------------------------------------------
# 生物群系地图生成（分块网格采样 + 多线程）
# --------------------------------------------------------------------------
def generate_biome_rgb(seed, cx, cz, W, H, bpp, dimension, version="1.20.1",
                     target=20000):
    """生成 (H, W, 3) uint8 的地图截图（cubiomes 精确还原）。

    seed      : int 种子（已解析为 long）
    cx, cz    : 中心方块坐标
    W, H      : 像素尺寸
    bpp       : 每像素代表多少方块（缩放）
    dimension : 'overworld' / 'nether' / 'end'
    version   : Minecraft 版本字符串
    """
    seed_u = int(seed) & MASK64
    vint = _mc_version(version)
    dimint = _dim_int(dimension)
    y = 64 if dimension == "nether" else 0

    block_w = W * bpp
    block_h = H * bpp
    bx0 = cx - block_w / 2.0
    bz0 = cz - block_h / 2.0

    # 采样步长：让总采样数维持在 ~target，避免逐像素调用过慢
    step = max(4, int(round(bpp * math.sqrt(W * H / max(1, target)))))
    step = max(1, step)

    g = _get_gen(seed_u, version, dimension)
    gx = max(2, int(block_w // step) + 1)
    gz = max(2, int(block_h // step) + 1)

    sx = np.arange(gx, dtype=np.float64) * step + bx0
    sz = np.arange(gz, dtype=np.float64) * step + bz0
    bids = np.empty((gz, gx), dtype=np.int32)

    def work(i0, i1):
        for i in range(i0, i1):
            x = int(round(sx[i]))
            for j in range(gz):
                bids[j, i] = cp.get_biome_at(g, x, y, int(round(sz[j])))

    nthreads = min(_MAX_THREADS, max(1, gx))
    if nthreads <= 1:
        work(0, gx)
    else:
        threads = []
        span = (gx + nthreads - 1) // nthreads
        for t in range(nthreads):
            a = t * span
            b = min(a + span, gx)
            th = threading.Thread(target=work, args=(a, b))
            th.start()
            threads.append(th)
        for th in threads:
            th.join()

    # 像素 -> 采样网格索引
    px = (np.arange(W, dtype=np.float64) - W / 2.0) * bpp + cx
    pz = (np.arange(H, dtype=np.float64) - H / 2.0) * bpp + cz
    ci = np.clip(np.round((px - bx0) / step).astype(np.int32), 0, gx - 1)
    cj = np.clip(np.round((pz - bz0) / step).astype(np.int32), 0, gz - 1)
    ci = np.broadcast_to(ci, (H, W))
    cj = np.broadcast_to(cj[:, None], (H, W))
    flat = bids[cj.ravel(), ci.ravel()]
    rgb = _BIOME_RGB[np.clip(flat, 0, _MAX_BID - 1)].reshape(H, W, 3).copy()
    # 与 rgb 同分辨率的生物群系 id 网格（用于 UI 端悬停查名，避免再调用 cubiomes）
    bid_grid = flat.reshape(H, W).astype(np.int32)
    return rgb, bid_grid


def downscale_rgb(rgb, pw, ph):
    """把 (H, W, 3) 地图按最近邻降采样到 (ph, pw, 3)，用于派生低分辨率预览。

    纯 numpy 操作，无任何 cubiomes C 调用，可安全在 Qt 工作线程中执行。
    """
    h, w = rgb.shape[:2]
    if pw >= w and ph >= h:
        return rgb.copy()
    xs = np.linspace(0, w - 1, pw).round().astype(np.int32)
    ys = np.linspace(0, h - 1, ph).round().astype(np.int32)
    return rgb[ys][:, xs]


def biome_name_at(seed, x, z, dimension, version="1.20.1"):
    """返回某方块坐标处的生物群系中文名（cubiomes 精确）。"""
    seed_u = int(seed) & MASK64
    y = 64 if dimension == "nether" else 0
    g = _get_gen(seed_u, version, dimension)
    bid = cp.get_biome_at(g, int(x), y, int(z))
    return _biome_name_cn(bid)


# --------------------------------------------------------------------------
# 结构定位（cubiomes 精确）
# --------------------------------------------------------------------------
STRUCT_TO_CUBIOMES = {
    "village":         {"struct": cp.Structure.Village, "dim": "overworld"},
    "desert_pyramid":  {"struct": cp.Structure.Desert_Pyramid, "dim": "overworld"},
    "jungle_temple":   {"struct": cp.Structure.Jungle_Temple, "dim": "overworld"},
    "swamp_hut":       {"struct": cp.Structure.Swamp_Hut, "dim": "overworld"},
    "igloo":           {"struct": cp.Structure.Igloo, "dim": "overworld"},
    "monument":        {"struct": cp.Structure.Monument, "dim": "overworld"},
    "ocean_ruin":      {"struct": cp.Structure.Ocean_Ruin, "dim": "overworld"},
    "shipwreck":       {"struct": cp.Structure.Shipwreck, "dim": "overworld"},
    "pillager":        {"struct": cp.Structure.Outpost, "dim": "overworld"},
    "mansion":         {"struct": cp.Structure.Mansion, "dim": "overworld"},
    "ruined_portal":   {"struct": cp.Structure.Ruined_Portal, "dim": "overworld"},
    "fortress":        {"struct": cp.Structure.Fortress, "dim": "nether"},
    "bastion":         {"struct": cp.Structure.Bastion, "dim": "nether"},
    "end_city":        {"struct": cp.Structure.End_City, "dim": "end"},
}

# UI 使用的结构字典：键与页面 STRUCT_GROUPS 对应；name/color 用于标记绘制
STRUCTURES = {
    "village":        {"name": "村庄", "color": (224, 196, 70)},
    "desert_pyramid": {"name": "沙漠神殿", "color": (214, 178, 92)},
    "jungle_temple":  {"name": "丛林神庙", "color": (92, 160, 80)},
    "swamp_hut":      {"name": "女巫小屋", "color": (120, 90, 150)},
    "igloo":          {"name": "雪屋", "color": (210, 220, 230)},
    "monument":       {"name": "海底神殿", "color": (60, 160, 200)},
    "ocean_ruin":     {"name": "海底废墟", "color": (110, 140, 170)},
    "shipwreck":      {"name": "沉船", "color": (150, 110, 70)},
    "pillager":       {"name": "掠夺者前哨", "color": (150, 60, 60)},
    "mansion":        {"name": "林地府邸", "color": (120, 80, 170)},
    "ruined_portal":  {"name": "废墟传送门", "color": (170, 60, 200)},
    "stronghold":     {"name": "要塞", "color": (230, 230, 120)},
    "fortress":       {"name": "下界要塞", "color": (200, 70, 40)},
    "bastion":        {"name": "堡垒遗迹", "color": (200, 120, 40)},
    "end_city":       {"name": "末地城", "color": (220, 220, 200)},
}


def structure_positions(seed, name, min_cx, max_cx, min_cz, max_cz,
                        version="1.20.1"):
    """返回某结构在 [min_cx..max_cx]×[min_cz..max_cz] 区块范围内的**方块**坐标列表。

    min_cx..max_cx / min_cz..max_cz 为区块（chunk）坐标。
    """
    info = STRUCT_TO_CUBIOMES.get(name)
    if info is None:
        return []
    seed_u = int(seed) & MASK64
    g = _get_gen(seed_u, version, info["dim"])
    # cubiomes 的 find_structure_in_range 使用 region 坐标（512 方块 = 32 区块）
    rminx = min_cx // 32 - 1
    rmaxx = max_cx // 32 + 1
    rminz = min_cz // 32 - 1
    rmaxz = max_cz // 32 + 1
    res = cp.find_structure_in_range(g, info["struct"], rminx, rminz, rmaxx, rmaxz)
    if not res:
        return []
    # 直接返回（已按 region 扩大一圈搜索）；画布会再用视野边界过滤，
    # 此处不过度裁剪，避免把刚好在视野边缘的结构丢掉。
    return [(int(bx), int(bz)) for (bx, bz) in res]


def stronghold_positions(seed, version="1.20.1", max_count=128):
    """返回全部要塞的方块坐标列表（与游戏内一致）。

    要塞位置对给定种子是确定不变的，故按 (种子, 版本) 缓存，避免每次重绘都
    重新计算 128 个要塞（cubiomes 计算较慢）。
    """
    seed_u = int(seed) & MASK64
    cache_key = (seed_u, _mc_version(version))
    cached = _STRONGHOLD_CACHE.get(cache_key)
    if cached is not None:
        return cached
    g = _get_gen(seed_u, version, "overworld")
    pts = cp.get_stronghold_pos(g, max_count)
    out = [(int(p[0]), int(p[1])) for p in (pts or [])]
    _STRONGHOLD_CACHE[cache_key] = out
    return out


_STRONGHOLD_CACHE = {}


def find_spawn(seed, version="1.20.1"):
    """返回世界出生点方块坐标 (x, z)（cubiomes 真实出生点）。"""
    seed_u = int(seed) & MASK64
    g = _get_gen(seed_u, version, "overworld")
    x, z = cp.get_spawn_pos(g)
    return (int(x), int(z))


def search_biome_near(seed, biome_cn, cx, cz, radius, step, dimension,
                      version="1.20.1"):
    """在 [cx-radius, cx+radius]×[cz-radius, cz+radius] 内逐点采样，返回命中
    目标群系中文名 == biome_cn 的方块坐标列表。

    注意：内含 cubiomes C 调用，只能在子进程内执行（主线程会死锁）。
    """
    seed_u = int(seed) & MASK64
    y = 64 if dimension == "nether" else 0
    g = _get_gen(seed_u, version, dimension)
    found = []
    bx = cx - radius
    while bx <= cx + radius:
        bz = cz - radius
        while bz <= cz + radius:
            bid = cp.get_biome_at(g, int(bx), y, int(bz))
            if _biome_name_cn(bid) == biome_cn:
                found.append((int(bx), int(bz)))
            bz += step
        bx += step
    return found


# --------------------------------------------------------------------------
# 种子搜索（MinecraftSearch）：扫描大量种子，返回满足条件的结果
# --------------------------------------------------------------------------
def _new_gen(seed_u, version, dim):
    """新建一个 Generator（不进缓存，便于大规模扫描时控制内存）。"""
    return cp.Generator(_mc_version(version), int(seed_u) & MASK64, _dim_int(dim))


def _find_seeds_structure(name, version, radius, max_tries, max_results, rng):
    info = STRUCT_TO_CUBIOMES.get(name)
    if info is None:
        return []
    dim, struct = info["dim"], info["struct"]
    out = []
    tries = 0
    while tries < max_tries and len(out) < max_results:
        tries += 1
        seed_u = rng.randrange(0, 1 << 63)
        g = _new_gen(seed_u, version, dim)
        if dim == "overworld":
            sx, sz = cp.get_spawn_pos(g)
        else:
            sx, sz = 0, 0
        rc = int(math.ceil(radius / 512.0)) + 1
        rx, rz = int(sx) // 512, int(sz) // 512
        res = cp.find_structure_in_range(g, struct, rx - rc, rz - rc,
                                         rx + rc, rz + rc)
        best = None
        for (bx, bz) in res:
            d = math.hypot(bx - sx, bz - sz)
            if d <= radius and (best is None or d < best[0]):
                best = (d, bx, bz)
        if best:
            out.append((seed_u,
                        f"{STRUCTURES[name]['name']} @ ({best[1]}, {best[2]})，"
                        f"距出生点 {int(best[0])} 方块"))
    return out


def _find_seeds_biome(cn, version, radius, max_tries, max_results, rng):
    out = []
    tries = 0
    r = int(radius)
    # 仅采样出生点 + 半径为 r 的 8 个方位点（共 9 点），保证扫描够快
    pts = [(0, 0), (r, 0), (-r, 0), (0, r), (0, -r),
           (r, r), (-r, r), (r, -r), (-r, -r)]
    while tries < max_tries and len(out) < max_results:
        tries += 1
        seed_u = rng.randrange(0, 1 << 63)
        g = _new_gen(seed_u, version, "overworld")
        sx, sz = cp.get_spawn_pos(g)
        hit = None
        for (dx, dz) in pts:
            bid = cp.get_biome_at(g, int(sx + dx), 0, int(sz + dz))
            if _biome_name_cn(bid) == cn:
                hit = (int(sx + dx), int(sz + dz))
                break
        if hit:
            out.append((seed_u, f"出生点附近有「{cn}」 @ ({hit[0]}, {hit[1]})"))
    return out


def find_seeds(criterion, version="1.20.1", max_tries=1500, seed_start=None,
               max_results=60):
    """扫描种子，返回满足条件的结果列表 [(seed_int, info_str), ...]。

    criterion:
      - {"kind": "structure", "name": <STRUCT_TO_CUBIOMES 键>, "radius": <方块>}
      - {"kind": "biome", "biome": <中文群系名>, "radius": <方块>}
    随机种子由 seed_start 决定（默认随机）。整个扫描在 cubiomes 子进程内执行。
    """
    rng = random.Random(seed_start if seed_start is not None
                        else int(time.time() * 1000) ^ os.getpid())
    if criterion.get("kind") == "structure":
        return _find_seeds_structure(criterion["name"], version,
                                     int(criterion.get("radius", 1500)),
                                     max_tries, max_results, rng)
    if criterion.get("kind") == "biome":
        return _find_seeds_biome(criterion["biome"], version,
                                 int(criterion.get("radius", 800)),
                                 max_tries, max_results, rng)
    return []


# --------------------------------------------------------------------------
# 种子解析（与 Minecraft 一致）
# --------------------------------------------------------------------------
def parse_seed(text):
    text = (text or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return java_string_hash(text)


def java_string_hash(s):
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h


# --------------------------------------------------------------------------
# 读取 level.dat 的种子（极简 NBT 解析）
# --------------------------------------------------------------------------
def _read_nbt(data, pos=0):
    tag = data[pos]
    pos += 1
    if tag == 0:
        return None, pos
    if tag in (9, 10):
        name_len = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2 + name_len
    return _read_nbt_value(data, tag, pos)


def _read_nbt_value(data, tag, pos):
    if tag == 1:
        return int(data[pos]), pos + 1
    if tag == 2:
        return int.from_bytes(data[pos:pos + 2], "little", signed=True), pos + 2
    if tag == 3:
        return int.from_bytes(data[pos:pos + 4], "little", signed=True), pos + 4
    if tag == 4:
        return int.from_bytes(data[pos:pos + 8], "little", signed=True), pos + 8
    if tag == 5:
        return None, pos + 4
    if tag == 6:
        return None, pos + 8
    if tag == 7:
        n = int.from_bytes(data[pos:pos + 4], "little"); return None, pos + 4 + n
    if tag == 8:
        ln = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
        s = data[pos:pos + ln].decode("utf-8", "replace"); return s, pos + ln
    if tag == 9:
        elem_tag = data[pos]; pos += 1
        n = int.from_bytes(data[pos:pos + 4], "little"); pos += 4
        items = []
        for _ in range(n):
            if elem_tag == 10:
                d, pos = _read_compound(data, pos)
                items.append(d)
            elif elem_tag == 0:
                items.append(None)
            else:
                v, pos = _read_nbt_value(data, elem_tag, pos)
                items.append(v)
        return items, pos
    if tag == 10:
        return _read_compound(data, pos)
    if tag == 11:
        n = int.from_bytes(data[pos:pos + 4], "little"); pos += 4
        return None, pos + 4 + n * 4
    if tag == 12:
        n = int.from_bytes(data[pos:pos + 4], "little"); pos += 4
        return None, pos + 4 + n * 8
    return None, pos


def _read_compound(data, pos):
    d = {}
    while True:
        tag = data[pos]; pos += 1
        if tag == 0:
            break
        name_len = int.from_bytes(data[pos:pos + 2], "little"); pos += 2
        name = data[pos:pos + name_len].decode("utf-8", "replace"); pos += name_len
        if tag == 10:
            v, pos = _read_compound(data, pos)
        elif tag == 9:
            v, pos = _read_nbt_value(data, 9, pos)
        else:
            v, pos = _read_nbt_value(data, tag, pos)
        d[name] = v
    return d, pos


def read_level_seed(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    try:
        root, _ = _read_compound(data, 0)
        data_node = root.get("Data")
        if isinstance(data_node, dict) and "Seed" in data_node:
            return int(data_node["Seed"])
        if "Seed" in root:
            return int(root["Seed"])
    except Exception:
        pass
    return None


if __name__ == "__main__":
    s = parse_seed("54545")
    print("seed 54545 ->", s)
    g = _get_gen(s, "1.19.2", "overworld")
    print("spawn:", find_spawn(s, "1.19.2"))
    print("village @0,0 region:", structure_positions(
        s, "village", -2, 2, -2, 2, "1.19.2")[:3])
    print("strongholds count:", len(stronghold_positions(s, "1.19.2")))
    import time
    t = time.time()
    img = generate_biome_rgb(s, 0, 0, 200, 150, 4, "overworld", "1.19.2")
    print("img shape:", img.shape, "gen time %.2fs" % (time.time() - t))
    print("biome @0,0:", biome_name_at(s, 0, 0, "overworld", "1.19.2"))
