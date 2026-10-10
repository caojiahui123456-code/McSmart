"""McSmart 数据模型层。

所有跨模块传递的数据结构统一定义在此（Pydantic v2），
保证 MCP 响应、意图识别结果、推荐结果都有强类型约束。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 意图识别（IntentRecognizer 的输出）
# ---------------------------------------------------------------------------

class Goal(str, Enum):
    """用户点餐目标分类。

    健康模式五目标: 减脂 / 增肌 / 儿童 / 低糖 / 低脂
    （低糖说明: 官方营养数据无"糖"字段，以碳水 carbohydrate 作代理指标）
    """

    FAT_LOSS = "减脂"        # 减肥 / 减脂 / 轻食
    MUSCLE_GAIN = "增肌"     # 增肌 / 健身 / 高蛋白
    KIDS = "儿童"            # 儿童 / 孩子 / 小朋友
    LOW_SUGAR = "低糖"       # 低糖 / 控糖 / 无糖（碳水代理）
    LOW_FAT = "低脂"         # 低脂 / 少油 / 清淡
    BEST_VALUE = "省钱"      # 最划算 / 省钱 / 薅羊毛（优惠优化模式）
    NORMAL = "普通"          # 无特殊目标

    @classmethod
    def health_goals(cls) -> list["Goal"]:
        """健康模式目标集（触发营养表拉取与营养筛选的目标）。"""
        return [cls.FAT_LOSS, cls.MUSCLE_GAIN, cls.KIDS,
                cls.LOW_SUGAR, cls.LOW_FAT]


class IntentProfile(BaseModel):
    """从用户自然语言中抽取的结构化意图（对应需求示例中的输出）。"""

    goal: Goal = Goal.NORMAL
    max_calories: Optional[int] = None      # 单餐热量上限 kcal，如 500
    budget: Optional[float] = None          # 预算上限，单位: 元，如 30
    dining_mode: str = "dine_in"            # dine_in / drive_thru / delivery
    city: str = "北京"                      # 门店搜索城市（searchType=2 必需）
    city_inferred: bool = True              # True=用户未提城市，用的是默认城市（须显式告知）
    store_query: bool = False               # 门店查询模式（"附近哪里可以买"）
    missing_slots: list[str] = Field(default_factory=list)

    def as_demo_json(self) -> dict:
        """按比赛需求示例的字段风格输出（goal/maxCalories/budget）。"""
        return {
            "goal": self.goal.value,
            "maxCalories": self.max_calories,
            "budget": self.budget,
            "storeQuery": self.store_query,
        }


# ---------------------------------------------------------------------------
# MCP 工具返回的领域对象
# ---------------------------------------------------------------------------

class Store(BaseModel):
    """可点餐门店（query-nearby-stores 返回项的简化视图）。

    实测字段（2026-10-10）: distance 单位为米; businessStatus 为营业布尔;
    businessStartTime/businessEndTime 为 "HH:MM" 营业时段。
    """

    store_code: str
    store_name: str
    be_code: str = ""
    distance: Optional[int] = None          # 距离，米；无定位信息时为 None
    address: str = ""
    business_status: bool = True            # 当前是否营业
    business_start: str = ""                # "07:00"
    business_end: str = ""                  # "22:00"
    support_reservation: bool = False       # 支持预约取餐


class StoreRecommendation(BaseModel):
    """门店推荐卡片（门店查询模式输出项）: 门店 + 距离 + 营业状态 + 推荐理由。"""

    store: Store
    reason: str                             # 推荐理由（模板拼接，非 LLM）

    def render(self, rank: int) -> str:
        """渲染为文本卡片。"""
        s = self.store
        lines = [f"【门店 {rank}】{s.store_name}"]
        if s.distance is not None:
            lines.append(f"  距离: {_fmt_distance(s.distance)}")
        status = "营业中 ✅" if s.business_status else "未营业 ❌"
        hours = f"（{s.business_start}-{s.business_end}）" if s.business_end else ""
        lines.append(f"  营业状态: {status}{hours}")
        if s.address:
            lines.append(f"  地址: {s.address}")
        lines.append(f"  推荐理由: {self.reason}")
        return "\n".join(lines)


def _fmt_distance(meters: int) -> str:
    """米 → 人类可读距离（<1km 用米，≥1km 用公里保留 1 位小数）。"""
    if meters < 1000:
        return f"{meters}米"
    return f"{meters / 1000:.1f}公里"


class MealItem(BaseModel):
    """在售餐品（query-meals 返回项的简化视图）。"""

    product_code: str
    product_name: str
    price: float = 0.0          # 元
    category: str = ""


class NutritionInfo(BaseModel):
    """餐品营养成分（list-nutrition-foods 返回项的简化视图）。"""

    name: str
    calories: float = 0.0       # kcal
    protein: float = 0.0        # g
    fat: float = 0.0            # g
    carbohydrate: float = 0.0   # g
    sodium: float = 0.0         # mg


class CouponInfo(BaseModel):
    """优惠券（available-coupons / query-store-coupons 返回项的简化视图）。"""

    coupon_id: str
    coupon_code: str = ""
    title: str
    min_spend: float = 0.0      # 使用门槛，元；0 表示无门槛
    discount: float = 0.0       # 抵扣金额，元
    product_codes: list[str] = Field(default_factory=list)  # 券适用商品（store-coupons 提供）


class StoreCoupon(CouponInfo):
    """query-store-coupons 返回的门店可用券（带 couponId/couponCode/适用商品）。

    这是唯一能同时拿到券标识与适用商品productCode 的数据源，
    是 calculate-price 券后试算的输入。
    """

    valid_period: str = ""
    promotion_id: str = ""


class PriceResult(BaseModel):
    """calculate-price 返回的试算结果（金额已由分转元）。"""

    original_price: float       # 原价，元
    final_price: float          # 现价（含优惠），元
    discount: float = 0.0       # 优惠金额，元


# ---------------------------------------------------------------------------
# 推荐结果
# ---------------------------------------------------------------------------

class ScoreBreakdown(BaseModel):
    """推荐分拆解: 推荐分 = 营养匹配度 + 价格优惠度 + 用户目标匹配度。"""

    nutrition_match: float = Field(ge=0, le=100, description="营养匹配度 0-100")
    price_saving: float = Field(ge=0, le=100, description="价格优惠度 0-100")
    goal_match: float = Field(ge=0, le=100, description="用户目标匹配度 0-100")
    total: float = 0.0          # 加权总分

    def compute_total(self, w_nutrition=0.4, w_price=0.3, w_goal=0.3) -> float:
        """按权重计算总分并写回 total。

        权重默认 0.4/0.3/0.3（营养优先，符合 McSmart 健康点餐定位）。
        """
        self.total = round(
            self.nutrition_match * w_nutrition
            + self.price_saving * w_price
            + self.goal_match * w_goal,
            2,
        )
        return self.total


class Recommendation(BaseModel):
    """一套推荐方案（推荐引擎的最终产物）。"""

    combo_name: str                       # 方案名，如"高蛋白轻食组合"
    items: list[MealItem]                 # 组合内餐品
    nutrition: NutritionInfo              # 组合合计营养
    price: PriceResult                    # 券后价
    coupons_used: list[CouponInfo] = Field(default_factory=list)
    store: Optional[Store] = None
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    notes: list[str] = Field(default_factory=list)   # 降级标注等信息

    def render(self, index: int) -> str:
        """渲染为给用户看的文本卡片（数值全部来自工具返回，无 LLM 参与）。"""
        lines = [
            f"【麦智方案 {index}】{self.combo_name}  ·  综合推荐分 {self.score.total}",
            f"  组合: {' + '.join(i.product_name for i in self.items)}",
            f"  热量 {self.nutrition.calories:.0f}kcal | 蛋白质 {self.nutrition.protein:.1f}g"
            f" | 脂肪 {self.nutrition.fat:.1f}g | 碳水 {self.nutrition.carbohydrate:.1f}g"
            f" | 钠 {self.nutrition.sodium:.0f}mg",
            f"  价格: ¥{self.price.final_price:.2f}"
            f"（原价 ¥{self.price.original_price:.2f}，省 ¥{self.price.discount:.2f}）",
        ]
        if self.coupons_used:
            shown = ", ".join(c.title for c in self.coupons_used[:2])
            extra = f" 等{len(self.coupons_used)}张" if len(self.coupons_used) > 2 else ""
            lines.append(f"  可领优惠: {shown}{extra}")
        if self.store:
            lines.append(f"  门店: {self.store.store_name}")
        for note in self.notes:
            lines.append(f"  ⚠ {note}")
        return "\n".join(lines)


class DealResult(BaseModel):
    """一个"最划算"候选方案（deal_optimizer 的产物）。

    四项核心指标全部来自 calculate-price 实测返回（报价零幻觉）:
    原价 / 优惠金额 / 最终价格 / 节省比例。
    """

    title: str                            # 方案名（券名或组合名）
    product_names: list[str]              # 商品清单
    coupon: Optional[StoreCoupon] = None  # 使用的券（None = 平台价直降）
    original_price: float                 # 原价，元
    discount: float                       # 优惠金额，元
    final_price: float                    # 最终价格，元
    savings_ratio: float = 0.0            # 节省比例 0-1（discount / original_price）
    store: Optional[Store] = None
    notes: list[str] = Field(default_factory=list)

    def compute_ratio(self) -> float:
        """计算节省比例（原价为 0 时保守记 0）。"""
        self.savings_ratio = (
            round(self.discount / self.original_price, 4) if self.original_price else 0.0
        )
        return self.savings_ratio

    def render(self, index: int) -> str:
        """渲染为用户可读的省钱方案卡片（数值全部来自工具返回）。"""
        lines = [
            f"【省钱方案 {index}】{self.title}  ·  节省比例 {self.savings_ratio:.1%}",
            f"  商品: {' + '.join(self.product_names)}",
            f"  原价: ¥{self.original_price:.2f}"
            f" | 优惠金额: ¥{self.discount:.2f}"
            f" | 最终价格: ¥{self.final_price:.2f}",
        ]
        if self.coupon:
            lines.append(f"  用券: {self.coupon.title}"
                         + (f"（{self.coupon.valid_period}）" if self.coupon.valid_period else ""))
        if self.store:
            lines.append(f"  门店: {self.store.store_name}")
        for note in self.notes:
            lines.append(f"  ⚠ {note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 会话状态机（见 docs/agent-workflow.md 第 4 节）
# ---------------------------------------------------------------------------

class SessionState(str, Enum):
    """会话状态枚举，迁移规则由 orchestrator 强制。"""

    IDLE = "idle"
    INTENT_PARSING = "intent_parsing"
    CLARIFYING = "clarifying"
    PLANNING = "planning"
    EXECUTING = "executing"
    AGGREGATING = "aggregating"
    RECOMMENDATION_READY = "recommendation_ready"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ORDERING = "ordering"
    ORDER_CREATED = "order_created"
    ERROR = "error"
