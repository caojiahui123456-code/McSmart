"""优惠优化服务（能力③+: "怎么吃最划算"模式）。

面向 "今天吃麦当劳怎么最划算" 这类输入，编排四步:
1. 我的券概览（query-my-coupons；为空时自动一键领券 auto-bind-coupons 并复查）;
2. 门店可用券（query-store-coupons —— 唯一带 couponId+couponCode+适用商品 的数据源）;
3. 门店活动（query-promotions，按 beType 过滤）与在售餐品（query-meals）;
4. 逐个"券×商品"组合调 calculate-price 试算，产出
   原价 / 优惠金额 / 最终价格 / 节省比例，按节省比例排序输出 Top 方案。

报价零幻觉: 所有金额均来自 calculate-price 实测返回（分→元），LLM 不参与数值。

调用成本控制: 券试算 ≤ MAX_COUPON_TRIES 次，无券基线 ≤ MAX_BASELINE_TRIES 次。
"""

from __future__ import annotations

from ..mcp.client import MCPClient, MCPError
from ..models.schemas import DealResult, IntentProfile, MealItem, Store
from . import coupon as coupon_svc
from . import food_query as food_svc
from . import pricing as pricing_svc

# 试算调用上限（控制时延，比赛现场 3-5 秒目标的保障）
MAX_COUPON_TRIES = 8        # 券后试算次数上限
MAX_BASELINE_TRIES = 3      # 无券基线试算次数上限
MAX_COMBO_TRIES = 2         # 组合试算次数上限
MIN_BASELINE_PRICE = 10.0   # 基线候选价格门槛（元），排除酱料/饼干等小配件


def find_best_deals(
    client: MCPClient,
    store: Store,
    intent: IntentProfile,
    order_type: int = 1,
    be_type: int = 1,
    allow_auto_bind: bool = True,
    log=print,
) -> tuple[list[DealResult], dict]:
    """主入口: 返回 (省钱方案列表, 优惠情报摘要)。

    方案按节省比例降序；摘要供上层渲染"我的券/门店活动/可领券"概览。
    """
    summary: dict = {"my_coupons": [], "promotions": [], "claimable": [], "notes": []}

    # --- Step 1: 我的券（为空则一键领券后复查） -------------------------
    my = coupon_svc.my_coupons(client)
    if not my and allow_auto_bind:
        log("[Agent] 我的券为空 → 自动一键领券（auto-bind-coupons）")
        bound, _ = coupon_svc.auto_bind(client)
        if bound:
            log(f"[Agent] 领券成功 {bound} 张")
            my = coupon_svc.my_coupons(client)
    summary["my_coupons"] = my
    log(f"[Agent] 我的优惠券: {len(my)} 张"
        + (f"（{'、'.join(c.title for c in my[:3])}{'…' if len(my) > 3 else ''}）" if my else ""))

    # --- Step 2: 门店可用券（带 couponId/couponCode/适用商品） -----------
    store_cs = coupon_svc.store_coupons(client, store.store_code, order_type, be_type)
    usable = [c for c in store_cs if c.coupon_id and c.coupon_code and c.product_codes]
    log(f"[Agent] 门店可用券(可试算): {len(usable)} 张")

    # --- Step 3: 门店活动 + 在售餐品 ------------------------------------
    try:
        summary["promotions"] = coupon_svc.query_promotions(
            client, store.store_code, order_type=order_type, be_type=be_type,
        )
        log(f"[Agent] 门店活动: {len(summary['promotions'])} 条")
    except MCPError as exc:
        summary["notes"].append(f"门店活动查询失败: {exc}")
    try:
        summary["claimable"] = coupon_svc.available_coupons(client)
    except MCPError:
        pass        # 可领券仅展示用，失败不阻断

    meals = food_svc.query_meals(client, store.store_code,
                                 order_type=order_type, be_type=be_type)
    meal_by_code = {m.product_code: m for m in meals}
    log(f"[Agent] 在售餐品: {len(meals)} 个")

    # --- Step 4: 试算 ----------------------------------------------------
    deals, best_pair = _price_coupon_deals(client, store, usable, meal_by_code,
                                           order_type, be_type, log)
    deals += _price_baseline_deals(client, store, meals,
                                   order_type, be_type, log)
    deals.sort(key=lambda d: (d.savings_ratio, d.discount), reverse=True)

    # --- Step 5: 组合优化（券商品 + 低价正餐 → 一顿完整套餐） ------------
    if best_pair is not None:
        combo_deals = _price_combo_deals(
            client, store, best_pair, meals, intent,
            order_type, be_type, log,
        )
        deals = combo_deals + deals
        deals.sort(key=lambda d: (d.savings_ratio, d.discount), reverse=True)

    # 预算过滤: 优先保留预算内的方案，全部超预算时保留最接近预算的并标注
    if intent.budget:
        within = [d for d in deals if d.final_price <= intent.budget]
        if within:
            deals = within
        elif deals:
            deals = [min(deals, key=lambda d: d.final_price)]
            deals[0].notes.append(f"所有方案均超出预算 ¥{intent.budget:.0f}，已选最便宜的一档")

    return deals[:3], summary


# ---------------------------------------------------------------------------
# 内部: 券后试算 / 无券基线
# ---------------------------------------------------------------------------

def _price_coupon_deals(
    client: MCPClient,
    store: Store,
    usable: list,
    meal_by_code: dict[str, MealItem],
    order_type: int,
    be_type: int,
    log,
) -> tuple[list[DealResult], tuple | None]:
    """逐个"券×适用商品"试算券后价（calculate-price 需 couponId+couponCode 成对传）。

    返回 (方案列表, 首个成功的 (券, 在售餐品) 元组) —— 后者作为组合优化的种子。
    """
    deals: list[DealResult] = []
    best_pair: tuple | None = None
    tries = 0
    for coupon in usable:
        for code in coupon.product_codes:
            if tries >= MAX_COUPON_TRIES:
                return deals, best_pair
            meal = meal_by_code.get(code)
            try:
                price = pricing_svc.calculate_price(
                    client, store.store_code, [meal] if meal else
                    [MealItem(product_code=code, product_name=code)],
                    order_type=order_type, be_type=be_type,
                    coupon_id=coupon.coupon_id, coupon_code=coupon.coupon_code,
                )
                deal = DealResult(
                    title=f"用券吃「{(meal.product_name if meal else coupon.title)}」",
                    product_names=[meal.product_name if meal else coupon.title],
                    coupon=coupon,
                    original_price=price.original_price,
                    discount=price.discount,
                    final_price=price.final_price,
                    store=store,
                )
                deal.compute_ratio()
                deals.append(deal)
                if best_pair is None and meal is not None:
                    best_pair = (coupon, meal)
                tries += 1
            except MCPError as exc:
                log(f"[Agent] ⚠ 券试算失败 {coupon.title}: {exc}")
    return deals, best_pair


def _price_baseline_deals(
    client: MCPClient,
    store: Store,
    meals: list[MealItem],
    order_type: int,
    be_type: int,
    log,
) -> list[DealResult]:
    """无券基线: 对最便宜的几件正餐试算（calculate-price 已含平台直降）。

    候选过滤: 价格 ≥ MIN_BASELINE_PRICE，排除酱料/饼干等小配件
    （实测 query-meals 最低价是 ¥2 素龙饼干 / ¥3 蘸酱，不是"怎么吃"的答案）。
    """
    deals: list[DealResult] = []
    mains = [m for m in meals if m.price >= MIN_BASELINE_PRICE]
    for meal in sorted(mains, key=lambda m: m.price)[:MAX_BASELINE_TRIES]:
        try:
            price = pricing_svc.calculate_price(
                client, store.store_code, [meal],
                order_type=order_type, be_type=be_type,
            )
            deal = DealResult(
                title=f"单品直降「{meal.product_name}」",
                product_names=[meal.product_name],
                coupon=None,
                original_price=price.original_price,
                discount=price.discount,
                final_price=price.final_price,
                store=store,
            )
            deal.compute_ratio()
            deals.append(deal)
        except MCPError as exc:
            log(f"[Agent] ⚠ 基线试算失败 {meal.product_name}: {exc}")
    return deals


def _price_combo_deals(
    client: MCPClient,
    store: Store,
    best_pair: tuple,
    meals: list[MealItem],
    intent: IntentProfile,
    order_type: int,
    be_type: int,
    log,
) -> list[DealResult]:
    """组合优化: 最优券商品 + 1~2 件低价正餐 → 一顿完整套餐。

    券只挂到券商品（pricing.calculate_price 的 coupon_product_code 参数）。
    预算约束: 用券后小估计价（券商品券后价 + 正餐平台价）预过滤，再实测。
    """
    coupon, coupon_meal = best_pair
    additions = [m for m in sorted(meals, key=lambda m: m.price)
                 if m.price >= MIN_BASELINE_PRICE
                 and m.product_code != coupon_meal.product_code]
    if not additions:
        return []

    # 组合候选: +1 件 / +2 件（有预算时按估计总价预过滤，避免无效试算）
    candidates: list[list[MealItem]] = []
    for extra in (additions[:1], additions[:2]):
        if not extra:
            continue
        est_total = _estimated_final(coupon_meal, extra)
        if intent.budget and est_total > intent.budget:
            continue
        if any(extra[0].product_code == c.product_code for c in extra[1:]):
            continue
        candidates.append([coupon_meal] + list(extra))

    deals: list[DealResult] = []
    for combo in candidates[:MAX_COMBO_TRIES]:
        try:
            price = pricing_svc.calculate_price(
                client, store.store_code, combo,
                order_type=order_type, be_type=be_type,
                coupon_id=coupon.coupon_id, coupon_code=coupon.coupon_code,
                coupon_product_code=coupon_meal.product_code,
            )
            names = [m.product_name for m in combo]
            deal = DealResult(
                title="超值组合「" + " + ".join(names) + "」",
                product_names=names,
                coupon=coupon,
                original_price=price.original_price,
                discount=price.discount,
                final_price=price.final_price,
                store=store,
            )
            deal.compute_ratio()
            deals.append(deal)
        except MCPError as exc:
            log(f"[Agent] ⚠ 组合试算失败: {exc}")
    return deals


def _estimated_final(coupon_meal: MealItem, extras: list[MealItem]) -> float:
    """组合估计总价（用于预算预过滤; 真实价格以 calculate-price 为准）。

    估计口径: 券商品按餐牌价（query-meals 的平台价），附加项同理 ——
    高估不误筛，最终展示值仍全部来自实测。
    """
    return round(coupon_meal.price + sum(m.price for m in extras), 2)
