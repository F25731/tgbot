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


class GuangyaPushApiClient:
    """光鸭推送 API 客户端。"""

    def __init__(self, api_base: str, api_key: str, timeout: int = 30):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    def _check_config(self) -> None:
        if not self.api_base:
            raise RuntimeError("未配置推送 API 地址")
        if not self.api_key:
            raise RuntimeError("未配置推送 API Key")

    async def health(self) -> dict[str, Any]:
        self._check_config()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.api_base}/api/external/push/health",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def lease(self, limit: int, retry_stale_minutes: int) -> dict[str, Any]:
        self._check_config()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.api_base}/api/external/push/lease",
                headers=self.headers,
                params={"limit": limit, "retry_stale_minutes": retry_stale_minutes},
            )
            resp.raise_for_status()
            return resp.json()

    async def callback(
        self,
        resource_id: int,
        status: str,
        error_message: str | None = None,
        message_id: str | None = None,
        response_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._check_config()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.api_base}/api/external/push/callback",
                headers=self.headers,
                json={
                    "resource_id": resource_id,
                    "status": status,
                    "error_message": error_message,
                    "message_id": message_id,
                    "response_payload": response_payload,
                },
            )
            resp.raise_for_status()
            return resp.json()
