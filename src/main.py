"""McSmart 麦智点餐官 — 入口。

端到端串联五段流水线（见 docs/agent-workflow.md）:

    用户自然语言
      → ① 意图识别 (agent.orchestrator)
      → ② 工具规划 (agent.planner)
      → ③ MCP 执行 (services × mcp.client)
      → ④ 数据融合 (services.recommend)
      → ⑤ 建议生成 (models.Recommendation.render)

用法:
    cd McSmart && python -m src.main "帮我推荐一个500卡以内，30元以内的麦当劳套餐"
"""

from __future__ import annotations

import json
import sys

from .agent.orchestrator import recognize_intent
from .agent.planner import build_plan, describe_plan
from .mcp.client import MCPClient, MCPError
from .models.schemas import SessionState
from .services import coupon as coupon_svc
from .services import deal_optimizer as deal_svc
from .services import food_query as food_svc
from .services import nutrition as nutrition_svc
from .services import pricing as pricing_svc
from .services import recommend as recommend_svc
from .services import store_locator as store_svc


class McSmartAgent:
    """会话级 Agent: 持有 MCP 客户端与当前会话状态。"""

    def __init__(self) -> None:
        self.client = MCPClient()
        self.state = SessionState.IDLE
        self.notes: list[str] = []          # 降级标注（透传到推荐结果）

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def recommend(self, user_text: str) -> str:
        """输入自然语言 → 返回渲染后的推荐结果文本。"""
        # ① 意图识别 -----------------------------------------------------
        self.state = SessionState.INTENT_PARSING
        intent = recognize_intent(user_text)
        self.state = SessionState.PLANNING

        # 门店查询模式走轻量分支（只需 query-nearby-stores 一个工具）
        if intent.store_query:
            return self.find_stores(intent)

        # 省钱模式走专用优惠优化流水线
        if intent.goal.value == "省钱":
            return self.best_value(intent)

        # ② 工具规划 -----------------------------------------------------
        plan = build_plan(intent)
        print("[Agent] 意图:", json.dumps(intent.as_demo_json(), ensure_ascii=False))
        print("[Agent] 调用计划:\n" + describe_plan(plan), end="\n\n")

        # ③ MCP 执行（波次内串行简化实现，结构上已按 DAG 组织） -----------
        self.state = SessionState.EXECUTING

        # Wave 1: 门店 + 营养表
        nutrition_table = []
        stores = store_svc.query_nearby_stores(self.client, city=intent.city)
        store = store_svc.pick_primary_store(stores)
        if store is None:
            self.state = SessionState.ERROR
            return f"❌ 在{intent.city}未找到可点餐门店，请换个城市试试"
        print(f"[Agent] 门店: {store.store_name} ({intent.city}, 共{len(stores)}家)")
        if any(n.real_name == "list-nutrition-foods" for n in plan.all_nodes()):
            try:
                nutrition_table = nutrition_svc.get_nutrition(self.client)
                print(f"[Agent] 营养数据: {len(nutrition_table)} 条餐品")
            except MCPError as exc:
                self.notes.append("营养数据获取失败，本次推荐未含营养评分")
                print(f"[Agent] ⚠ 营养数据降级: {exc}")

        # Wave 2: 餐品 + 优惠
        meals = food_svc.query_meals(
            self.client, store.store_code,
            order_type=1 if intent.dining_mode != "delivery" else 2,
            be_type={"dine_in": 1, "drive_thru": 5, "delivery": 2}[intent.dining_mode],
        )
        print(f"[Agent] 在售餐品: {len(meals)} 个")

        coupons = []
        try:
            coupons = coupon_svc.available_coupons(self.client)
            print(f"[Agent] 可领优惠: {len(coupons)} 张")
        except MCPError:
            self.notes.append("可领券查询失败，价格按无券试算")
        try:
            be_type = {"dine_in": 1, "drive_thru": 5, "delivery": 2}[intent.dining_mode]
            promos = coupon_svc.query_promotions(
                self.client, store.store_code,
                order_type=1 if intent.dining_mode != "delivery" else 2,
                be_type=be_type,
            )
            if promos:
                coupons = coupons + promos
                print(f"[Agent] 门店促销: {len(promos)} 条")
        except MCPError:
            self.notes.append("门店促销查询失败")

        # ④ 数据融合: 交叉筛选 → 组合生成 → 批量试算 -----------------------
        self.state = SessionState.AGGREGATING
        candidates = recommend_svc.filter_candidates(
            meals, nutrition_table, intent, nutrition_svc.find_nutrition,
        )
        print(f"[Agent] 营养达标候选: {len(candidates)} 个")
        if not candidates:
            return "❌ 没有找到符合营养目标的餐品，试试放宽热量限制？"

        combos = recommend_svc.build_combos(candidates, intent)

        # Wave 3: 价格试算（上限 MAX_PRICE_CALLS 次控制时延）
        priced: list[tuple[list, object]] = []
        for combo in combos[: recommend_svc.MAX_PRICE_CALLS]:
            try:
                best_coupon = _pick_coupon(coupons, sum(m.price for m in combo))
                price = pricing_svc.calculate_price(
                    self.client, store.store_code, combo,
                    coupon_id=best_coupon.coupon_id if best_coupon else "",
                )
                priced.append((combo, price))
            except MCPError as exc:
                self.notes.append(f"组合试算失败: {exc}")

        # ⑤ 建议生成 ------------------------------------------------------
        recs = recommend_svc.generate_recommendations(
            meals, nutrition_table, intent, priced,
            store=store, coupons=coupons,
            nutrition_lookup=nutrition_svc.find_nutrition,
        )
        for rec in recs:
            rec.notes.extend(self.notes)

        self.state = SessionState.RECOMMENDATION_READY
        header = f"已为「{intent.goal.value}」目标生成 {len(recs)} 套方案:\n"
        body = "\n\n".join(rec.render(i + 1) for i, rec in enumerate(recs))
        return header + body

    # ------------------------------------------------------------------
    # 门店查询模式: 附近门店推荐流水线
    # ------------------------------------------------------------------

    def find_stores(self, intent) -> str:
        """"附近哪里可以买": 只调 query-nearby-stores，输出距离/营业状态/推荐理由。"""
        print("[Agent] 意图: 门店查询（仅 query-nearby-stores，轻量链路）")
        print(f"[Agent] 目标城市: {intent.city}\n")

        self.state = SessionState.EXECUTING
        try:
            stores = store_svc.query_nearby_stores(self.client, city=intent.city)
        except MCPError as exc:
            self.state = SessionState.ERROR
            return f"❌ 门店查询失败: {exc}"

        if not stores:
            self.state = SessionState.ERROR
            return f"❌ 在{intent.city}未找到可点餐门店，请换个城市或关键词试试"

        self.state = SessionState.AGGREGATING
        recs = store_svc.recommend_stores(stores)
        self.state = SessionState.RECOMMENDATION_READY
        print(f"[Agent] 查到 {len(stores)} 家门店，推荐前 {len(recs)} 家:\n")

        # 城市来源标注: 未提供城市时用的是默认城市，必须显式告知用户可覆盖
        # （官方门店工具不接受经纬度，城市是定位链路唯一可控锚点，不能静默猜）
        city_note = ""
        if intent.city_inferred:
            city_note = (f"（未提供城市，按默认城市「{intent.city}」查询，"
                         f"可加\"在XX\"指定其他城市）")
        header = (f"「{intent.city}」附近有 {len(stores)} 家麦当劳可点餐，"
                  f"为你推荐{city_note}:\n")
        body = "\n\n".join(r.render(i + 1) for i, r in enumerate(recs))
        tail = "\n\n💬 告诉我想吃什么（如\"推荐个500卡以内的\"），我可以基于门店继续推荐餐品～"
        return header + body + tail


    # ------------------------------------------------------------------
    # 省钱模式: 优惠优化流水线
    # ------------------------------------------------------------------

    def best_value(self, intent) -> str:
        """"怎么吃最划算": 我的券 → 门店券 → 活动 → 套餐 → 最低价组合试算。"""
        self.state = SessionState.PLANNING
        plan = build_plan(intent)
        print("[Agent] 意图: {}（省钱模式）".format(intent.goal.value))
        print("[Agent] 调用计划:\n" + describe_plan(plan), end="\n\n")

        self.state = SessionState.EXECUTING
        stores = store_svc.query_nearby_stores(self.client, city=intent.city)
        store = store_svc.pick_primary_store(stores)
        if store is None:
            self.state = SessionState.ERROR
            return f"❌ 在{intent.city}未找到可点餐门店，请换个城市试试"
        print(f"[Agent] 门店: {store.store_name} ({intent.city}, 共{len(stores)}家)")

        self.state = SessionState.AGGREGATING
        deals, summary = deal_svc.find_best_deals(
            self.client, store, intent, log=print,
        )
        if not deals:
            self.state = SessionState.ERROR
            return "❌ 未获取到可试算的优惠方案（门店可能暂无可用券）"

        self.state = SessionState.RECOMMENDATION_READY
        # 优惠情报概览（我的券 / 门店活动 / 可领券）
        info: list[str] = []
        if summary["my_coupons"]:
            titles = "、".join(c.title for c in summary["my_coupons"][:3])
            more = f" 等{len(summary['my_coupons'])}张" if len(summary["my_coupons"]) > 3 else ""
            info.append(f"我的券: {titles}{more}")
        if summary["promotions"]:
            info.append(f"门店活动: {len(summary['promotions'])} 条进行中")
        if summary["claimable"]:
            info.append(f"还可领: {len(summary['claimable'])} 张（麦麦省）")

        header = "已为你算出最划算的吃法（价格均为实时试算）:\n"
        if info:
            header += "  " + " | ".join(info) + "\n"
        if intent.budget:
            header += f"  预算约束: ¥{intent.budget:.0f} 以内\n"
        body = "\n\n".join(d.render(i + 1) for i, d in enumerate(deals))
        return header + "\n" + body


def _pick_coupon(coupons, total_price: float):
    """按"门槛可达且折扣最大"选一张最优券（确定性规则）。"""
    usable = [c for c in coupons if c.min_spend <= total_price and c.discount > 0]
    return max(usable, key=lambda c: c.discount) if usable else None


def main() -> None:
    """CLI 入口: python -m src.main "<自然语言需求>" """
    text = " ".join(sys.argv[1:]) or "帮我推荐一个500卡以内，30元以内的麦当劳套餐"
    print(f"用户: {text}\n" + "-" * 56)
    agent = McSmartAgent()
    print("-" * 56)
    print(agent.recommend(text))


if __name__ == "__main__":
    main()
