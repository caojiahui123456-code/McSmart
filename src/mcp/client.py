"""MCP 客户端封装。

统一处理: 凭证加载(.env)、Streamable HTTP JSON-RPC、超时、重试、工具发现。
业务层只调用 `call_tool()`，不接触任何传输细节。

实测结论（2026-10-10）:
- Server: mcd-mcp v1.0.0 @ https://mcp.mcd.cn
- Streamable HTTP 无状态会话: 每次 POST 独立，无需维护 Mcp-Session-Id
- 金额单位: 分（由调用方换算为元）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

# .env 位于项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MCPError(RuntimeError):
    """MCP 调用异常，携带 trace 上下文便于排障。"""


def load_env() -> tuple[str, str]:
    """从项目根 .env 读取 MCP_SERVER_URL 与 MCP_AUTH_TOKEN。

    刻意不引入 python-dotenv 依赖——两行解析足够，减少供应链面。
    """
    env_path = _PROJECT_ROOT / ".env"
    config: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip()

    url = os.environ.get("MCP_SERVER_URL") or config.get("MCP_SERVER_URL")
    token = os.environ.get("MCP_AUTH_TOKEN") or config.get("MCP_AUTH_TOKEN")
    if not url or not token:
        raise MCPError("缺少 MCP_SERVER_URL / MCP_AUTH_TOKEN（检查 McSmart/.env）")
    return url, token


class MCPClient:
    """麦当劳 MCP Server 的最小可用客户端。"""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout: float = 8.0,
        max_retries: int = 1,
    ) -> None:
        self.url, self.token = (url, token) if url and token else load_env()
        self.timeout = timeout
        self.max_retries = max_retries
        self._request_id = 0
        self._tools: dict[str, dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # 底层: 单次 JSON-RPC POST
    # ------------------------------------------------------------------

    def _post(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送一个 JSON-RPC 请求并返回 result 字段（失败抛 MCPError）。"""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }

        last_error: Exception | None = None
        for attempt in range(1 + self.max_retries):     # 首次 + 重试
            try:
                resp = httpx.post(
                    self.url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = self._parse_response(resp)
                if "error" in data:
                    raise MCPError(f"服务端错误: {data['error']}")
                return data.get("result", {})
            except (httpx.HTTPError, MCPError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(0.5)     # 重试前短暂退避
        raise MCPError(f"MCP 调用失败 [{method}]: {last_error}")

    @staticmethod
    def _parse_response(resp: httpx.Response) -> dict[str, Any]:
        """兼容 application/json 与 text/event-stream 两种返回格式。"""
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            # SSE: 逐行找 data: 开头的 JSON
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    body = line[5:].strip()
                    if body:
                        return json.loads(body)
            raise MCPError("SSE 响应中无 data 帧")
        return resp.json()

    # ------------------------------------------------------------------
    # 协议: initialize / tools/list / tools/call
    # ------------------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        """MCP 握手，返回 serverInfo（用于健康检查）。"""
        return self._post(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mcsmart", "version": "0.1.0"},
            },
        )

    def list_tools(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        """拉取并缓存工具注册表 {工具名: 工具定义}。"""
        if self._tools is None or refresh:
            result = self._post("tools/list", {})
            self._tools = {t["name"]: t for t in result.get("tools", [])}
        return self._tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调用一个 MCP 工具，返回结构化数据。

        官方约定 content 为块数组，且实测 text 块为
        「使用说明前导文本 + 真实 JSON 响应（可能尾部还有说明）」的混合文本，
        因此这里做三级解析:
        1. 整体能 json.loads → 直接返回
        2. 从第一个 { 开始 raw_decode → 返回首个完整 JSON 对象
        3. 均失败 → 原样返回文本（由上层标注降级）
        """
        result = self._post("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content") or []
        for block in content:
            if block.get("type") == "text":
                return self._extract_json(block.get("text", ""))
        return result                    # 兜底: 直接返回 result

    @staticmethod
    def _extract_json(text: str) -> Any:
        """从混合文本中提取首个 JSON 对象（官方 text 块带说明前缀/后缀）。"""
        text = text.strip()
        # 1. 整体就是 JSON
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
        # 2. 从第一个 { 开始扫描首个合法 JSON 对象
        start = text.find("{")
        while start != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[start:])
                return obj
            except json.JSONDecodeError:
                start = text.find("{", start + 1)
        return text                      # 3. 非结构化文本原样返回
