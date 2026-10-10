"""McSmart 单元测试（离线，不依赖 MCP Server）。

覆盖: 意图识别、营养表解析、组合生成、三维评分、别名解析、
      优惠优化（我的券/门店券解析、节省比例、方案排序）。
运行: cd McSmart && python -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.orchestrator import recognize_intent
from src.mcp.tools import is_high_risk, resolve
from src.models.schemas import (
    DealResult,
    Goal,
    IntentProfile,
    MealItem,
    NutritionInfo,
    PriceResult,
    Store,
    StoreCoupon,
)
from src.services.coupon import auto_bind, my_coupons, store_coupons
from src.services.nutrition import _parse_table
from src.services.recommend import (
    build_combos,
    filter_candidates,
    score_goal,
    score_nutrition,
    score_price,
)
from src.services.store_locator import _build_reason, recommend_stores

# 官方紧凑表格样例（截取自真实返回）
_TABLE_SAMPLE = (
    "[3]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,"
    "carbohydrate,sodium,calcium}:\n"
    "  猪柳麦满分,null,1288,308,16,16,24,781,213\n"
    "  小薯条,null,877,210,3,9,28,120,13\n"
    "  麦辣鸡腿堡,null,2050,490,22,26,49,890,60\n"
)


# ---------------------------------------------------------------------------
# 意图识别
# ---------------------------------------------------------------------------

def test_intent_extraction_500cal_30yuan():
    """需求示例: 500卡 + 30元 → 减脂 + 显式约束。"""
    intent = recognize_intent("帮我推荐一个500卡以内，30元以内的麦当劳套餐")
    assert intent.goal == Goal.FAT_LOSS      # 低卡约束(≤600)推断减脂
    assert intent.max_calories == 500
    assert intent.budget == 30.0


def test_intent_keywords():
    assert recognize_intent("我最近减肥，想吃麦当劳").goal == Goal.FAT_LOSS
    assert recognize_intent("增肌期要高蛋白").goal == Goal.MUSCLE_GAIN
    assert recognize_intent("给孩子买点吃的").goal == Goal.KIDS
    assert recognize_intent("随便吃点").goal == Goal.NORMAL


def test_intent_best_value_mode():
    """"怎么最划算"类输入进入省钱模式。"""
    assert recognize_intent("今天吃麦当劳怎么最划算").goal == Goal.BEST_VALUE
    assert recognize_intent("想吃麦当劳，怎么买最省钱，预算20元").goal == Goal.BEST_VALUE
    assert recognize_intent("帮我薅羊毛").goal == Goal.BEST_VALUE
    # 省钱词优先于健康词（"减肥怎么吃最划算"按显式省钱目标处理）
    assert recognize_intent("减肥但今天想放纵，怎么最便宜").goal == Goal.BEST_VALUE


def test_intent_health_mode_goals():
    """健康模式五目标: 减脂/增肌/儿童/低糖/低脂。"""
    assert recognize_intent("我在控糖，推荐个低糖的晚餐").goal == Goal.LOW_SUGAR
    assert recognize_intent("无糖饮食有啥能吃的").goal == Goal.LOW_SUGAR
    assert recognize_intent("想吃点低脂清淡的").goal == Goal.LOW_FAT
    assert recognize_intent("少油忌口，推荐一下").goal == Goal.LOW_FAT
    assert recognize_intent("我最近减肥").goal == Goal.FAT_LOSS
    assert recognize_intent("增肌期要高蛋白").goal == Goal.MUSCLE_GAIN
    assert recognize_intent("给孩子买点吃的").goal == Goal.KIDS


def test_intent_city_and_delivery():
    intent = recognize_intent("在上海点外送，预算50元")
    assert intent.city == "上海"
    assert intent.dining_mode == "delivery"
    assert intent.budget == 50.0


def test_intent_city_slot_and_default_flag():
    """城市槽位: 识别到城市则 city_inferred=False；未提城市用默认值并标记为推断。

    定位背景: 官方 query-nearby-stores 不接受经纬度参数，"位置"由服务端按账号判定，
    客户端唯一可控锚点是 city + keyword，所以城市槽位与"是否推断"必须显式可测。
    """
    explicit = recognize_intent("上海附近哪里可以买")
    assert explicit.city == "上海" and explicit.city_inferred is False

    inferred = recognize_intent("附近哪里可以买")
    assert inferred.city_inferred is True
    assert "location" in inferred.missing_slots    # 输出层需告知可覆盖

    # 扩充后的城市词表: 非一线城市也要能识别
    assert recognize_intent("长沙有啥麦当劳").city == "长沙"
    assert recognize_intent("青岛麦当劳推荐").city == "青岛"


# ---------------------------------------------------------------------------
# 营养表解析
# ---------------------------------------------------------------------------

def test_nutrition_table_parsing():
    infos = _parse_table(_TABLE_SAMPLE)
    assert len(infos) == 3
    assert infos[0].name == "猪柳麦满分"
    assert infos[0].calories == 308
    assert infos[0].protein == 16
    assert infos[1].name == "小薯条"
    assert infos[1].sodium == 120


# ---------------------------------------------------------------------------
# 组合生成
# ---------------------------------------------------------------------------

def _make_candidates():
    return [
        (MealItem(product_code="A", product_name="鸡排", price=14),
         NutritionInfo(name="鸡排", calories=385, protein=24)),
        (MealItem(product_code="B", product_name="双层吉士", price=23),
         NutritionInfo(name="双层吉士", calories=429, protein=27)),
        (MealItem(product_code="C", product_name="小薯条", price=8),
         NutritionInfo(name="小薯条", calories=210, protein=3)),
    ]


def test_build_combos_respects_calorie_cap():
    intent = IntentProfile(goal=Goal.FAT_LOSS, max_calories=500, budget=30)
    combos = build_combos(_make_candidates(), intent)
    assert 1 <= len(combos) <= 3
    # 所有组合热量必须 ≤ 500
    for combo in combos:
        total = sum(
            info.calories for m, info in _make_candidates()
            if m.product_code in {x.product_code for x in combo}
        )
        assert total <= 500


# ---------------------------------------------------------------------------
# 三维评分: 推荐分 = 营养匹配度 + 价格优惠度 + 用户目标匹配度
# ---------------------------------------------------------------------------

def test_score_nutrition_full_marks():
    intent = IntentProfile(goal=Goal.FAT_LOSS, max_calories=500, budget=30)
    info = NutritionInfo(name="x", calories=450, protein=25)   # 90%目标+蛋白达标
    assert score_nutrition(info, intent) > 80


def test_score_price_within_budget_with_discount():
    intent = IntentProfile(goal=Goal.FAT_LOSS, max_calories=500, budget=30)
    price = PriceResult(original_price=25, final_price=14, discount=11)
    assert score_price(price, intent) > 80                     # 预算内+大折扣


def test_score_price_over_budget():
    intent = IntentProfile(goal=Goal.FAT_LOSS, max_calories=500, budget=30)
    price = PriceResult(original_price=40, final_price=35, discount=5)
    assert score_price(price, intent) < 40                     # 超预算重罚


def test_score_goal_calorie_violation():
    intent = IntentProfile(goal=Goal.FAT_LOSS, max_calories=500, budget=30)
    info = NutritionInfo(name="x", calories=600, protein=25)   # 超热量 → 拉低
    assert score_goal(info, intent) < 100


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------

def test_alias_resolution():
    assert resolve("mcd-mcp_query-nutrition-foods") == "list-nutrition-foods"
    assert resolve("mcd-mcp_calculate-price") == "calculate-price"
    assert resolve("query-meals") == "query-meals"             # 真名直通


def test_unknown_tool_rejected():
    try:
        resolve("mcd-mcp_invented-tool")
        raise AssertionError("应当拒绝未注册工具")
    except KeyError:
        pass


def test_high_risk_gate():
    assert is_high_risk("mcd-mcp_create-order") is True
    assert is_high_risk("mcd-mcp_query-meals") is False


# ---------------------------------------------------------------------------
# 优惠优化（省钱模式）
# ---------------------------------------------------------------------------

class _FakeClient:
    """离线假客户端: 按工具名返回预置响应（真实返回形态的截取）。"""

    def __init__(self, responses: dict):
        self._responses = responses
        self.calls: list[tuple] = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self._responses[name]


_MY_COUPONS_MD = (
    "# 您的优惠券列表\n\n共 2 张可用优惠券 | 第 1/1 页\n\n"
    "## 9.9元中杯冰美式\n- **优惠**: ¥9.9 (用券价格)\n- **有效期**: 2026-10-10 00:00-2026-10-16 23:59\n\n"
    "## 人气麦旋风买一送一\n- **优惠**: ¥15 (用券价格)\n"
)

_STORE_COUPONS_JSON = {
    "success": True, "code": 200, "data": [
        {
            "title": "9.9元中杯冰美式",
            "couponId": "ID123", "couponCode": "CODE123",
            "tradeDateTime": "2026-10-10 00:00:00-2026-10-16 23:59:59",
            "products": [{"productCode": "507411", "productName": "麦咖啡™冰美式中杯"}],
            "promotionId": "112023002",
        },
    ],
}

_BIND_MD = (
    "### 领券结果\n\n总计: 2 张优惠券\n成功: 2 张\n失败: 0 张\n\n"
    "#### 成功领取的优惠券：\n\n"
    "- 9.9元中杯冰美式\n  - couponId：ID123\n  - couponCode：CODE123\n"
    "- 买一送一券\n  - couponId：ID456\n  - couponCode：CODE456\n"
)


def test_my_coupons_markdown_parsing():
    client = _FakeClient({"query-my-coupons": _MY_COUPONS_MD})
    coupons = my_coupons(client)
    assert len(coupons) == 2
    assert coupons[0].title == "9.9元中杯冰美式"
    assert coupons[0].discount == 9.9


def test_auto_bind_parsing():
    client = _FakeClient({"auto-bind-coupons": _BIND_MD})
    count, coupons = auto_bind(client)
    assert count == 2
    assert coupons[0].coupon_id == "ID123"
    assert coupons[1].coupon_code == "CODE456"


def test_store_coupons_json_parsing():
    client = _FakeClient({"query-store-coupons": _STORE_COUPONS_JSON})
    coupons = store_coupons(client, "1950970")
    assert len(coupons) == 1
    c = coupons[0]
    assert isinstance(c, StoreCoupon)
    assert c.coupon_id == "ID123" and c.coupon_code == "CODE123"
    assert c.product_codes == ["507411"]
    assert c.valid_period.startswith("2026-10-10")


def test_deal_result_ratio_and_render():
    deal = DealResult(
        title="用券吃「美式」", product_names=["美式"],
        coupon=StoreCoupon(coupon_id="ID1", coupon_code="C1", title="9.9元美式"),
        original_price=19.0, discount=9.1, final_price=9.9,
        store=Store(store_code="1950970", store_name="测试餐厅"),
    )
    assert deal.compute_ratio() == 0.4789 or abs(deal.savings_ratio - 0.4789) < 1e-3
    text = deal.render(1)
    # 四项指标全部渲染: 原价/优惠金额/最终价格/节省比例
    assert "原价: ¥19.00" in text
    assert "优惠金额: ¥9.10" in text
    assert "最终价格: ¥9.90" in text
    assert "节省比例" in text


def test_deals_ranked_by_savings_ratio():
    """"最低价组合"排序: 节省比例优先，优惠金额次之。"""
    deals = [
        DealResult(title="a", product_names=["a"], original_price=15,
                   discount=4, final_price=11),
        DealResult(title="b", product_names=["b"], original_price=19,
                   discount=9.1, final_price=9.9),
        DealResult(title="c", product_names=["c"], original_price=29,
                   discount=9.1, final_price=19.9),
    ]
    for d in deals:
        d.compute_ratio()
    ranked = sorted(deals, key=lambda d: (d.savings_ratio, d.discount), reverse=True)
    assert [d.title for d in ranked] == ["b", "c", "a"]


# ---------------------------------------------------------------------------
# 健康模式: 营养筛选与组合上限
# ---------------------------------------------------------------------------

def _health_meals():
    """模拟在售餐品 × 营养表（含高糖/高脂/高钠干扰项）。"""
    meals = [
        MealItem(product_code="W", product_name="薄皮焦香V翅", price=16),
        MealItem(product_code="S", product_name="草莓圣代", price=9),
        MealItem(product_code="F", product_name="双层吉士堡", price=23),
        MealItem(product_code="N", product_name="麦乐鸡", price=14),
    ]
    table = [
        NutritionInfo(name="薄皮焦香V翅", calories=192, protein=17, fat=11, carbohydrate=6, sodium=594),
        NutritionInfo(name="草莓圣代", calories=280, protein=6, fat=8, carbohydrate=45, sodium=80),
        NutritionInfo(name="双层吉士堡", calories=590, protein=30, fat=33, carbohydrate=41, sodium=1150),
        NutritionInfo(name="麦乐鸡", calories=213, protein=12, fat=12, carbohydrate=13, sodium=422),
    ]
    return meals, table


def _lookup(table, name):
    for info in table:
        if info.name == name:
            return info
    return None


def test_filter_low_sugar_drops_high_carb():
    """低糖模式: 碳水 >50g 的餐品被硬过滤（圣代 45g 保留，吉士堡 41g... 均不超）。"""
    meals, table = _health_meals()
    intent = IntentProfile(goal=Goal.LOW_SUGAR)
    got = filter_candidates(meals, table, intent, _lookup)
    names = {m.product_name for m, _ in got}
    assert "草莓圣代" in names                     # 45g 碳水在阈值内
    # 再放一个 60g 碳水的极端项验证硬过滤
    table2 = table + [NutritionInfo(name="可乐大杯", calories=200, protein=0,
                                    fat=0, carbohydrate=60, sodium=20)]
    meals2 = meals + [MealItem(product_code="C", product_name="可乐大杯", price=10)]
    got2 = filter_candidates(meals2, table2, intent, _lookup)
    assert all("可乐大杯" != m.product_name for m, _ in got2)


def test_filter_low_fat_drops_high_fat():
    """低脂模式: 脂肪 >15g 的餐品被硬过滤（吉士堡 33g 剔除）。"""
    meals, table = _health_meals()
    intent = IntentProfile(goal=Goal.LOW_FAT)
    got = filter_candidates(meals, table, intent, _lookup)
    names = {m.product_name for m, _ in got}
    assert "双层吉士堡" not in names
    assert "薄皮焦香V翅" in names                     # 11g 脂肪在阈值内


def test_filter_kids_drops_high_sodium():
    """儿童模式: 单品钠 >600mg 被硬过滤。"""
    meals, table = _health_meals()
    intent = IntentProfile(goal=Goal.KIDS)
    got = filter_candidates(meals, table, intent, _lookup)
    names = {m.product_name for m, _ in got}
    assert "双层吉士堡" not in names                   # 1150mg 钠超标
    assert "麦乐鸡" in names                           # 422mg 达标


def test_build_combos_respect_all_caps():
    """组合生成: 热量/钠/预算上限全部生效（儿童场景组合钠不得超 600mg）。"""
    meals, table = _health_meals()
    intent = IntentProfile(goal=Goal.KIDS, budget=30)
    candidates = filter_candidates(meals, table, intent, _lookup)
    combos = build_combos(candidates, intent)
    assert combos
    for combo in combos:
        by_name = {m.product_name: m for m in combo}
        total_cal = sum(i.calories for i in table if i.name in by_name)
        total_sodium = sum(i.sodium for i in table if i.name in by_name)
        total_price = sum(m.price for m in combo)
        assert total_cal <= 500
        assert total_sodium <= 600
        assert total_price <= 30


def test_score_goal_new_rules():
    """低糖/低脂的目标匹配度评分: 达标 100，超标线性扣分。"""
    sugar_ok = IntentProfile(goal=Goal.LOW_SUGAR)
    assert score_goal(NutritionInfo(name="x", calories=400, carbohydrate=45), sugar_ok) == 100.0
    assert score_goal(NutritionInfo(name="x", calories=400, carbohydrate=60), sugar_ok) < 100

    fat_ok = IntentProfile(goal=Goal.LOW_FAT)
    assert score_goal(NutritionInfo(name="x", calories=400, fat=12), fat_ok) == 100.0
    assert score_goal(NutritionInfo(name="x", calories=400, fat=20), fat_ok) < 100


# ---------------------------------------------------------------------------
# 门店查询模式
# ---------------------------------------------------------------------------

def test_intent_store_query_mode():
    """"附近哪里可以买"类输入进入门店查询模式。"""
    assert recognize_intent("附近哪里可以买").store_query is True
    assert recognize_intent("附近的麦当劳在哪里").store_query is True
    assert recognize_intent("上海最近的麦当劳是哪家").store_query is True
    # 有健康/省钱目标时不分流，仍走推荐链路
    assert recognize_intent("减脂期附近有店吗").store_query is False
    assert recognize_intent("附近哪里买最划算").store_query is False
    # 普通点餐诉求不触发
    assert recognize_intent("帮我推荐一个500卡以内，30元以内的麦当劳套餐").store_query is False


def test_store_recommendation_ranking():
    """门店排序: 营业中优先，同状态按距离升序。"""
    stores = [
        Store(store_code="A", store_name="远但营业", distance=1200,
              business_status=True),
        Store(store_code="B", store_name="近但未营业", distance=100,
              business_status=False, business_start="07:00"),
        Store(store_code="C", store_name="最近且营业", distance=29,
              business_status=True),
    ]
    recs = recommend_stores(stores)
    assert [r.store.store_code for r in recs] == ["C", "A", "B"]
    # 第 1 名（营业中且 ≤800m）理由含"步行即可到达"
    assert "步行即可到达" in recs[0].reason
    # 未营业门店理由含开门时间提示
    assert "未营业" in recs[2].reason and "07:00" in recs[2].reason


def test_store_reason_details():
    """推荐理由附加信息: 打烊早提示 + 预约能力。"""
    store = Store(store_code="X", store_name="早打烊店", distance=500,
                  business_status=True, business_start="07:00",
                  business_end="20:30", support_reservation=True)
    reason = _build_reason(store, rank=0)
    assert "步行即可到达" in reason
    assert "营业至20:30" in reason
    assert "支持预约取餐" in reason


def test_store_render_contains_three_elements():
    """渲染卡片必须含距离/营业状态/推荐理由三要素。"""
    store = Store(store_code="S", store_name="麦当劳测试餐厅", distance=1031,
                  business_status=True, business_start="06:30",
                  business_end="22:30", address="测试路1号")
    rec = recommend_stores([store])[0]
    text = rec.render(1)
    assert "1.0公里" in text            # 距离（米→公里换算）
    assert "营业中" in text              # 营业状态
    assert "推荐理由" in text            # 推荐理由
    assert "06:30-22:30" in text        # 营业时段
