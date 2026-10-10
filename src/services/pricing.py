"""价格计算服务（能力④）。

封装 calculate-price。核心职责:
1. 参数组装（storeCode + 商品组合 + 优惠券）；
2. 金额单位换算（官方返回为"分"，统一 ÷100 转元 —— 见 MCP_INTEGRATION.md 规则）;
3. 批量试算入口（推荐引擎对多个候选组合并行试算）。
"""

from __future__ import annotations

from ..mcp.client import MCPClient
from ..models.schemas import MealItem, PriceResult


def calculate_price(
    client: MCPClient,
    store_code: str,
    items: list[MealItem],
    order_type: int = 1,
    be_type: int = 1,
    coupon_id: str = "",
    coupon_code: str = "",
    coupon_product_code: str = "",
    be_code: str = "",
) -> PriceResult:
    """试算一组商品的价格（含优惠）。

    官方 schema 要求 items = [{productCode, quantity, (couponId, couponCode)}]，
    quantity 必须 >= 1。
    券规则（2026-10-10 实测）: couponId 与 couponCode 必须成对传入，否则报 600010；
    coupon_product_code 指定时，券只挂到该商品（组合试算场景），否则挂到全部商品。
    """
    def _entry(it: MealItem) -> dict:
        entry: dict = {"productCode": it.product_code, "quantity": 1}
        use_coupon = coupon_id and (not coupon_product_code
                                     or it.product_code == coupon_product_code)
        if use_coupon:
            entry["couponId"] = coupon_id
            if coupon_code:
                entry["couponCode"] = coupon_code
        return entry

    args: dict = {
        "storeCode": store_code,
        "orderType": order_type,
        "beType": be_type,
        "items": [_entry(it) for it in items],
    }
    if be_code:
        args["beCode"] = be_code

    data = client.call_tool("calculate-price", args)
    return _parse_price(data)


def _parse_price(data) -> PriceResult:
    """解析试算返回。金额单位为分 → 元；字段缺失时做保守兜底。"""
    node = data.get("data", data) if isinstance(data, dict) else {}

    def fen(key: str) -> float:
        v = node.get(key, 0) or 0
        return round(int(v) / 100, 2)          # 分 → 元

    original = fen("originalPrice") or fen("productOriginalPrice")
    final = fen("price") or fen("productPrice")
    discount = fen("discount")

    # 兜底: 若接口没给 original，用 final+discount 推算
    if not original:
        original = round(final + discount, 2)
    # 兜底: 若接口没给 final，退化为 original
    if not final:
        final = original

    return PriceResult(original_price=original, final_price=final, discount=discount)
