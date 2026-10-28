# -*- coding: utf-8 -*-
"""Minecraft Java 版物品数据库（用于配方生成器的物品选择器）。

每个物品：
    id    : 形如 "minecraft:stone" 的完整命名空间 ID
    zh    : 中文名
    cat   : 分类（用于分组与配色）
    color : 图标底色（十六进制），缺省时按 id 哈希生成稳定色

提供：
    search_items(q, limit)  -> 按中文名 / id 模糊搜索
    ITEMS                   -> 全量列表
    CATEGORIES              -> 分类顺序
"""

# 分类 -> 默认配色（用于图标底色，保证同分类视觉统一）
_CAT_COLOR = {
    "方块": "#8A8F98",
    "装饰": "#B58B6A",
    "自然": "#6FAE5A",
    "矿石": "#9AA0A6",
    "工具": "#C9A24B",
    "武器": "#C26B5A",
    "装备": "#7C8BD0",
    "红石": "#C24B4B",
    "食物": "#D98A4B",
    "生物": "#C77FB0",
    "材料": "#B7BFC9",
    "药水": "#9B5BC7",
    "其他": "#8E96A6",
}

# (id, 中文名, 分类, 颜色[可选])
_RAW = [
    # ---- 方块 / 基础 ----
    ("minecraft:stone", "石头", "方块", "#8A8A8A"),
    ("minecraft:granite", "花岗岩", "方块", "#9E6B58"),
    ("minecraft:diorite", "闪长岩", "方块", "#D9D6CE"),
    ("minecraft:andesite", "安山岩", "方块", "#8C8C8C"),
    ("minecraft:deepslate", "深板岩", "方块", "#3F4147"),
    ("minecraft:cobblestone", "圆石", "方块", "#7E7E7E"),
    ("minecraft:sand", "沙子", "方块", "#E3D9A6"),
    ("minecraft:red_sand", "红沙", "方块", "#CB8B5A"),
    ("minecraft:gravel", "沙砾", "方块", "#8C8577"),
    ("minecraft:dirt", "泥土", "方块", "#8B6038"),
    ("minecraft:grass_block", "草方块", "自然", "#6AAE52"),
    ("minecraft:podzol", "灰化土", "自然", "#7A5230"),
    ("minecraft:mycelium", "菌丝体", "自然", "#7E5B8C"),
    ("minecraft:coarse_dirt", "粗泥土", "方块", "#7A5436"),
    ("minecraft:rooted_dirt", "缠根泥土", "方块", "#6E4B30"),
    ("minecraft:clay", "黏土块", "方块", "#9FB6C2"),
    ("minecraft:terracotta", "陶瓦", "装饰", "#A85B43"),
    ("minecraft:white_terracotta", "白色陶瓦", "装饰", "#D6CABA"),
    ("minecraft:red_terracotta", "红色陶瓦", "装饰", "#9C4730"),
    ("minecraft:black_terracotta", "黑色陶瓦", "装饰", "#3B312F"),
    ("minecraft:obsidian", "黑曜石", "方块", "#1B1330"),
    ("minecraft:bedrock", "基岩", "方块", "#2B2B30"),
    ("minecraft:glass", "玻璃", "装饰", "#BBD6E0"),
    ("minecraft:glass_pane", "玻璃板", "装饰", "#BBD6E0"),
    ("minecraft:bookshelf", "书架", "装饰", "#7A5430"),
    # ---- 木板 / 原木 ----
    ("minecraft:oak_log", "橡木原木", "自然", "#6E4B30"),
    ("minecraft:spruce_log", "云杉原木", "自然", "#5A4632"),
    ("minecraft:birch_log", "白桦原木", "自然", "#9A8B6A"),
    ("minecraft:jungle_log", "丛林木原木", "自然", "#6B5232"),
    ("minecraft:acacia_log", "金合欢原木", "自然", "#786049"),
    ("minecraft:dark_oak_log", "深色橡木原木", "自然", "#43332A"),
    ("minecraft:mangrove_log", "红树木原木", "自然", "#6B4A3A"),
    ("minecraft:crimson_stem", "绯红菌柄", "自然", "#9B3B5B"),
    ("minecraft:warped_stem", "诡异菌柄", "自然", "#3B7B7B"),
    ("minecraft:oak_planks", "橡木木板", "方块", "#B5894E"),
    ("minecraft:spruce_planks", "云杉木板", "方块", "#7E5C3A"),
    ("minecraft:birch_planks", "白桦木板", "方块", "#C2B089"),
    ("minecraft:jungle_planks", "丛林木板", "方块", "#9A744A"),
    ("minecraft:acacia_planks", "金合欢木板", "方块", "#A07448"),
    ("minecraft:dark_oak_planks", "深色橡木木板", "方块", "#4E3A2C"),
    ("minecraft:mangrove_planks", "红树木板", "方块", "#8A5A44"),
    ("minecraft:crimson_planks", "绯红木板", "方块", "#B24B68"),
    ("minecraft:warped_planks", "诡异木板", "方块", "#3E8C8C"),
    ("minecraft:oak_wood", "橡木", "自然", "#7A5230"),
    ("minecraft:oak_leaves", "橡树树叶", "自然", "#4E8B3A"),
    ("minecraft:spruce_leaves", "云杉树叶", "自然", "#2F5E2C"),
    ("minecraft:birch_leaves", "白桦树叶", "自然", "#6FAE5A"),
    ("minecraft:azalea_leaves", "杜鹃树叶", "自然", "#4E8B3A"),
    # ---- 石制品 ----
    ("minecraft:stone_bricks", "石砖", "方块", "#8A8F98"),
    ("minecraft:mossy_stone_bricks", "苔石砖", "方块", "#6E7A52"),
    ("minecraft:cracked_stone_bricks", "裂石砖", "方块", "#7E828A"),
    ("minecraft:polished_andesite", "磨制安山岩", "方块", "#9A9A9A"),
    ("minecraft:polished_diorite", "磨制闪长岩", "方块", "#E2DFD7"),
    ("minecraft:polished_granite", "磨制花岗岩", "方块", "#AE7A66"),
    ("minecraft:bricks", "红砖块", "方块", "#A85B43"),
    ("minecraft:nether_bricks", "下界砖块", "方块", "#43271F"),
    ("minecraft:red_nether_bricks", "红色下界砖", "方块", "#7A2A2A"),
    ("minecraft:quartz_block", "石英块", "方块", "#EDEDED"),
    ("minecraft:smooth_quartz", "平滑石英块", "方块", "#F2F2F2"),
    ("minecraft:purpur_block", "紫珀块", "方块", "#9B6BC7"),
    ("minecraft:end_stone", "末地石", "方块", "#D9D2B0"),
    ("minecraft:end_stone_bricks", "末地石砖", "方块", "#CFC8A4"),
    ("minecraft:prismarine", "海晶石", "方块", "#5A8FA0"),
    ("minecraft:dark_prismarine", "暗海晶石", "方块", "#2F5560"),
    ("minecraft:sea_lantern", "海晶灯", "装饰", "#9FD6C2"),
    ("minecraft:sandstone", "砂岩", "方块", "#D9C68A"),
    ("minecraft:red_sandstone", "红砂岩", "方块", "#C0794A"),
    ("minecraft:cut_sandstone", "切制砂岩", "方块", "#D2BC7E"),
    # ---- 台阶 / 楼梯 / 墙 / 栅栏 ----
    ("minecraft:stone_stairs", "石楼梯", "装饰", "#8A8F98"),
    ("minecraft:oak_stairs", "橡木楼梯", "装饰", "#B5894E"),
    ("minecraft:oak_slab", "橡木台阶", "装饰", "#B5894E"),
    ("minecraft:cobblestone_wall", "圆石墙", "装饰", "#7E7E7E"),
    ("minecraft:oak_fence", "橡木栅栏", "装饰", "#B5894E"),
    ("minecraft:oak_fence_gate", "橡木栅栏门", "装饰", "#B5894E"),
    ("minecraft:spruce_fence", "云杉栅栏", "装饰", "#7E5C3A"),
    ("minecraft:iron_bars", "铁栏杆", "装饰", "#B7BFC9"),
    # ---- 彩色混凝土 / 羊毛 ----
    ("minecraft:white_concrete", "白色混凝土", "装饰", "#DFDFDF"),
    ("minecraft:gray_concrete", "灰色混凝土", "装饰", "#8E96A6"),
    ("minecraft:black_concrete", "黑色混凝土", "装饰", "#2B2B30"),
    ("minecraft:red_concrete", "红色混凝土", "装饰", "#B5403A"),
    ("minecraft:blue_concrete", "蓝色混凝土", "装饰", "#3A6FB5"),
    ("minecraft:green_concrete", "绿色混凝土", "装饰", "#4E9A52"),
    ("minecraft:yellow_concrete", "黄色混凝土", "装饰", "#D6B53A"),
    ("minecraft:white_wool", "白色羊毛", "装饰", "#E6E6E6"),
    ("minecraft:red_wool", "红色羊毛", "装饰", "#C24B4B"),
    ("minecraft:blue_wool", "蓝色羊毛", "装饰", "#4B6FC2"),
    ("minecraft:black_wool", "黑色羊毛", "装饰", "#2B2B30"),
    ("minecraft:lime_wool", "黄绿羊毛", "装饰", "#7FC24B"),
    ("minecraft:purple_wool", "紫色羊毛", "装饰", "#8B5BC2"),
    ("minecraft:carpet", "地毯", "装饰", "#C24B4B"),
    # ---- 矿石 / 锭 ----
    ("minecraft:coal", "煤炭", "矿石", "#2B2B30"),
    ("minecraft:charcoal", "木炭", "矿石", "#3A3A3A"),
    ("minecraft:iron_ore", "铁矿石", "矿石", "#B58B6A"),
    ("minecraft:deepslate_iron_ore", "深板岩铁矿石", "矿石", "#5A5560"),
    ("minecraft:gold_ore", "金矿石", "矿石", "#C9A24B"),
    ("minecraft:copper_ore", "铜矿石", "矿石", "#B5703A"),
    ("minecraft:redstone_ore", "红石矿石", "矿石", "#9B3B3B"),
    ("minecraft:lapis_ore", "青金石矿石", "矿石", "#3A5BC2"),
    ("minecraft:diamond_ore", "钻石矿石", "矿石", "#5BC2B5"),
    ("minecraft:emerald_ore", "绿宝石矿石", "矿石", "#3AC26B"),
    ("minecraft:netherite_scrap", "下界合金碎片", "矿石", "#3A3530"),
    ("minecraft:coal_block", "煤炭块", "矿石", "#2B2B30"),
    ("minecraft:iron_block", "铁块", "矿石", "#D6D6D6"),
    ("minecraft:gold_block", "金块", "矿石", "#F2C94C"),
    ("minecraft:diamond_block", "钻石块", "矿石", "#5BE0D0"),
    ("minecraft:emerald_block", "绿宝石块", "矿石", "#3AE07B"),
    ("minecraft:lapis_block", "青金石块", "矿石", "#3A5BE0"),
    ("minecraft:netherite_block", "下界合金块", "矿石", "#3A3330"),
    ("minecraft:iron_ingot", "铁锭", "材料", "#D6D6D6"),
    ("minecraft:gold_ingot", "金锭", "材料", "#F2C94C"),
    ("minecraft:copper_ingot", "铜锭", "材料", "#C77B45"),
    ("minecraft:diamond", "钻石", "材料", "#5BE0D0"),
    ("minecraft:emerald", "绿宝石", "材料", "#3AE07B"),
    ("minecraft:lapis_lazuli", "青金石", "材料", "#3A5BE0"),
    ("minecraft:redstone", "红石", "红石", "#D63A3A"),
    ("minecraft:netherite_ingot", "下界合金锭", "材料", "#3A3330"),
    ("minecraft:raw_iron", "粗铁", "材料", "#B58B6A"),
    ("minecraft:raw_gold", "粗金", "材料", "#C9A24B"),
    ("minecraft:raw_copper", "粗铜", "材料", "#B5703A"),
    ("minecraft:quartz", "石英", "材料", "#EDEDED"),
    ("minecraft:amethyst_shard", "紫水晶碎片", "材料", "#B57BC2"),
    ("minecraft:nether_star", "下界之星", "材料", "#E6E6E6"),
    ("minecraft:prismarine_shard", "海晶碎片", "材料", "#5A8FA0"),
    ("minecraft:prismarine_crystals", "海晶砂砾", "材料", "#7FC2D0"),
    ("minecraft:blaze_rod", "烈焰棒", "材料", "#E0A23A"),
    ("minecraft:ender_pearl", "末影珍珠", "材料", "#C9B6D6"),
    ("minecraft:slime_ball", "黏液球", "生物", "#6FC26B"),
    ("minecraft:ghast_tear", "恶魂之泪", "材料", "#D6E6F2"),
    # ---- 工具 / 武器 / 装备 ----
    ("minecraft:wooden_pickaxe", "木镐", "工具", "#B5894E"),
    ("minecraft:stone_pickaxe", "石镐", "工具", "#8A8F98"),
    ("minecraft:iron_pickaxe", "铁镐", "工具", "#D6D6D6"),
    ("minecraft:golden_pickaxe", "金镐", "工具", "#F2C94C"),
    ("minecraft:diamond_pickaxe", "钻石镐", "工具", "#5BE0D0"),
    ("minecraft:netherite_pickaxe", "下界合金镐", "工具", "#3A3330"),
    ("minecraft:wooden_sword", "木剑", "武器", "#B5894E"),
    ("minecraft:stone_sword", "石剑", "武器", "#8A8F98"),
    ("minecraft:iron_sword", "铁剑", "武器", "#D6D6D6"),
    ("minecraft:diamond_sword", "钻石剑", "武器", "#5BE0D0"),
    ("minecraft:netherite_sword", "下界合金剑", "武器", "#3A3330"),
    ("minecraft:bow", "弓", "武器", "#9A744A"),
    ("minecraft:crossbow", "弩", "武器", "#8A8F98"),
    ("minecraft:trident", "三叉戟", "武器", "#7FC2D0"),
    ("minecraft:shield", "盾牌", "装备", "#B58B6A"),
    ("minecraft:iron_helmet", "铁头盔", "装备", "#D6D6D6"),
    ("minecraft:iron_chestplate", "铁胸甲", "装备", "#D6D6D6"),
    ("minecraft:iron_leggings", "铁护腿", "装备", "#D6D6D6"),
    ("minecraft:iron_boots", "铁靴子", "装备", "#D6D6D6"),
    ("minecraft:diamond_helmet", "钻石头盔", "装备", "#5BE0D0"),
    ("minecraft:diamond_chestplate", "钻石胸甲", "装备", "#5BE0D0"),
    ("minecraft:diamond_leggings", "钻石护腿", "装备", "#5BE0D0"),
    ("minecraft:diamond_boots", "钻石靴子", "装备", "#5BE0D0"),
    ("minecraft:netherite_helmet", "下界合金头盔", "装备", "#3A3330"),
    ("minecraft:netherite_chestplate", "下界合金胸甲", "装备", "#3A3330"),
    ("minecraft:netherite_leggings", "下界合金护腿", "装备", "#3A3330"),
    ("minecraft:netherite_boots", "下界合金靴子", "装备", "#3A3330"),
    ("minecraft:leather_helmet", "皮革头盔", "装备", "#B5703A"),
    ("minecraft:elytra", "鞘翅", "装备", "#9A8B6A"),
    ("minecraft:fishing_rod", "钓鱼竿", "工具", "#9A744A"),
    ("minecraft:shears", "剪刀", "工具", "#B7BFC9"),
    ("minecraft:flint_and_steel", "打火石", "工具", "#8A8F98"),
    ("minecraft:compass", "指南针", "工具", "#C9A24B"),
    ("minecraft:clock", "钟表", "工具", "#C9A24B"),
    # ---- 食物 ----
    ("minecraft:apple", "苹果", "食物", "#C24B4B"),
    ("minecraft:golden_apple", "金苹果", "食物", "#F2C94C"),
    ("minecraft:enchanted_golden_apple", "附魔金苹果", "食物", "#F2C94C"),
    ("minecraft:bread", "面包", "食物", "#C9A24B"),
    ("minecraft:cooked_beef", "熟牛排", "食物", "#8A4B3A"),
    ("minecraft:beef", "生牛肉", "食物", "#C26B5A"),
    ("minecraft:cooked_porkchop", "熟猪排", "食物", "#B5703A"),
    ("minecraft:porkchop", "生猪排", "食物", "#D08B7A"),
    ("minecraft:chicken", "鸡肉", "食物", "#E0D6C0"),
    ("minecraft:cooked_chicken", "熟鸡肉", "食物", "#B58B4B"),
    ("minecraft:cake", "蛋糕", "食物", "#E6C9C0"),
    ("minecraft:cookie", "曲奇", "食物", "#B5894E"),
    ("minecraft:carrot", "胡萝卜", "食物", "#E08A3A"),
    ("minecraft:potato", "土豆", "食物", "#C9A24B"),
    ("minecraft:baked_potato", "烤土豆", "食物", "#B5703A"),
    ("minecraft:pumpkin_pie", "南瓜派", "食物", "#E0A23A"),
    ("minecraft:melon_slice", "西瓜片", "食物", "#C24B4B"),
    ("minecraft:sweet_berries", "甜浆果", "食物", "#9B3B5B"),
    ("minecraft:honey_bottle", "蜂蜜瓶", "食物", "#F0C24B"),
    ("minecraft:milk_bucket", "牛奶桶", "食物", "#E6E6E6"),
    # ---- 红石 / 机械 ----
    ("minecraft:redstone_block", "红石块", "红石", "#D63A3A"),
    ("minecraft:piston", "活塞", "红石", "#B7BFC9"),
    ("minecraft:sticky_piston", "粘性活塞", "红石", "#6FC26B"),
    ("minecraft:dispenser", "发射器", "红石", "#8A8F98"),
    ("minecraft:dropper", "投掷器", "红石", "#8A8F98"),
    ("minecraft:hopper", "漏斗", "红石", "#B7BFC9"),
    ("minecraft:observer", "观察者", "红石", "#8A8F98"),
    ("minecraft:repeater", "红石中继器", "红石", "#C9A24B"),
    ("minecraft:comparator", "红石比较器", "红石", "#C9A24B"),
    ("minecraft:redstone_lamp", "红石灯", "红石", "#C9B6D6"),
    ("minecraft:lever", "拉杆", "红石", "#9A744A"),
    ("minecraft:button", "石按钮", "红石", "#8A8F98"),
    ("minecraft:pressure_plate", "石压力板", "红石", "#8A8F98"),
    ("minecraft:daylight_detector", " daylight 探测器".strip(), "红石", "#B7BFC9"),
    ("minecraft:target", "标靶", "红石", "#C24B4B"),
    ("minecraft:tnt", "TNT", "红石", "#C24B4B"),
    ("minecraft:jukebox", "唱片机", "红石", "#9A744A"),
    ("minecraft:note_block", "音符盒", "红石", "#B5894E"),
    ("minecraft:trapped_chest", "陷阱箱", "红石", "#B5703A"),
    # ---- 生物 / 掉落 / 特殊 ----
    ("minecraft:string", "线", "材料", "#E6E6E6"),
    ("minecraft:feather", "羽毛", "生物", "#E6E6E6"),
    ("minecraft:leather", "皮革", "材料", "#B5703A"),
    ("minecraft:rabbit_hide", "兔子皮", "生物", "#C0A88A"),
    ("minecraft:bone", "骨头", "生物", "#E6E6E6"),
    ("minecraft:gunpowder", "火药", "生物", "#8E96A6"),
    ("minecraft:ender_eye", "末影之眼", "材料", "#C9B64B"),
    ("minecraft:blaze_powder", "烈焰粉", "材料", "#E0A23A"),
    ("minecraft:spider_eye", "蜘蛛眼", "生物", "#6B3A5B"),
    ("minecraft:fermented_spider_eye", "发酵蛛眼", "材料", "#6B3A5B"),
    ("minecraft:rotten_flesh", "腐肉", "生物", "#9A6B5A"),
    ("minecraft:spawn_egg", "刷怪蛋", "生物", "#9B5BC7"),
    ("minecraft:egg", "鸡蛋", "生物", "#E6E6E6"),
    ("minecraft:experience_bottle", "经验瓶", "其他", "#7FC2D0"),
    ("minecraft:totem_of_undying", "不死图腾", "其他", "#E0A23A"),
    ("minecraft:dragon_head", "龙首", "其他", "#4E9A52"),
    ("minecraft:creeper_head", "苦力怕头", "其他", "#6FAE5A"),
    ("minecraft:skeleton_skull", "骷髅头", "其他", "#E6E6E6"),
    # ---- 染料 ----
    ("minecraft:white_dye", "白色染料", "材料", "#E6E6E6"),
    ("minecraft:red_dye", "红色染料", "材料", "#C24B4B"),
    ("minecraft:yellow_dye", "黄色染料", "材料", "#F2C94C"),
    ("minecraft:blue_dye", "蓝色染料", "材料", "#4B6FC2"),
    ("minecraft:green_dye", "绿色染料", "材料", "#4E9A52"),
    ("minecraft:black_dye", "黑色染料", "材料", "#2B2B30"),
    ("minecraft:purple_dye", "紫色染料", "材料", "#8B5BC2"),
    ("minecraft:lapis_dye", "青金石染料", "材料", "#3A5BE0"),
    ("minecraft:cocoa_beans", "可可豆", "材料", "#6B3A2A"),
    ("minecraft:ink_sac", "墨囊", "材料", "#2B2B30"),
    ("minecraft:bone_meal", "骨粉", "材料", "#E6E6E6"),
    # ---- 药水 ----
    ("minecraft:glass_bottle", "玻璃瓶", "药水", "#BBD6E0"),
    ("minecraft:potion", "药水", "药水", "#9B5BC7"),
    ("minecraft:splash_potion", "喷溅药水", "药水", "#9B5BC7"),
    ("minecraft:lingering_potion", "滞留药水", "药水", "#9B5BC7"),
    ("minecraft:brewing_stand", "酿造台", "红石", "#9A744A"),
    ("minecraft:cauldron", "炼药锅", "红石", "#B7BFC9"),
    ("minecraft:nether_wart", "地狱疣", "材料", "#9B3B5B"),
    ("minecraft:blaze_powder", "烈焰粉", "材料", "#E0A23A"),
    # ---- 其他常用 ----
    ("minecraft:paper", "纸", "材料", "#E6E6E6"),
    ("minecraft:book", "书", "材料", "#C9A24B"),
    ("minecraft:writable_book", "可写书", "材料", "#C9A24B"),
    ("minecraft:enchanted_book", "附魔书", "材料", "#7C8BD0"),
    ("minecraft:chest", "箱子", "装饰", "#B5703A"),
    ("minecraft:crafting_table", "工作台", "装饰", "#9A744A"),
    ("minecraft:furnace", "熔炉", "装饰", "#8A8F98"),
    ("minecraft:torch", "火把", "装饰", "#E0A23A"),
    ("minecraft:soul_torch", "灵魂火把", "装饰", "#3A8FE0"),
    ("minecraft:lantern", "灯笼", "装饰", "#E0A23A"),
    ("minecraft:soul_lantern", "灵魂灯笼", "装饰", "#3A8FE0"),
    ("minecraft:ender_chest", "末影箱", "装饰", "#3A5BE0"),
    ("minecraft:anvil", "铁砧", "装饰", "#B7BFC9"),
    ("minecraft:cartography_table", "制图台", "装饰", "#9A744A"),
    ("minecraft:fletching_table", "制箭台", "装饰", "#9A744A"),
    ("minecraft:smithing_table", "锻造台", "装饰", "#8A8F98"),
    ("minecraft:grindstone", "砂轮", "装饰", "#8A8F98"),
    ("minecraft:bell", "钟", "装饰", "#C9A24B"),
    ("minecraft:barrel", "木桶", "装饰", "#9A744A"),
    ("minecraft:loom", "织布机", "装饰", "#9A744A"),
    ("minecraft:composter", "堆肥桶", "装饰", "#9A744A"),
    ("minecraft:honeycomb", "蜂巢", "自然", "#E0A23A"),
    ("minecraft:honeycomb_block", "蜜脾块", "装饰", "#E0A23A"),
    ("minecraft:bee_nest", "蜂巢", "自然", "#9A744A"),
    ("minecraft:ice", "冰", "自然", "#BBD6E0"),
    ("minecraft:packed_ice", "浮冰", "自然", "#9FD6E0"),
    ("minecraft:blue_ice", "蓝冰", "自然", "#5A8FE0"),
    ("minecraft:snow_block", "雪块", "自然", "#E6E6E6"),
    ("minecraft:magma_block", "岩浆块", "自然", "#C24B3A"),
    ("minecraft:soul_sand", "灵魂沙", "自然", "#7A6B5A"),
    ("minecraft:soul_soil", "灵魂土", "自然", "#6B5A4A"),
    ("minecraft:netherrack", "下界岩", "方块", "#5A2A2A"),
    ("minecraft:basalt", "玄武岩", "方块", "#3F3F47"),
    ("minecraft:blackstone", "黑石", "方块", "#2B2B30"),
    ("minecraft:gilded_blackstone", "镶金黑石", "方块", "#5A4A2A"),
    ("minecraft:crying_obsidian", "哭泣的黑曜石", "方块", "#3A2A5A"),
    ("minecraft:ancient_debris", "远古残骸", "矿石", "#3A3530"),
    ("minecraft:slime_block", "黏液块", "自然", "#6FC26B"),
    ("minecraft:honey_block", "蜂蜜块", "其他", "#F0C24B"),
    ("minecraft:lodestone", "磁石", "方块", "#9A6B5A"),
    ("minecraft:respawn_anchor", "重生锚", "方块", "#5A2A4A"),
    ("minecraft:chain", "锁链", "装饰", "#B7BFC9"),
    ("minecraft:pointed_dripstone", "滴水石锥", "自然", "#C9B6A0"),
    ("minecraft:amethyst_block", "紫水晶块", "方块", "#B57BC2"),
    ("minecraft:budding_amethyst", "含芽紫水晶", "方块", "#B57BC2"),
    ("minecraft:calcite", "方解石", "方块", "#E6E0D6"),
    ("minecraft:tuff", "凝灰岩", "方块", "#6B6B6B"),
    ("minecraft:copper_block", "铜块", "方块", "#C77B45"),
    ("minecraft:raw_iron_block", "粗铁块", "方块", "#9A6B4A"),
    ("minecraft:raw_gold_block", "粗金块", "方块", "#B5902A"),
    ("minecraft:raw_copper_block", "粗铜块", "方块", "#9A5A2A"),
    ("minecraft:waxed_copper_block", "涂蜡铜块", "方块", "#C77B45"),
    ("minecraft:lightning_rod", "避雷针", "红石", "#C9A24B"),
    ("minecraft:spyglass", "望远镜", "工具", "#9A744A"),
]


def _color_for(item):
    c = item.get("color")
    if c:
        return c
    # 稳定哈希配色
    s = item["id"].encode("utf-8")
    h = 0
    for b in s:
        h = (h * 31 + b) & 0xFFFFFFFF
    hue = h % 360
    return f"hsl({hue}, 38%, 55%)"


ITEMS = []
for _id, _zh, _cat, *_rest in _RAW:
    _color = _rest[0] if _rest else None
    ITEMS.append({"id": _id, "zh": _zh, "cat": _cat, "color": _color_for({"id": _id, "color": _color})})

# 分类顺序（用于选择器分组）
_CATS = []
for it in ITEMS:
    if it["cat"] not in _CATS:
        _CATS.append(it["cat"])
CATEGORIES = _CATS


def search_items(query, limit=400):
    """按中文名 / id 模糊搜索物品。"""
    q = (query or "").strip().lower()
    if not q:
        return list(ITEMS[:limit])
    out = []
    # 前缀优先
    starts = []
    contains = []
    for it in ITEMS:
        zh = it["zh"].lower()
        iid = it["id"].lower()
        if zh.startswith(q) or iid.endswith(q):
            starts.append(it)
        elif q in zh or q in iid:
            contains.append(it)
    out = starts + contains
    return out[:limit]


def item_label(item):
    """图标上的简短标签（取中文名前 2 字，过长截断）。"""
    zh = item["zh"]
    if len(zh) <= 2:
        return zh
    return zh[:2]


def get_item(item_id):
    for it in ITEMS:
        if it["id"] == item_id:
            return it
    return None


if __name__ == "__main__":
    print("items:", len(ITEMS), "categories:", CATEGORIES)
    print("search '剑':", [i["zh"] for i in search_items("剑", 5)])
    print("search 'diamond':", [i["zh"] for i in search_items("diamond", 5)])
