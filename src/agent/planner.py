"""工具规划器（流水线第 2 段）。

根据 IntentProfile 生成 MCP 工具调用计划（波次 DAG）。
纯规则模板生成 —— Planner 永不"发明"工具名（tools.resolve 会拦截），
LLM 仅作为模板未命中时的兜底通道（预留接口，当前未启用）。

调用计划示例（减脂+预算场景）:
    Wave 1（并行）: query-nearby-stores ‖ list-nutrition-foods
    Wave 2（并行）: query-meals ‖ available-coupons ‖ query-promotions
    本地: 交叉筛选 + 组合生成
    Wave 3: calculate-price ×候选组合
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.schemas import IntentProfile
from ..mcp.tools import TOOL_REGISTRY, resolve


@dataclass
class CallNode:
    """DAG 中的一个待执行工具节点。"""

    alias: str                     # 客户端别名（mcd-mcp_*）
    arguments: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)   # 依赖的上游节点别名

    @property
    def real_name(self) -> str:
        """解析为 Server 真实工具名（不存在的别名会在这里抛错）。"""
        return resolve(self.alias)


@dataclass
class CallPlan:
    """一次推荐的完整执行计划: 按波次组织的节点列表。"""

    waves: list[list[CallNode]] = field(default_factory=list)

    def all_nodes(self) -> list[CallNode]:
        return [n for wave in self.waves for n in wave]


def build_plan(intent: IntentProfile) -> CallPlan:
    """意图 → 调用计划（模板法）。

    模板规则:
    - 任何推荐场景都需要: 门店（根依赖）+ 餐品 + 价格
    - 省钱模式（goal=省钱）→ 优惠优化专用 DAG（见 _build_best_value_plan）
    - 有营养目标（max_calories 或 goal ∉ {普通,省钱}）→ 追加营养表
    - 有预算或主动问优惠 → 追加可领券；门店确定后追加门店促销
    - create-order 永不进入推荐计划（由确认门单独触发）
    """
    if intent.goal.value == "省钱":
        return _build_best_value_plan(intent)

    store_alias = TOOL_REGISTRY["mcd-mcp_query-nearby-stores"].alias
    meals_alias = TOOL_REGISTRY["mcd-mcp_query-meals"].alias
    nutrition_alias = TOOL_REGISTRY["mcd-mcp_query-nutrition-foods"].alias
    coupons_alias = TOOL_REGISTRY["mcd-mcp_available-coupons"].alias
    promos_alias = TOOL_REGISTRY["mcd-mcp_query-promotions"].alias
    price_alias = TOOL_REGISTRY["mcd-mcp_calculate-price"].alias

    be_type = {"dine_in": 1, "drive_thru": 5, "delivery": 2}[intent.dining_mode]

    # Wave 1: 无依赖的根节点（门店 + 营养表可并行）
    wave1 = [
        CallNode(store_alias, {"beType": 1 if be_type != 5 else 5, "searchType": 1}),
    ]
    need_nutrition = intent.max_calories is not None or intent.goal.value not in ("普通", "省钱")
    if need_nutrition:
        wave1.append(CallNode(nutrition_alias, {}))

    # Wave 2: 依赖 storeCode 的节点
    wave2 = [
        CallNode(meals_alias, {"orderType": 1 if be_type != 2 else 2, "beType": be_type},
                 depends_on=[store_alias]),
    ]
    need_coupons = intent.budget is not None or intent.goal.value not in ("普通", "省钱")
    if need_coupons:
        wave2.append(CallNode(coupons_alias, {}))
        wave2.append(CallNode(promos_alias, {}, depends_on=[store_alias]))

    # Wave 3: 价格试算（参数在执行期由候选组合填充，此处仅声明节点）
    wave3 = [CallNode(price_alias, {}, depends_on=[meals_alias])]

    return CallPlan(waves=[wave1, wave2, wave3])


def _build_best_value_plan(intent: IntentProfile) -> CallPlan:
    """省钱模式专用 DAG: "今天吃麦当劳怎么最划算"。

    Wave 1（并行）: query-nearby-stores ‖ query-my-coupons
    Wave 2（并行，依赖 storeCode）: query-store-coupons ‖ query-promotions
                                   ‖ query-meals ‖ available-coupons
    Wave 3: calculate-price ×（券×商品）组合批量试算 → 按节省比例排序
    （auto-bind-coupons 仅在"我的券为空"时由执行层触发，不进静态计划）
    """
    store_alias = TOOL_REGISTRY["mcd-mcp_query-nearby-stores"].alias
    my_alias = TOOL_REGISTRY["mcd-mcp_query-my-coupons"].alias
    store_coupons_alias = TOOL_REGISTRY["mcd-mcp_query-store-coupons"].alias
    meals_alias = TOOL_REGISTRY["mcd-mcp_query-meals"].alias
    coupons_alias = TOOL_REGISTRY["mcd-mcp_available-coupons"].alias
    promos_alias = TOOL_REGISTRY["mcd-mcp_query-promotions"].alias
    price_alias = TOOL_REGISTRY["mcd-mcp_calculate-price"].alias

    wave1 = [
        CallNode(store_alias, {"beType": 1, "searchType": 1}),
        CallNode(my_alias, {}),
    ]
    wave2 = [
        CallNode(store_coupons_alias, {"orderType": 1, "beType": 1},
                 depends_on=[store_alias]),
        CallNode(promos_alias, {"orderType": 1, "beType": 1}, depends_on=[store_alias]),
        CallNode(meals_alias, {"orderType": 1, "beType": 1}, depends_on=[store_alias]),
        CallNode(coupons_alias, {}),
    ]
    wave3 = [CallNode(price_alias, {}, depends_on=[store_coupons_alias, meals_alias])]
    return CallPlan(waves=[wave1, wave2, wave3])


def describe_plan(plan: CallPlan) -> str:
    """把计划渲染成可读文本（用于 Demo 展示 Agent 的规划过程）。"""
    lines = []
    for i, wave in enumerate(plan.waves, 1):
        names = " ‖ ".join(n.real_name for n in wave)
        lines.append(f"Wave {i}: {names}")
    return "\n".join(lines)
