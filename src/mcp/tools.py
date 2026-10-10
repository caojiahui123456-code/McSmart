"""MCP 工具注册表与别名映射。

约定（见 MCP_INTEGRATION.md）:
- 文档/比赛材料中使用 `mcd-mcp_*` 客户端别名，风格统一；
- Server 端真实工具名以 tools/list 实测为准，个别不一致处（如营养工具）
  在 ALIAS_MAP 中显式映射，避免评委对接踩坑。
"""

from __future__ import annotations

from dataclasses import dataclass

# 客户端别名 -> Server 真实工具名
# ⚠️ query-nutrition-foods 在官方 35 个工具中不存在，真名为 list-nutrition-foods
ALIAS_MAP: dict[str, str] = {
    "mcd-mcp_query-meals": "query-meals",
    "mcd-mcp_query-nutrition-foods": "list-nutrition-foods",
    "mcd-mcp_available-coupons": "available-coupons",
    "mcd-mcp_query-my-coupons": "query-my-coupons",
    "mcd-mcp_query-store-coupons": "query-store-coupons",
    "mcd-mcp_auto-bind-coupons": "auto-bind-coupons",
    "mcd-mcp_query-promotions": "query-promotions",
    "mcd-mcp_calculate-price": "calculate-price",
    "mcd-mcp_query-nearby-stores": "query-nearby-stores",
    "mcd-mcp_create-order": "create-order",
}


@dataclass(frozen=True)
class ToolSpec:
    """一个受管工具的元信息: 别名、真名、能力域、是否高危。"""

    alias: str
    real_name: str
    capability: str          # food / nutrition / coupon / pricing / store / order
    high_risk: bool = False  # 高危工具必须通过确认门才允许调用


# McSmart 主链路受管工具（推荐 7 个 + 优惠优化 3 个）
TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.alias: spec
    for spec in [
        ToolSpec("mcd-mcp_query-meals", "query-meals", "food"),
        ToolSpec("mcd-mcp_query-nutrition-foods", "list-nutrition-foods", "nutrition"),
        ToolSpec("mcd-mcp_available-coupons", "available-coupons", "coupon"),
        ToolSpec("mcd-mcp_query-my-coupons", "query-my-coupons", "coupon"),
        ToolSpec("mcd-mcp_query-store-coupons", "query-store-coupons", "coupon"),
        ToolSpec("mcd-mcp_auto-bind-coupons", "auto-bind-coupons", "coupon"),
        ToolSpec("mcd-mcp_query-promotions", "query-promotions", "coupon"),
        ToolSpec("mcd-mcp_calculate-price", "calculate-price", "pricing"),
        ToolSpec("mcd-mcp_query-nearby-stores", "query-nearby-stores", "store"),
        ToolSpec("mcd-mcp_create-order", "create-order", "order", high_risk=True),
    ]
}


def resolve(alias: str) -> str:
    """把客户端别名解析为 Server 真实工具名。

    - 已注册别名: 走 ALIAS_MAP
    - 直接给了真实工具名: 原样放行（宽容调用）
    - 都不存在: 抛 KeyError，Planner 禁止发明工具名的防线也在此
    """
    if alias in ALIAS_MAP:
        return ALIAS_MAP[alias]
    known_real = {s.real_name for s in TOOL_REGISTRY.values()}
    if alias in known_real:
        return alias
    raise KeyError(f"未注册的 MCP 工具: {alias}（禁止发明工具名）")


def is_high_risk(alias: str) -> bool:
    """判断工具是否高危（下单/抽奖/领券类需要用户显式确认）。"""
    spec = TOOL_REGISTRY.get(alias)
    return bool(spec and spec.high_risk)
