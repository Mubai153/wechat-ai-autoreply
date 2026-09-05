"""SearXNG local search client used only by the LM Studio tool loop."""

from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import urlopen


class SearXNGSearch:
    def __init__(self, base_url: str, *, max_results: int = 5, timeout_seconds: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_results = max(1, max_results)
        self.timeout_seconds = max(3, timeout_seconds)

    def search(self, query: str) -> str:
        query = (query or "").strip()[:240]
        if not query:
            return "未提供搜索关键词。"
        params = urlencode({"q": query, "format": "json", "language": "zh-CN"})
        try:
            with urlopen(f"{self.base_url}/search?{params}", timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return f"本地搜索服务暂时不可用：{exc}"
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list) or not rows:
            return "没有找到可靠的搜索结果。"
        items = []
        for row in rows[: self.max_results]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title", "")).strip()
            url = str(row.get("url", "")).strip()
            content = str(row.get("content", "")).replace("\n", " ").strip()[:400]
            if title or content:
                items.append(f"标题：{title}\n摘要：{content}\n来源：{url}")
        return "\n\n".join(items) if items else "没有找到可用的搜索结果。"
