"""通用工具函数（金额换算、餐品名清洗等）。"""

from __future__ import annotations


def fen_to_yuan(fen) -> float:
    """分 → 元。官方金额单位为分（见 MCP_INTEGRATION.md 规则）。"""
    return round(int(fen or 0) / 100, 2)


def yuan_to_fen(yuan: float) -> int:
    """元 → 分（构造下单/试算参数时使用）。"""
    return int(round(yuan * 100))


def clean_name(name: str) -> str:
    """餐品名清洗: 去空格与常见装饰符，提高营养表交叉匹配命中率。"""
    return name.replace(" ", "").replace("（", "(").split("(")[0].strip()


def format_money(value: float) -> str:
    """金额展示统一格式: ¥12.50。"""
    return f"¥{value:.2f}"
