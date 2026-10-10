"""食品查询服务（能力①）。

封装 query-meals / query-meal-detail，把官方原始返回裁剪为 MealItem 列表。
"""

from __future__ import annotations

from ..mcp.client import MCPClient, MCPError
from ..models.schemas import MealItem


def query_meals(
    client: MCPClient,
    store_code: str,
    order_type: int = 1,
    be_type: int = 1,
    be_code: str = "",
) -> list[MealItem]:
    """查询指定门店在售餐品。

    实测返回结构（2026-10-10）:
        data.categories: [{name, meals: [{code, tags}]}]   # 分类分组（只有 code）
        data.meals: {code: {name, currentPrice, originalPrice, ...}}  # 详情字典

    两层交叉后产出带名称与现价的 MealItem，按价格升序返回。
    """
    args = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
    if be_code:
        args["beCode"] = be_code

    data = client.call_tool("query-meals", args)
    node = data.get("data", {}) if isinstance(data, dict) else {}
    if not isinstance(node, dict):
        return []

    details: dict = node.get("meals") or {}          # code -> {name, currentPrice}
    # code -> 分类名 反查表
    category_of: dict[str, str] = {}
    for cat in node.get("categories") or []:
        cat_name = str(cat.get("name") or "")
        for meal_ref in cat.get("meals") or []:
            code = str(meal_ref.get("code") or "")
            if code:
                category_of.setdefault(code, cat_name)

    items: list[MealItem] = []
    for code, info in details.items():
        if not isinstance(info, dict):
            continue
        price = _to_float(info.get("currentPrice") or info.get("price"))
        items.append(
            MealItem(
                product_code=str(code),
                product_name=str(info.get("name") or "未知餐品"),
                price=price,
                category=category_of.get(str(code), ""),
            )
        )
    return sorted(items, key=lambda m: m.price)


def _to_float(v) -> float:
    """价格字段容错（官方返回字符串数字）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def query_meal_detail(client: MCPClient, store_code: str, code: str,
                      order_type: int = 1, be_type: int = 1) -> dict:
    """查询餐品详情（含特制选项 modification），供下单定制用。"""
    return client.call_tool(
        "query-meal-detail",
        {"storeCode": store_code, "orderType": order_type,
         "beType": be_type, "code": code},
    )
