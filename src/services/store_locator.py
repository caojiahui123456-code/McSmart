"""门店推荐服务（能力⑤）。

封装 query-nearby-stores（到店/得来速）。返回的 storeCode 是整条点餐
链路的根依赖（餐品/促销/价格/下单都依赖它）。

实测结论（2026-10-10）:
- searchType=1（收藏餐厅）: 该测试账号收藏为空，返回 600050
- searchType=2（按城市搜索）: 必须传 city + keyword，否则返回 600058
- 返回项含 distance（米）/ businessStatus / 营业时段 / 地址 / 预约能力，
  且官方已按距离升序返回
因此默认走 searchType=2 + 城市/关键词兜底策略。
"""

from __future__ import annotations

import os

from ..mcp.client import MCPClient
from ..models.schemas import Store, StoreRecommendation

# 兜底默认值（用户未提供位置信息时的默认城市，可用 MCSMART_DEFAULT_CITY 覆盖）
DEFAULT_CITY = os.getenv("MCSMART_DEFAULT_CITY", "北京").strip() or "北京"
DEFAULT_KEYWORD = "麦当劳"

# 门店查询模式的推荐上限
MAX_STORE_RECOMMENDATIONS = 3


def query_nearby_stores(
    client: MCPClient,
    be_type: int = 1,
    city: str = DEFAULT_CITY,
    keyword: str = DEFAULT_KEYWORD,
) -> list[Store]:
    """查询可点餐门店。

    官方 schema: beType 1=到店自取 5=得来速; searchType 2=按城市搜索。
    优先级: 用户提供的城市 > 默认城市（见 orchestrator 的 city 槽位）。
    """
    data = client.call_tool(
        "query-nearby-stores",
        {"beType": be_type, "searchType": 2, "city": city, "keyword": keyword},
    )
    return _parse_stores(data)


def pick_primary_store(stores: list[Store]) -> Store | None:
    """选择主门店: 列表中第一家（官方默认按距离排序）。"""
    return stores[0] if stores else None


def recommend_stores(
    stores: list[Store],
    limit: int = MAX_STORE_RECOMMENDATIONS,
) -> list[StoreRecommendation]:
    """生成门店推荐列表（门店查询模式核心）。

    排序规则:
    1. 营业中优先（未营业门店排后，但仍展示供参考）
    2. 同状态下按距离升序（官方顺序即距离序，此处显式再排保证确定性）

    推荐理由由规则模板拼接（非 LLM）: 距离档位 + 营业状态 + 预约能力。
    """
    ranked = sorted(
        stores,
        key=lambda s: (not s.business_status, s.distance if s.distance is not None else 10**9),
    )
    results: list[StoreRecommendation] = []
    for idx, store in enumerate(ranked[:limit]):
        results.append(StoreRecommendation(store=store, reason=_build_reason(store, idx)))
    return results


def _build_reason(store: Store, rank: int) -> str:
    """按规则模板生成推荐理由（确定性，可测试）。

    规则优先级:
    - 未营业 → 提示开门时间，提醒暂不可点
    - 营业中:
        - 第 1 名 + 步行可达（≤800m）→ "距离最近，步行即可到达"
        - 第 1 名 → "距离最近的门店"
        - 其他 → "距离第 N 近"
    - 附加: 营业时段（打烊早的门店提示"营业至X点"）
    - 附加: 支持预约取餐
    """
    parts: list[str] = []

    if not store.business_status:
        open_hint = f"{store.business_start}开门" if store.business_start else "明日"
        parts.append(f"当前未营业（{open_hint}），适合提前规划")
    elif rank == 0:
        if store.distance is not None and store.distance <= 800:
            parts.append("距离最近，步行即可到达")
        else:
            parts.append("距离最近的门店")
    else:
        parts.append(f"距离第 {rank + 1} 近的备选")

    # 打烊提示: 营业结束时间不晚于 21:00 的给出提示（晚归用户有用）
    if store.business_status and store.business_end and store.business_end <= "21:00":
        parts.append(f"营业至{store.business_end}，打烊较早")

    if store.support_reservation:
        parts.append("支持预约取餐")

    return "，".join(parts)


def _parse_stores(data) -> list[Store]:
    """裁剪官方返回: {success, code, data: [{storeCode, storeName, beCode, distance,
    businessStatus, businessStartTime, businessEndTime, address, reservation, ...}]}。
    """
    if not isinstance(data, dict):
        return []
    items = data.get("data") or []
    stores: list[Store] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        code = raw.get("storeCode")
        if not code:
            continue
        distance = raw.get("distance")
        stores.append(
            Store(
                store_code=str(code),
                store_name=str(raw.get("storeName") or "麦当劳门店"),
                be_code=str(raw.get("beCode") or ""),
                distance=int(distance) if isinstance(distance, (int, float)) else None,
                address=str(raw.get("address") or ""),
                business_status=bool(raw.get("businessStatus", True)),
                business_start=str(raw.get("businessStartTime") or ""),
                business_end=str(raw.get("businessEndTime") or ""),
                support_reservation=bool(raw.get("reservation", False)),
            )
        )
    return stores
