"""营养分析服务（能力②）。

封装 list-nutrition-foods（注意: 官方真名，无 query- 前缀，见 tools.py 勘误）。

实测数据格式（2026-10-10）: data 为字符串化紧凑表格:

    "[160]{productName,nutritionDescription,energyKj,energyKcal,protein,
      fat,carbohydrate,sodium,calcium}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
      ..."

即: [行数]{字段表}: + 每行一个 CSV 行。无参数、全量返回 → 本地缓存。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..mcp.client import MCPClient, MCPError
from ..models.schemas import NutritionInfo

_CACHE_FILE = Path(__file__).resolve().parents[2] / "output" / "nutrition_cache.json"

# 会话内缓存（进程级）
_memory_cache: list[NutritionInfo] | None = None

# 紧凑表格字段名 -> NutritionInfo 字段映射
_FIELD_MAP = {
    "productName": "name",
    "energyKcal": "calories",
    "protein": "protein",
    "fat": "fat",
    "carbohydrate": "carbohydrate",
    "sodium": "sodium",
}


def _parse_table(text: str) -> list[NutritionInfo]:
    """解析官方紧凑表格格式: [N]{f1,f2,...}:
    行: 值1,值2,...（与字段表按序对应）
    """
    m = re.search(r"\{([^{}]+)\}\s*:", text)
    if not m:
        return []
    fields = [f.strip() for f in m.group(1).split(",")]
    # 表体: 字段声明行之后的所有非空行
    body = text[m.end():]

    infos: list[NutritionInfo] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        values = [v.strip() for v in line.split(",")]
        if len(values) < len(fields):
            continue                          # 字段数不齐的脏行跳过
        row = dict(zip(fields, values))
        kwargs: dict = {}
        for src, dst in _FIELD_MAP.items():
            if dst == "name":                   # 名称字段直接取字符串
                kwargs["name"] = row.get(src, "")
            else:                               # 数值字段容错转换
                kwargs[dst] = _num(row.get(src))
        if kwargs.get("name"):
            infos.append(NutritionInfo(**kwargs))
    return infos


def _num(v) -> float:
    """数值容错: 'null'/空/非数字 → 0.0。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_items(data) -> list[NutritionInfo]:
    """把 call_tool 返回统一转为 NutritionInfo 列表。

    兼容两种形态: ① dict.data 为紧凑表格字符串（当前实测形态）
                 ② 已经是 list[dict]（官方未来若改为结构化 JSON）
    """
    if isinstance(data, dict):
        node = data.get("data", data)
        if isinstance(node, str):
            return _parse_table(node)
        data = node
    if isinstance(data, str):
        return _parse_table(data)
    if not isinstance(data, list):
        return []
    infos = []
    for raw in data:
        if isinstance(raw, dict) and raw.get("productName"):
            infos.append(NutritionInfo(
                name=str(raw.get("productName")),
                calories=_num(raw.get("energyKcal")),
                protein=_num(raw.get("protein")),
                fat=_num(raw.get("fat")),
                carbohydrate=_num(raw.get("carbohydrate")),
                sodium=_num(raw.get("sodium")),
            ))
    return infos


def get_nutrition(client: MCPClient, use_cache: bool = True) -> list[NutritionInfo]:
    """获取全量餐品营养数据（带三级缓存: 内存 → 磁盘 → MCP）。

    降级策略（见 agent-workflow.md）: MCP 失败时回退磁盘缓存并在结果侧标注。
    """
    global _memory_cache
    if use_cache and _memory_cache is not None:
        return _memory_cache

    # 磁盘缓存命中 → 不打远程
    if use_cache and _CACHE_FILE.exists():
        try:
            cached = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            infos = [NutritionInfo(**r) for r in cached]
            _memory_cache = infos
            return infos
        except (json.JSONDecodeError, TypeError):
            pass  # 缓存损坏则重新拉取

    try:
        data = client.call_tool("list-nutrition-foods", {})
        infos = _parse_items(data)
        if not infos:
            raise MCPError("营养数据解析为空")
    except MCPError:
        # 降级: 只剩磁盘缓存这一条路，失败则向上抛
        if _CACHE_FILE.exists():
            cached = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            infos = [NutritionInfo(**r) for r in cached]
        else:
            raise

    # 写缓存（内存 + 磁盘），供降级与下次会话使用
    _memory_cache = infos
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(
        json.dumps([i.model_dump() for i in infos], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return infos


def find_nutrition(all_nutrition: list[NutritionInfo], name: str) -> NutritionInfo | None:
    """按餐品名模糊匹配营养数据（推荐引擎用: 在售餐品 × 营养表交叉）。

    匹配策略: 精确 → 双向包含 → 去装饰符包含，逐级放宽。
    """
    name_clean = name.replace(" ", "")
    for info in all_nutrition:
        if info.name == name_clean:
            return info
    for info in all_nutrition:
        a, b = info.name.replace(" ", ""), name_clean
        if a in b or b in a:
            return info
    # 最后一级: 去掉括号装饰后再试（"麦辣鸡腿堡(中)" → "麦辣鸡腿堡"）
    stripped = name_clean.split("(")[0]
    if stripped and stripped != name_clean:
        for info in all_nutrition:
            if info.name in stripped or stripped in info.name:
                return info
    return None
