"""意图识别器（流水线第 1 段）。

从用户自然语言中抽取 IntentProfile。示例:

    输入: "帮我推荐一个500卡以内，30元以内的麦当劳套餐"
    输出: {"goal": "减脂", "maxCalories": 500, "budget": 30}

实现说明:
- 当前为规则引擎版（正则 + 关键词），零外部依赖、结果 100% 可复现，
  是 Demo 现场的保底实现；
- 预留 LLM 适配器接口（LLMIntentRecognizer），接入大模型后可平滑替换，
  输出仍必须通过 IntentProfile 的 Pydantic 校验（防幻觉字段）。
"""

from __future__ import annotations

import os
import re

from ..models.schemas import Goal, IntentProfile

# ---------------------------------------------------------------------------
# 城市槽位（定位链路的关键输入）
# ---------------------------------------------------------------------------
# 官方 query-nearby-stores 不接受经纬度，"位置"由服务端按账号判定，
# 客户端只能提供 city + keyword；所以城市是定位链路唯一可控的锚点。
# 未识别到城市时用默认城市兜底（可用环境变量 MCSMART_DEFAULT_CITY 覆盖），
# 并在输出层显式标注"默认城市"，不做静默推荐。
DEFAULT_CITY: str = os.getenv("MCSMART_DEFAULT_CITY", "北京").strip() or "北京"

# 常见城市词表（覆盖一线/新一线/主要省会与地级市，命中即视为用户指定城市）
CITY_NAMES: tuple[str, ...] = (
    "北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "西安",
    "重庆", "天津", "苏州", "长沙", "青岛", "郑州", "济南", "合肥", "福州",
    "厦门", "昆明", "大连", "沈阳", "哈尔滨", "长春", "石家庄", "太原",
    "南昌", "贵阳", "南宁", "兰州", "银川", "西宁", "乌鲁木齐", "呼和浩特",
    "海口", "三亚", "无锡", "宁波", "温州", "佛山", "东莞", "珠海", "中山",
    "常州", "南通", "徐州", "烟台", "潍坊", "洛阳", "保定", "唐山", "泉州",
    "嘉兴", "绍兴", "惠州", "金华", "台州", "中山", "汕头", "湛江", "桂林",
)
CITY_PATTERN = re.compile("(" + "|".join(CITY_NAMES) + ")")   # 捕获组: group(1) 为命中的城市

# ---------------------------------------------------------------------------
# 规则表: goal 关键词 → 分类
# ---------------------------------------------------------------------------

GOAL_KEYWORDS: list[tuple[Goal, tuple[str, ...]]] = [
    # 省钱模式优先匹配: "怎么最划算/省钱/薅羊毛"是显式目标词，先于健康目标判断
    (Goal.BEST_VALUE, ("最划算", "划算", "省钱", "最省", "薅羊毛", "怎么买最", "最便宜", "便宜")),
    # 健康模式五目标（特异性词优先: 低糖/低脂 先于 减脂，避免"低脂"被泛化目标吞掉）
    (Goal.LOW_SUGAR, ("低糖", "无糖", "控糖", "少糖", "减糖", "糖分低", "血糖")),
    (Goal.LOW_FAT, ("低脂", "少油", "低油", "清淡", "油腻")),
    (Goal.FAT_LOSS, ("减肥", "减脂", "瘦身", "轻食", "热量低", "低卡")),
    (Goal.MUSCLE_GAIN, ("增肌", "健身", "高蛋白", "蛋白质", "练后")),
    (Goal.KIDS, ("儿童", "小孩", "孩子", "小朋友", "宝宝", "遛娃")),
]

# 门店查询模式的强触发词（"附近哪里可以买"类输入）
# 仅当无健康/省钱目标时生效——"减脂期附近有店吗"仍走推荐链路，门店在其中
STORE_QUERY_PATTERNS: tuple[str, ...] = (
    "附近哪里", "附近哪家", "附近门店", "附近的", "最近的麦当劳", "哪里有麦当劳",
    "哪里可以买", "哪里能买", "哪家店", "有什么店", "附近有店", "门店推荐",
)


def recognize_intent(text: str) -> IntentProfile:
    """规则引擎版意图识别。

    抽取槽位:
    - goal: 关键词匹配，默认"普通"
    - max_calories: "500卡/卡路里/kcal/大卡以内" → 500
    - budget: "30元/块/块钱以内" → 30
    - dining_mode: 外送/得来速关键词 → delivery / drive_thru
    """
    goal = Goal.NORMAL
    for candidate, keywords in GOAL_KEYWORDS:
        if any(kw in text for kw in keywords):
            goal = candidate
            break
    # 热量: "500卡以内" / "500大卡" / "热量不超过500"
    max_calories = None
    m = re.search(r"(\d{2,4})\s*(?:卡|大卡|kcal|千卡|卡路里)", text, re.IGNORECASE)
    if m:
        max_calories = int(m.group(1))
    else:
        m = re.search(r"热量[^0-9]{0,6}(\d{2,4})", text)
        if m:
            max_calories = int(m.group(1))

    # 目标推断兜底: 显式给出低卡约束(≤600kcal)且无其他目标关键词 → 减脂
    if goal == Goal.NORMAL and max_calories and max_calories <= 600:
        goal = Goal.FAT_LOSS

    # 预算: "30元" / "30块" / "预算30"
    budget = None
    m = re.search(r"(?:预算|不超过|以内|控制)?\s*(\d+(?:\.\d+)?)\s*(?:元|块钱|块)", text)
    if m:
        budget = float(m.group(1))
    else:
        m = re.search(r"(?:预算|花费)[^0-9]{0,4}(\d+(?:\.\d+)?)", text)
        if m:
            budget = float(m.group(1))

    # 用餐方式
    if any(kw in text for kw in ("外送", "外卖", "配送", "送到家", "麦乐送")):
        dining_mode = "delivery"
    elif any(kw in text for kw in ("得来速", "车道", "开车")):
        dining_mode = "drive_thru"
    else:
        dining_mode = "dine_in"

    # 城市（门店搜索 searchType=2 必需）
    # 说明: 官方 query-nearby-stores 不接受经纬度入参，"位置"由服务端按账号判定，
    # 客户端只能靠 city + keyword 收窄；因此城市槽位是定位链路的关键输入。
    # 用户未提城市时用默认城市兜底，但必须标记 city_inferred=True 以便输出层显式告知，
    # 避免静默按默认城市推荐（非该城市用户会得到完全错误的结果）。
    city = DEFAULT_CITY
    m = CITY_PATTERN.search(text)
    if m:
        city = m.group(1)

    # 缺失槽位（触发追问/默认值策略的位置）
    missing: list[str] = []
    if not m:
        missing.append("location")          # 已用默认城市兜底，输出层需告知可覆盖

    # 门店查询模式: 命中门店强触发词，且没有健康/省钱等点餐目标
    # （有目标时"附近有店吗"只是推荐链路的附带诉求，不单独分流）
    store_query = (
        goal == Goal.NORMAL
        and any(p in text for p in STORE_QUERY_PATTERNS)
    )

    return IntentProfile(
        goal=goal,
        max_calories=max_calories,
        budget=budget,
        dining_mode=dining_mode,
        city=city,
        city_inferred=not bool(m),
        store_query=store_query,
        missing_slots=missing,
    )
