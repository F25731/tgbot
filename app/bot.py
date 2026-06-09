from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
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
CALLBACK_PAGE_PREFIX = "gy:p:"
CALLBACK_JUMP_PREFIX = "gy:j:"
CALLBACK_BACK = "gy:b"
PAGE_WINDOW = 2
PAGE_JUMP = 5


@dataclass
class SearchPage:
    items: list[dict[str, Any]]
    cursor: str | None
    has_more: bool
    shown_count: int


@dataclass
class SearchSession:
    keyword: str
    pages: dict[int, SearchPage] = field(default_factory=dict)
    current_page: int = 1
    exhausted: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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
                pattern=r"^gy:(?:d:\d+|p:\d+|j:\d+|b)$",
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

    def _page_summary(self, session: SearchSession, page: SearchPage) -> str:
        limit_text = "不限" if self._config().max_results == 0 else str(self._config().max_results)
        more_text = "，还有更多" if page.has_more else ""
        return (
            f"关键词「{html.escape(session.keyword)}」的搜索结果\n"
            f"第 {session.current_page} 页，当前 {len(page.items)} 条，已显示 {page.shown_count} 条，最多 {limit_text}{more_text}"
        )

    def _page_item_markup(self, session: SearchSession, page: SearchPage) -> list[list[InlineKeyboardButton]]:
        rows: list[list[InlineKeyboardButton]] = []
        start_index = page.shown_count - len(page.items) + 1
        for offset, item in enumerate(page.items):
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
        rows.append(self._page_nav_row(session))
        return rows

    def _page_nav_row(self, session: SearchSession) -> list[InlineKeyboardButton]:
        current = session.current_page
        pages = {current}
        for page_no in range(max(1, current - PAGE_WINDOW), current + PAGE_WINDOW + 1):
            pages.add(page_no)
        pages.add(1)
        if session.exhausted and session.pages:
            pages.add(max(session.pages))

        sorted_pages = sorted(p for p in pages if p >= 1)
        buttons: list[InlineKeyboardButton] = []
        prev_page: int | None = None
        for page_no in sorted_pages:
            if prev_page is not None and page_no - prev_page > 1:
                jump_to = max(1, current - PAGE_JUMP) if page_no <= current else current + PAGE_JUMP
                buttons.append(
                    InlineKeyboardButton("…", callback_data=f"{CALLBACK_JUMP_PREFIX}{jump_to}")
                )
            label = f"{page_no}✓" if page_no == current else str(page_no)
            buttons.append(
                InlineKeyboardButton(label, callback_data=f"{CALLBACK_PAGE_PREFIX}{page_no}")
            )
            prev_page = page_no

        if not session.exhausted:
            buttons.append(
                InlineKeyboardButton(
                    "…",
                    callback_data=f"{CALLBACK_JUMP_PREFIX}{current + PAGE_JUMP}",
                )
            )
        return buttons

    def _detail_markup(self, session: SearchSession | None) -> InlineKeyboardMarkup | None:
        if not session:
            return None
        rows = [[InlineKeyboardButton("返回列表", callback_data=CALLBACK_BACK)]]
        rows.append(self._page_nav_row(session))
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

    async def _fetch_page(
        self,
        keyword: str,
        bot,
        cursor: str | None,
        page_no: int,
        already_shown: int,
    ) -> SearchPage:
        limit = self._page_limit(already_shown)
        if limit <= 0:
            raise RuntimeError("已达到最大展示数量")
        result = await self._api().search(keyword, limit=limit, cursor=cursor)
        items = result.get("items") or []
        shown_count = already_shown + len(items)
        max_results = self._config().max_results
        has_more = bool(result.get("has_more")) and (max_results == 0 or shown_count < max_results)
        return SearchPage(
            items=items,
            cursor=result.get("next_cursor"),
            has_more=has_more,
            shown_count=shown_count,
        )

    async def _create_session(self, chat_id: int, keyword: str, bot) -> SearchSession:
        session = SearchSession(keyword=keyword)
        page = await self._fetch_page(keyword, bot, cursor=None, page_no=1, already_shown=0)
        session.pages[1] = page
        session.current_page = 1
        session.exhausted = not page.has_more
        session.updated_at = datetime.now(timezone.utc)
        self._sessions[chat_id] = session
        return session

    async def _ensure_page(self, session: SearchSession, target_page: int, bot) -> SearchPage | None:
        if target_page < 1:
            return None
        if target_page in session.pages:
            return session.pages[target_page]

        highest_loaded = max(session.pages) if session.pages else 0
        while highest_loaded < target_page:
            prev = session.pages.get(highest_loaded)
            if prev is None or not prev.cursor or not prev.has_more:
                session.exhausted = True
                break
            page_no = highest_loaded + 1
            next_page = await self._fetch_page(
                session.keyword,
                bot,
                cursor=prev.cursor,
                page_no=page_no,
                already_shown=prev.shown_count,
            )
            session.pages[page_no] = next_page
            highest_loaded = page_no
            session.exhausted = not next_page.has_more
            session.updated_at = datetime.now(timezone.utc)
            if not next_page.items:
                break

        return session.pages.get(target_page)

    async def _send_page(self, chat_id: int, session: SearchSession, bot) -> None:
        page = session.pages.get(session.current_page)
        if not page:
            await bot.send_message(chat_id=chat_id, text="列表已过期，请重新搜索")
            return
        if not page.items:
            await bot.send_message(chat_id=chat_id, text=f"没有找到关键词「{session.keyword}」的结果")
            return
        await bot.send_message(
            chat_id=chat_id,
            text=self._page_summary(session, page),
            reply_markup=InlineKeyboardMarkup(self._page_item_markup(session, page)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text("发送关键词即可搜索资源，也可以使用 /gy 关键词")

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        try:
            health = await self._api().health()
            proxy_text = "已启用" if self._config().proxy_enabled else "关闭"
            await update.effective_message.reply_text(
                f"检索接口正常\n"
                f"Key: {health.get('key_name', '-')}\n"
                f"每页: {self._config().page_size}\n"
                f"最多: {self._config().max_results}\n"
                f"代理: {proxy_text}"
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
            session = await self._create_session(chat_id, keyword, bot)
            await self._send_page(chat_id, session, bot)
        except Exception as exc:
            logger.exception("检索失败")
            await message.reply_text(f"检索失败：{exc}")

    async def _callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.message:
            return
        chat_id = query.message.chat_id
        payload = query.data or ""
        session = self._sessions.get(chat_id)

        try:
            if payload == CALLBACK_BACK:
                if not session:
                    await query.answer("列表已过期，请重新搜索", show_alert=False)
                    return
                await query.answer("返回列表", show_alert=False)
                await self._send_page(chat_id, session, context.bot)
                return

            if payload.startswith(CALLBACK_DETAIL_PREFIX):
                if not session:
                    await query.answer("列表已过期，请重新搜索", show_alert=False)
                    return
                resource_id = int(payload.removeprefix(CALLBACK_DETAIL_PREFIX))
                await query.answer("加载中", show_alert=False)
                item = await self._api().detail(resource_id)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=self._format_detail(item),
                    reply_markup=self._detail_markup(session),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return

            if payload.startswith(CALLBACK_PAGE_PREFIX):
                if not session:
                    await query.answer("列表已过期，请重新搜索", show_alert=False)
                    return
                target_page = int(payload.removeprefix(CALLBACK_PAGE_PREFIX))
                await query.answer("加载中", show_alert=False)
                page = await self._ensure_page(session, target_page, context.bot)
                if not page:
                    await query.answer("没有更多页了", show_alert=False)
                    return
                session.current_page = target_page
                session.updated_at = datetime.now(timezone.utc)
                await self._send_page(chat_id, session, context.bot)
                return

            if payload.startswith(CALLBACK_JUMP_PREFIX):
                if not session:
                    await query.answer("列表已过期，请重新搜索", show_alert=False)
                    return
                target_page = int(payload.removeprefix(CALLBACK_JUMP_PREFIX))
                await query.answer("加载中", show_alert=False)
                page = await self._ensure_page(session, target_page, context.bot)
                if not page:
                    await query.answer("没有更多页了", show_alert=False)
                    return
                session.current_page = target_page
                session.updated_at = datetime.now(timezone.utc)
                await self._send_page(chat_id, session, context.bot)
                return

            await query.answer("无效按钮", show_alert=False)
        except Exception as exc:
            logger.exception("按钮处理失败")
            await query.answer("处理失败", show_alert=False)
            await context.bot.send_message(chat_id=chat_id, text=f"处理失败：{exc}")
