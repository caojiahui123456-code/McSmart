"""智能推荐引擎（能力⑥）。

三步走:
1. 候选筛选 —— 在售餐品 × 营养表交叉，按意图约束（热量/预算）过滤；
2. 组合生成 —— 主食+可选搭配 组成候选组合（上限控制试算成本）；
3. 综合评分 —— 推荐分 = 营养匹配度 + 价格优惠度 + 用户目标匹配度（加权）。

评分与组合全部为确定性规则，LLM 不参与数值计算（报价零幻觉原则）。
"""

from __future__ import annotations

from ..models.schemas import (
    CouponInfo,
    Goal,
    IntentProfile,
    MealItem,
    NutritionInfo,
    PriceResult,
    Recommendation,
    ScoreBreakdown,
    Store,
)

# 评分权重: 营养 0.4 / 价格 0.3 / 目标 0.3（健康定位优先，可按场景调参）
W_NUTRITION, W_PRICE, W_GOAL = 0.4, 0.3, 0.3

# 组合生成上限: 最多向 calculate-price 发起 N 次试算（控制时延）
MAX_PRICE_CALLS = 5

# goal → 营养软约束（规则表，与 skill/references/nutrition-rules.md 一致）
# 字段: max_cal 热量上限kcal / min_protein 蛋白下限g / max_sodium 钠上限mg
#        max_carb 碳水上限g（低糖代理指标）/ max_fat 脂肪上限g
GOAL_NUTRITION_RULES: dict[Goal, dict] = {
    Goal.FAT_LOSS: {"max_cal": 600, "min_protein": 20, "max_sodium": 800},
    Goal.MUSCLE_GAIN: {"max_cal": 900, "min_protein": 30},
    Goal.KIDS: {"max_cal": 500, "max_sodium": 600, "min_protein": 10},
    # 低糖: 官方营养数据无"糖"字段 → 以碳水为代理（含精制碳水与膳食纤维，
    # 阈值按单餐控糖需求放宽到 50g）
    Goal.LOW_SUGAR: {"max_cal": 700, "max_carb": 50},
    # 低脂: 单餐脂肪 ≤15g（约为成人单餐脂肪参考摄入量的 1/3 上限）
    Goal.LOW_FAT: {"max_cal": 700, "max_fat": 15},
    Goal.NORMAL: {},
    # 省钱模式不走营养筛选（deal_optimizer 专用流水线），无规则
    Goal.BEST_VALUE: {},
}


# ---------------------------------------------------------------------------
# 第 1 步: 候选筛选
# ---------------------------------------------------------------------------

def filter_candidates(
    meals: list[MealItem],
    nutrition_table: list[NutritionInfo],
    intent: IntentProfile,
    nutrition_lookup,
) -> list[tuple[MealItem, NutritionInfo]]:
    """在售餐品 × 营养表交叉，产出带营养信息的候选列表。

    过滤规则（硬约束，任一超标即剔除）:
    - 热量 ≤ max_calories（用户显式给出时优先，否则用 goal 规则表）
    - 蛋白 ≥ min_protein（增肌/儿童等场景）
    - 钠 ≤ max_sodium（儿童等场景）
    - 碳水 ≤ max_carb（低糖场景，碳水代理）
    - 脂肪 ≤ max_fat（低脂场景）
    - 排序: 蛋白质密度（蛋白 g / 100kcal）降序 —— 单位热量营养回报最高
    """
    rules = GOAL_NUTRITION_RULES.get(intent.goal, {})
    max_cal = intent.max_calories or rules.get("max_cal")
    min_protein = rules.get("min_protein", 0)
    max_sodium = rules.get("max_sodium", 0)
    max_carb = rules.get("max_carb", 0)
    max_fat = rules.get("max_fat", 0)

    candidates: list[tuple[MealItem, NutritionInfo, float]] = []
    for meal in meals:
        info = nutrition_lookup(nutrition_table, meal.product_name)
        if info is None or info.calories <= 0:
            continue                          # 无营养数据的不进健康推荐
        if max_cal and info.calories > max_cal:
            continue                          # 热量超标，硬过滤
        if min_protein and info.protein < min_protein:
            continue                          # 蛋白不达标（增肌等场景）
        if max_sodium and info.sodium > max_sodium:
            continue                          # 钠超标（儿童等场景）
        if max_carb and info.carbohydrate > max_carb:
            continue                          # 碳水超标（低糖代理）
        if max_fat and info.fat > max_fat:
            continue                          # 脂肪超标（低脂场景）
        density = info.protein / max(info.calories, 1) * 100
        candidates.append((meal, info, density))

    candidates.sort(key=lambda x: x[2], reverse=True)
    return [(m, i) for m, i, _ in candidates[:20]]     # 上限 20 个候选


# ---------------------------------------------------------------------------
# 第 2 步: 组合生成（确定性，无 LLM）
# ---------------------------------------------------------------------------

def build_combos(
    candidates: list[tuple[MealItem, NutritionInfo]],
    intent: IntentProfile,
) -> list[list[MealItem]]:
    """把候选单品组成 2-3 个候选组合（确定性贪心，无 LLM）。

    策略（多基准贪心）:
    - 基准 A: 营养密度最优单品 → 贪心填充低热量候选
    - 基准 B: 最低热量候选 → 贪心填充（给"吃得少"的备选组合）
    所有组合都不得突破营养上限（热量/碳水/脂肪；用户显式值优先，否则用规则表）。
    """
    if not candidates:
        return []
    rules = GOAL_NUTRITION_RULES.get(intent.goal, {})
    caps = {
        "calories": intent.max_calories or rules.get("max_cal") or 1200,
        "carbohydrate": rules.get("max_carb") or float("inf"),
        "fat": rules.get("max_fat") or float("inf"),
        "sodium": rules.get("max_sodium") or float("inf"),
        # 预算约束: 按餐牌价估计，最终以 calculate-price 券后价复核
        "price": intent.budget or float("inf"),
    }

    def _fits(combo: list[tuple[MealItem, NutritionInfo]]) -> bool:
        """组合是否仍在全部上限内（热量/碳水/脂肪/钠/预算估计）。"""
        cal = sum(i.calories for _, i in combo)
        carb = sum(i.carbohydrate for _, i in combo)
        fat = sum(i.fat for _, i in combo)
        sodium = sum(i.sodium for _, i in combo)
        price = sum(m.price for m, _ in combo)
        return (cal <= caps["calories"] and carb <= caps["carbohydrate"]
                and fat <= caps["fat"] and sodium <= caps["sodium"]
                and price <= caps["price"])

    # 除重排序: 最低热量候选（用于填充热量余量）
    low_cal = sorted(candidates, key=lambda c: c[1].calories)
    bases = [candidates[0]]                     # 基准 A: 营养最优
    if low_cal[0][0].product_code != candidates[0][0].product_code:
        bases.append(low_cal[0])                # 基准 B: 最低热量

    combos: list[list[MealItem]] = []
    seen: set[tuple[str, ...]] = set()
    for base in bases:
        current = [base]
        for cand in low_cal:
            if cand[0].product_code in {m.product_code for m, _ in current}:
                continue
            if len(current) >= 3:
                break
            if _fits(current + [cand]):
                current = current + [cand]
        key = tuple(sorted(m.product_code for m, _ in current))
        if key not in seen:
            seen.add(key)
            combos.append([m for m, _ in current])
    return combos[:3]


# ---------------------------------------------------------------------------
# 第 3 步: 综合评分
# ---------------------------------------------------------------------------

def score_nutrition(total: NutritionInfo, intent: IntentProfile) -> float:
    """营养匹配度 0-100: 热量越接近(不超)目标、蛋白越高，分越高。"""
    rules = GOAL_NUTRITION_RULES.get(intent.goal, {})
    target_cal = intent.max_calories or rules.get("max_cal") or 800
    min_protein = rules.get("min_protein", 15)

    # 热量分: 占目标 70-100% 满分（吃太少吃太饱都不健康），超目标线性扣分
    ratio = total.calories / target_cal if target_cal else 1.0
    if 0.7 <= ratio <= 1.0:
        cal_score = 100.0
    elif ratio < 0.7:
        cal_score = max(0.0, ratio / 0.7 * 100)
    else:
        cal_score = max(0.0, 100 - (ratio - 1.0) * 200)     # 每超 10% 扣 20 分

    # 蛋白分: 达到下限满分，不足按比例
    pro_score = min(100.0, total.protein / max(min_protein, 1) * 100)
    return round(cal_score * 0.6 + pro_score * 0.4, 1)


def score_price(price: PriceResult, intent: IntentProfile) -> float:
    """价格优惠度 0-100: 预算内 + 折扣力度大 → 高分。"""
    budget = intent.budget
    # 预算分: 预算内按剩余比例给分（花得越少越好），超预算 0 分
    if budget:
        ratio = price.final_price / budget
        budget_score = max(0.0, 100 - max(ratio - 0.6, 0) * 250) if ratio <= 1 else 0.0
    else:
        budget_score = 60.0        # 无预算约束给基准分

    # 折扣分: 优惠占原价比例 × 100
    discount_ratio = price.discount / price.original_price if price.original_price else 0
    discount_score = min(100.0, discount_ratio * 250)
    return round(budget_score * 0.7 + discount_score * 0.3, 1)


def score_goal(total: NutritionInfo, intent: IntentProfile) -> float:
    """用户目标匹配度 0-100: 意图显式约束的达成度。"""
    parts: list[float] = []
    # 热量目标达成
    if intent.max_calories:
        parts.append(100.0 if total.calories <= intent.max_calories else 0.0)
    # goal 专属约束达成度（每条规则等权平均）
    rules = GOAL_NUTRITION_RULES.get(intent.goal, {})
    if rules.get("min_protein"):
        parts.append(100.0 if total.protein >= rules["min_protein"] else
                     total.protein / rules["min_protein"] * 100)
    if rules.get("max_sodium"):
        parts.append(100.0 if total.sodium <= rules["max_sodium"] else
                     max(0.0, 100 - (total.sodium - rules["max_sodium"]) / 10))
    if rules.get("max_carb"):
        parts.append(100.0 if total.carbohydrate <= rules["max_carb"] else
                     max(0.0, 100 - (total.carbohydrate - rules["max_carb"]) * 4))
    if rules.get("max_fat"):
        parts.append(100.0 if total.fat <= rules["max_fat"] else
                     max(0.0, 100 - (total.fat - rules["max_fat"]) * 8))
    if not parts:
        parts.append(80.0)         # 普通消费无特殊目标 → 基准分
    return round(sum(parts) / len(parts), 1)


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def generate_recommendations(
    meals: list[MealItem],
    nutrition_table: list[NutritionInfo],
    intent: IntentProfile,
    price_results: list[tuple[list[MealItem], PriceResult]],
    store: Store | None = None,
    coupons: list[CouponInfo] | None = None,
    nutrition_lookup=None,
) -> list[Recommendation]:
    """聚合入口: 已试算的组合 → 打分 → 排序 → Recommendation 列表。

    price_results 由 orchestrator 调 pricing 服务批量试算后传入，
    本函数不发起任何远程调用（纯本地计算，可单测）。
    """
    from .nutrition import find_nutrition as default_lookup
    lookup = nutrition_lookup or default_lookup

    results: list[Recommendation] = []
    for combo_items, price in price_results:
        # 合计营养
        total = NutritionInfo(name="组合合计")
        for meal in combo_items:
            info = lookup(nutrition_table, meal.product_name)
            if info:
                total.calories += info.calories
                total.protein += info.protein
                total.fat += info.fat
                total.carbohydrate += info.carbohydrate
                total.sodium += info.sodium

        # 三维打分: 推荐分 = 营养匹配度 + 价格优惠度 + 用户目标匹配度
        score = ScoreBreakdown(
            nutrition_match=score_nutrition(total, intent),
            price_saving=score_price(price, intent),
            goal_match=score_goal(total, intent),
        )
        score.compute_total(W_NUTRITION, W_PRICE, W_GOAL)

        combo_name = _name_combo(combo_items, intent)
        results.append(
            Recommendation(
                combo_name=combo_name,
                items=combo_items,
                nutrition=total,
                price=price,
                coupons_used=coupons or [],
                store=store,
                score=score,
            )
        )

    results.sort(key=lambda r: r.score.total, reverse=True)
    return results[:3]                        # Top 3


def _name_combo(items: list[MealItem], intent: IntentProfile) -> str:
    """给组合起可解释的名字（模板拼接，非 LLM）。"""
    goal_label = {
        "减脂": "轻食", "增肌": "高蛋白", "儿童": "儿童友好",
        "低糖": "控糖", "低脂": "低脂", "省钱": "超值",
    }.get(intent.goal.value, "随心")
    size = "单品" if len(items) == 1 else f"{len(items)}件组合"
    return f"{goal_label}{size}"
