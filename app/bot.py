from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import ConfigStore, RuntimeConfig
from .guangya_api import GuangyaApiClient

logger = logging.getLogger(__name__)

CALLBACK_DETAIL_PREFIX = "gy:d:"
CALLBACK_NEXT = "gy:n"
CALLBACK_BACK = "gy:b"


@dataclass
class SearchSession:
    keyword: str
    cursor: str | None
    items: list[dict[str, Any]]
    page_no: int
    shown_count: int
    has_more: bool
    updated_at: datetime


class TelegramSearchBot:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.application: Application | None = None
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._sessions: dict[int, SearchSession] = {}

    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    def start_background(self) -> str:
        config = self.store.get()
        if not config.bot_enabled:
            return "Bot 未启用"
        if not config.telegram_bot_token:
            return "未配置 Telegram Bot Token"
        if self.running():
            return "Bot 已在运行"
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(config.telegram_bot_token))
        return "Bot 正在启动"

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._stop_event = None
        self.application = None

    async def restart(self) -> str:
        await self.stop()
        return self.start_background()

    def _telegram_request(self, config: RuntimeConfig) -> HTTPXRequest:
        kwargs: dict[str, Any] = {
            "connect_timeout": float(config.request_timeout_seconds),
            "read_timeout": float(config.request_timeout_seconds),
            "write_timeout": float(config.request_timeout_seconds),
            "pool_timeout": float(config.request_timeout_seconds),
        }
        if config.proxy_enabled and config.proxy_url:
            kwargs["proxy"] = config.proxy_url
        return HTTPXRequest(**kwargs)

    async def _run(self, token: str) -> None:
        config = self._config()
        request = self._telegram_request(config)
        get_updates_request = self._telegram_request(config)
        application = (
            Application.builder()
            .token(token)
            .request(request)
            .get_updates_request(get_updates_request)
            .build()
        )
        application.add_handler(CommandHandler("start", self._start_command))
        application.add_handler(CommandHandler("gy", self._search_command))
        application.add_handler(CommandHandler("status", self._status_command))
        application.add_handler(
            CallbackQueryHandler(
                self._callback,
                pattern=r"^gy:(?:d:\d+|n|b)$",
            )
        )
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._text_search))
        self.application = application

        try:
            await application.initialize()
            await application.start()
            if application.updater is None:
                raise RuntimeError("python-telegram-bot updater 不可用")
            await application.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
            logger.info("光鸭独立检索 Telegram Bot 已启动")
            if self._stop_event:
                await self._stop_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("光鸭独立检索 Telegram Bot 运行失败")
        finally:
            try:
                if application.updater and application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()
                await application.shutdown()
            except Exception:
                logger.exception("光鸭独立检索 Telegram Bot 停止异常")

    def _config(self) -> RuntimeConfig:
        return self.store.get()

    def _api(self) -> GuangyaApiClient:
        return GuangyaApiClient(self._config())

    def _page_limit(self, already_shown: int = 0) -> int:
        config = self._config()
        if config.max_results > 0:
            remaining = config.max_results - already_shown
            if remaining <= 0:
                return 0
            return min(config.page_size, remaining)
        return config.page_size

    def _clean_title(self, item: dict[str, Any]) -> str:
        title = str(item.get("name") or "-").replace("\r", " ").replace("\n", " ")
        return " ".join(title.split())

    def _button_title(self, index: int, item: dict[str, Any]) -> str:
        title = f"{index}. {self._clean_title(item)}"
        return title if len(title) <= 58 else f"{title[:57]}..."

    def _format_list_text(self, state: SearchSession) -> str:
        limit_text = "不限" if self._config().max_results == 0 else str(self._config().max_results)
        more_text = "，还有更多" if state.has_more else ""
        return (
            f"关键词「{html.escape(state.keyword)}」的搜索结果\n"
            f"第 {state.page_no} 页，当前 {len(state.items)} 条，已显示 {state.shown_count} 条，最多 {limit_text}{more_text}"
        )

    def _list_markup(self, state: SearchSession) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        start_index = state.shown_count - len(state.items) + 1
        for offset, item in enumerate(state.items):
            rid = item.get("id")
            if rid is None:
                continue
            rows.append(
                [
                    InlineKeyboardButton(
                        self._button_title(start_index + offset, item),
                        callback_data=f"{CALLBACK_DETAIL_PREFIX}{rid}",
                    )
                ]
            )
        if state.has_more:
            rows.append([InlineKeyboardButton("下一页", callback_data=CALLBACK_NEXT)])
        return InlineKeyboardMarkup(rows)

    def _detail_markup(self, state: SearchSession | None) -> InlineKeyboardMarkup | None:
        if not state:
            return None
        rows = [[InlineKeyboardButton("返回列表", callback_data=CALLBACK_BACK)]]
        if state.has_more:
            rows.append([InlineKeyboardButton("下一页", callback_data=CALLBACK_NEXT)])
        return InlineKeyboardMarkup(rows)

    def _format_detail(self, item: dict[str, Any]) -> str:
        lines = [
            f"<b>名称：</b>{html.escape(str(item.get('name') or '-'))}",
            f"<b>标签：</b>{html.escape(str(item.get('tags') or '-'))}",
            f"<b>链接：</b>{html.escape(str(item.get('link') or '-'))}",
        ]
        extract_code = item.get("extract_code")
        if extract_code:
            lines.append(f"<b>提取码：</b>{html.escape(str(extract_code))}")
        lines.append(f"<b>ID：</b>{html.escape(str(item.get('id') or '-'))}")
        return "\n".join(lines)

    async def _send_search_page(
        self,
        chat_id: int,
        keyword: str,
        bot,
        cursor: str | None = None,
        page_no: int = 1,
        already_shown: int = 0,
    ) -> None:
        limit = self._page_limit(already_shown)
        if limit <= 0:
            await bot.send_message(chat_id=chat_id, text="已达到最大展示数量")
            return
        result = await self._api().search(keyword, limit=limit, cursor=cursor)
        items = result.get("items") or []
        shown_count = already_shown + len(items)
        max_results = self._config().max_results
        has_more = bool(result.get("has_more")) and (max_results == 0 or shown_count < max_results)
        state = SearchSession(
            keyword=keyword,
            cursor=result.get("next_cursor"),
            items=items,
            page_no=page_no,
            shown_count=shown_count,
            has_more=has_more,
            updated_at=datetime.now(timezone.utc),
        )
        self._sessions[chat_id] = state

        if not items:
            await bot.send_message(chat_id=chat_id, text=f"没有找到关键词「{keyword}」的结果")
            return
        await bot.send_message(
            chat_id=chat_id,
            text=self._format_list_text(state),
            reply_markup=self._list_markup(state),
            parse_mode="HTML",
        )

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text("发送关键词即可搜索资源，也可以使用 /gy 关键词")

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        try:
            health = await self._api().health()
            await update.effective_message.reply_text(
                f"检索接口正常\nKey: {health.get('key_name', '-')}\n每页: {self._config().page_size}\n最多: {self._config().max_results}"
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"检索接口异常：{exc}")

    async def _search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return
        keyword = " ".join(context.args or []).strip()
        if not keyword:
            await message.reply_text("请发送 /gy 关键词，或直接发送关键词")
            return
        await self._safe_search(chat.id, keyword, context.bot, message)

    async def _text_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return
        keyword = (message.text or "").strip()
        if keyword:
            await self._safe_search(chat.id, keyword, context.bot, message)

    async def _safe_search(self, chat_id: int, keyword: str, bot, message) -> None:
        try:
            await self._send_search_page(chat_id, keyword, bot)
        except Exception as exc:
            logger.exception("检索失败")
            await message.reply_text(f"检索失败：{exc}")

    async def _callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.message:
            return
        chat_id = query.message.chat_id
        payload = query.data or ""
        state = self._sessions.get(chat_id)

        try:
            if payload == CALLBACK_NEXT:
                if not state:
                    await query.answer("列表已过期，请重新搜索", show_alert=False)
                    return
                if not state.has_more or not state.cursor:
                    await query.answer("没有更多了", show_alert=False)
                    return
                await query.answer("加载中", show_alert=False)
                await self._send_search_page(
                    chat_id,
                    state.keyword,
                    context.bot,
                    cursor=state.cursor,
                    page_no=state.page_no + 1,
                    already_shown=state.shown_count,
                )
                return

            if payload == CALLBACK_BACK:
                if not state:
                    await query.answer("列表已过期，请重新搜索", show_alert=False)
                    return
                await query.answer("返回列表", show_alert=False)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=self._format_list_text(state),
                    reply_markup=self._list_markup(state),
                    parse_mode="HTML",
                )
                return

            if payload.startswith(CALLBACK_DETAIL_PREFIX):
                resource_id = int(payload.removeprefix(CALLBACK_DETAIL_PREFIX))
                await query.answer("加载中", show_alert=False)
                item = await self._api().detail(resource_id)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=self._format_detail(item),
                    reply_markup=self._detail_markup(state),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return

            await query.answer("无效按钮", show_alert=False)
        except Exception as exc:
            logger.exception("按钮处理失败")
            await query.answer("处理失败", show_alert=False)
            await context.bot.send_message(chat_id=chat_id, text=f"处理失败：{exc}")
