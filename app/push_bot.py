from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import ConfigStore
from .guangya_api import GuangyaPushApiClient
from .metrics import Metrics

logger = logging.getLogger("app.push_bot")


class TelegramPushBot:
    """独立的资源推送 Bot，定时从光鸭后端领取资源并推送到 Telegram 频道。"""

    def __init__(self, store: ConfigStore, metrics: Metrics):
        self.store = store
        self.metrics = metrics
        self._task: asyncio.Task | None = None
        self._pushing = False

    # ---------- 生命周期 ----------

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_background(self) -> str:
        cfg = self.store.get()
        if not cfg.push_bot_token:
            return "未配置推送 Bot Token，无法启动"
        if not cfg.push_chat_id:
            return "未配置推送目标频道 ID，无法启动"
        if not cfg.push_api_base:
            return "未配置推送 API 地址，无法启动"
        if not cfg.push_api_key:
            return "未配置推送 API Key，无法启动"
        if self.running():
            return "推送 Bot 已在运行中"
        self._task = asyncio.create_task(self._poll_loop())
        self.metrics.add_event("success", "推送", "推送 Bot 已启动")
        logger.info("推送 Bot 已启动")
        return "推送 Bot 已启动"

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.metrics.add_event("warn", "推送", "推送 Bot 已停止")
        logger.info("推送 Bot 已停止")

    async def restart(self) -> str:
        await self.stop()
        return self.start_background()

    # ---------- 轮询循环 ----------

    async def _poll_loop(self) -> None:
        await asyncio.sleep(3)  # 启动延迟
        while True:
            try:
                cfg = self.store.get()
                if cfg.push_enabled:
                    await self._push_once_internal()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"推送轮询异常: {exc}")
                self.metrics.record_push_error(0, "", str(exc))
            cfg = self.store.get()
            await asyncio.sleep(cfg.push_poll_interval)

    async def push_once(self) -> int:
        """手动触发一次推送，返回推送条数。"""
        return await self._push_once_internal()

    async def _push_once_internal(self) -> int:
        if self._pushing:
            return 0
        self._pushing = True
        pushed = 0
        leased = 0
        try:
            cfg = self.store.get()
            client = GuangyaPushApiClient(
                cfg.push_api_base,
                cfg.push_api_key,
                proxy_url=self._proxy_url(cfg),
            )
            lease_data = await client.lease(cfg.push_batch_size, cfg.push_lease_stale_minutes)
            items = lease_data.get("items") or []
            leased = len(items)

            for item in items:
                resource_id = int(item["id"])
                resource_name = item.get("name", "")
                text = item.get("text") or self._fallback_text(item)
                try:
                    send_result = await self._send_message(cfg, text)
                    message_id = self._extract_message_id(send_result)
                    await self._callback_with_retry(
                        client, resource_id, "success",
                        message_id=message_id,
                        response_payload={"send_result": str(send_result)},
                    )
                    pushed += 1
                    self.metrics.record_push(resource_id, resource_name)
                except Exception as exc:
                    await self._callback_with_retry(
                        client, resource_id, "failed",
                        error_message=str(exc)[:500],
                    )
                    self.metrics.record_push_error(resource_id, resource_name, str(exc))
                await asyncio.sleep(cfg.push_send_interval)

            self.metrics.record_push_batch(leased, pushed)
            return pushed
        finally:
            self._pushing = False

    # ---------- Telegram API ----------

    @staticmethod
    def _proxy_url(cfg) -> str:
        if cfg.proxy_enabled and cfg.proxy_url:
            return cfg.proxy_url
        return ""

    async def _send_message(self, cfg, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": cfg.push_chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        if cfg.push_parse_mode:
            payload["parse_mode"] = cfg.push_parse_mode

        kwargs: dict[str, Any] = {"timeout": 30}
        proxy_url = self._proxy_url(cfg)
        if proxy_url:
            kwargs["proxy"] = proxy_url
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{cfg.push_bot_token}/sendMessage",
                json=payload,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            if resp.status_code >= 400 or data.get("ok") is False:
                raise RuntimeError(f"Telegram 发送失败: HTTP {resp.status_code}, {data}")
            return data

    # ---------- 回调 ----------

    async def _callback_with_retry(
        self,
        client: GuangyaPushApiClient,
        resource_id: int,
        status: str,
        error_message: str | None = None,
        message_id: str | None = None,
        response_payload: dict[str, Any] | None = None,
    ) -> None:
        last_error = None
        for _ in range(3):
            try:
                await client.callback(
                    resource_id=resource_id,
                    status=status,
                    error_message=error_message,
                    message_id=message_id,
                    response_payload=response_payload,
                )
                return
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(2)
        logger.error(f"推送回调失败 resource_id={resource_id}: {last_error}")

    # ---------- 辅助 ----------

    @staticmethod
    def _fallback_text(item: dict[str, Any]) -> str:
        return "\n".join([
            f"名称：{item.get('name') or ''}",
            f"标签：{item.get('tags') or ''}",
            f"链接：{item.get('share_link') or ''}",
        ])

    @staticmethod
    def _extract_message_id(send_result: Any) -> str | None:
        if send_result is None:
            return None
        if isinstance(send_result, dict):
            result = send_result.get("result")
            if isinstance(result, dict):
                value = result.get("message_id") or result.get("id")
                if value:
                    return str(value)
            value = send_result.get("message_id") or send_result.get("id")
            if value:
                return str(value)
        return None
