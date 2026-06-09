from __future__ import annotations

from typing import Any

import httpx

from .config import RuntimeConfig


class GuangyaApiClient:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.config.guangya_api_key}

    def _check_config(self) -> None:
        if not self.config.guangya_api_base:
            raise RuntimeError("未配置 GUANGYA_API_BASE")
        if not self.config.guangya_api_key:
            raise RuntimeError("未配置 GUANGYA_API_KEY")

    async def health(self) -> dict[str, Any]:
        self._check_config()
        async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
            resp = await client.get(
                f"{self.config.guangya_api_base}/api/external/search/health",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def search(
        self,
        keyword: str,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self._check_config()
        params: dict[str, Any] = {"q": keyword, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if self.config.status:
            params["status"] = self.config.status
        async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
            resp = await client.get(
                f"{self.config.guangya_api_base}/api/external/search/resources",
                headers=self.headers,
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def detail(self, resource_id: int) -> dict[str, Any]:
        self._check_config()
        async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
            resp = await client.get(
                f"{self.config.guangya_api_base}/api/external/search/resources/{resource_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()
