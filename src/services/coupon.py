"""优惠发现服务（能力③）。

封装的工具（2026-10-10 实测返回形态）:
- available-coupons: 返回 **Markdown 文本**（"优惠券标题：X / 状态：可领取"），
  无 couponId → 仅作展示与提醒，不能用于 calculate-price 试算；
- query-my-coupons: 返回 **Markdown 文本**（券名 + 用券价 + 有效期），
  无 couponId → 用于展示"我的券"概览；
- query-store-coupons: 返回 **JSON**（title/couponId/couponCode/products[]），
  是唯一能拿到券标识 + 适用商品 productCode 的数据源 → 券后试算的输入；
- auto-bind-coupons: 一键领取全部可领券，返回 Markdown（含 couponId），
  仅在"我的券为空"时由 deal_optimizer 自动触发（非高危但变更账户状态）；
- query-promotions: 返回 JSON list（promotionId, beTypes, products...），
  beTypes 限定适用场景（实测多为 '6' 团餐），需按当前 beType 过滤。

create-order 属高危动作，不在本服务暴露，须走确认门。
"""

from __future__ import annotations

import re

from ..mcp.client import MCPClient
from ..models.schemas import CouponInfo, StoreCoupon


def available_coupons(client: MCPClient) -> list[CouponInfo]:
    """解析"麦麦省优惠券"Markdown 文本。

    形态: "- 优惠券标题：9.9元中杯冰美式 \\\n  状态：可领取 \\\n ..."
    标题中的"N元"视作券价值（如 9.9 元买中杯冰美式）。
    """
    text = client.call_tool("available-coupons", {})
    if not isinstance(text, str):
        # 官方未来若改为 JSON，走通用解析
        return _parse_coupons_json(text)

    coupons: list[CouponInfo] = []
    for m in re.finditer(r"优惠券标题[：:]\s*(.+?)\s*\\", text):
        title = m.group(1).strip()
        # 从标题抽券价值: "9.9元中杯冰美式" → 9.9
        value_m = re.search(r"(\d+(?:\.\d+)?)\s*元", title)
        coupons.append(
            CouponInfo(
                coupon_id="",                   # 官方文本未提供 couponId
                title=title,
                discount=float(value_m.group(1)) if value_m else 0.0,
            )
        )
    return coupons


def my_coupons(client: MCPClient) -> list[CouponInfo]:
    """查询"我的优惠券"，解析 Markdown 文本。

    形态: "## 9.9元中杯冰美式\\n- **优惠**: ¥9.9 (用券价格)\\n- **有效期**: ..."
    官方文本未提供 couponId → 仅作概览展示（试算走 store_coupons）。
    """
    text = client.call_tool("query-my-coupons", {})
    if not isinstance(text, str):
        return _parse_coupons_json(text)

    coupons: list[CouponInfo] = []
    for m in re.finditer(r"##\s*(.+?)\n(.*?)(?=\n## |\Z)", text, re.S):
        title = m.group(1).strip()
        block = m.group(2)
        value_m = re.search(r"¥\s*(\d+(?:\.\d+)?)", block)
        coupons.append(
            CouponInfo(
                coupon_id="",
                title=title,
                discount=float(value_m.group(1)) if value_m else 0.0,
            )
        )
    return coupons


def auto_bind(client: MCPClient) -> tuple[int, list[CouponInfo]]:
    """一键领取全部可领券（auto-bind-coupons）。

    返回 (成功张数, 券列表)。重复调用时官方返回"暂无可领取"→ 计 0。
    注意: 该动作会变更账户状态，多轮对话形态下应先征求用户确认；
    CLI Demo 中仅在"我的券为空"时自动触发并打印提示。
    """
    text = client.call_tool("auto-bind-coupons", {})
    if not isinstance(text, str):
        return 0, []
    count_m = re.search(r"成功[：:]\s*(\d+)\s*张", text)
    coupons: list[CouponInfo] = []
    for m in re.finditer(
        r"-\s*(.+?)\n\s*-\s*couponId[：:]\s*(\S+)\n\s*-\s*couponCode[：:]\s*(\S+)",
        text,
    ):
        coupons.append(CouponInfo(
            coupon_id=m.group(2), coupon_code=m.group(3), title=m.group(1).strip(),
        ))
    return (int(count_m.group(1)) if count_m else 0), coupons


def store_coupons(
    client: MCPClient,
    store_code: str,
    order_type: int = 1,
    be_type: int = 1,
) -> list[StoreCoupon]:
    """查询指定门店+订单类型下可用的券（query-store-coupons，JSON 返回）。

    关键字段: couponId + couponCode（calculate-price 券试算必须成对传入）
    + products[].productCode（券适用商品，用于匹配在售餐品）。
    """
    data = client.call_tool(
        "query-store-coupons",
        {"storeCode": store_code, "orderType": order_type, "beType": be_type},
    )
    node = data.get("data", []) if isinstance(data, dict) else data
    if not isinstance(node, list):
        return []

    coupons: list[StoreCoupon] = []
    for raw in node:
        if not isinstance(raw, dict):
            continue
        products = raw.get("products") or []
        coupons.append(StoreCoupon(
            coupon_id=str(raw.get("couponId") or ""),
            coupon_code=str(raw.get("couponCode") or ""),
            title=str(raw.get("title") or "优惠券"),
            product_codes=[str(p.get("productCode") or "") for p in products
                           if isinstance(p, dict) and p.get("productCode")],
            valid_period=str(raw.get("tradeDateTime") or ""),
            promotion_id=str(raw.get("promotionId") or ""),
        ))
    # 无标识的券无法参与试算，但仍保留展示（coupon_id 为空会被上层过滤试算）
    return coupons


def query_promotions(
    client: MCPClient,
    store_code: str,
    order_type: int = 1,
    be_type: int = 1,
) -> list[CouponInfo]:
    """查询指定门店促销规则，按当前 beType 过滤（如 beTypes=['6'] 仅团餐可用）。"""
    data = client.call_tool(
        "query-promotions",
        {"storeCode": store_code, "orderType": order_type, "beType": be_type},
    )
    node = data.get("data", []) if isinstance(data, dict) else data
    if not isinstance(node, list):
        return []

    promotions: list[CouponInfo] = []
    for raw in node:
        if not isinstance(raw, dict):
            continue
        # 适用场景过滤: beTypes 不含当前 beType 的促销对本次点餐无效
        be_types = [str(b) for b in raw.get("beTypes") or []]
        if be_types and str(be_type) not in be_types:
            continue
        promo_id = str(raw.get("promotionId") or raw.get("id") or "")
        promotions.append(
            CouponInfo(
                coupon_id=promo_id,
                title=str(raw.get("title") or raw.get("name")
                         or raw.get("ruleName") or f"门店促销 {promo_id}"),
                # 满减金额字段实测未见，具体折扣以 calculate-price 试算为准
                discount=_parse_discount_from_title(str(raw.get("title") or "")),
            )
        )
    return promotions


# ---------------------------------------------------------------------------
# 内部: JSON 形态兜底解析（官方若切换返回格式时启用）
# ---------------------------------------------------------------------------

def _parse_coupons_json(data) -> list[CouponInfo]:
    if isinstance(data, dict):
        node = data.get("data", data)
        items = node if isinstance(node, list) else node.get("list", [])
    elif isinstance(data, list):
        items = data
    else:
        return []
    coupons = []
    for raw in items:
        if isinstance(raw, dict):
            coupons.append(CouponInfo(
                coupon_id=str(raw.get("couponId") or raw.get("promotionId") or ""),
                coupon_code=str(raw.get("couponCode") or ""),
                title=str(raw.get("title") or raw.get("couponName") or "优惠券"),
            ))
    return coupons


def _parse_discount_from_title(title: str) -> float:
    """从促销标题中抽取优惠金额（如"满39减8"→8.0）。"""
    m = re.search(r"减\s*(\d+(?:\.\d+)?)", title)
    return float(m.group(1)) if m else 0.0
