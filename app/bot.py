from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    Update,
    User,
)
from telegram.error import BadRequest
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
from .metrics import Metrics

logger = logging.getLogger(__name__)

CALLBACK_PAGE_PREFIX = "gy:p:"
CALLBACK_JUMP_PREFIX = "gy:j:"
DETAIL_START_PREFIX = "d_"
MENU_SEARCH_LABEL = "搜索资源"
MENU_HOT_LABEL = "热门资源"
MENU_HELP_LABEL = "帮助"
PAGE_WINDOW = 4
PAGE_JUMP = 4
MAX_LIST_TITLE_LENGTH = 76


@dataclass
class SearchPage:
    items: list[dict[str, Any]]
    cursor: str | None
    has_more: bool
    shown_count: int


@dataclass
class SearchSession:
    keyword: str
    total: int = 0
    pages: dict[int, SearchPage] = field(default_factory=dict)
    current_page: int = 1
    exhausted: bool = False
    list_message_id: int | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TelegramSearchBot:
    def __init__(self, store: ConfigStore, metrics: Metrics):
        self.store = store
        self.metrics = metrics
        self.application: Application | None = None
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._sessions: dict[int, SearchSession] = {}
        self._bot_username: str | None = None

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
        self._bot_username = None

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

    async def _set_bot_menu(self, application: Application) -> None:
        commands = [
            BotCommand("start", "打开菜单"),
            BotCommand("gy", "搜索资源"),
            BotCommand("hot", "热门资源"),
            BotCommand("status", "查看状态"),
        ]
        try:
            await application.bot.set_my_commands(commands)
            await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            logger.exception("设置 Telegram 命令菜单失败")

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
        application.add_handler(CommandHandler("hot", self._hot_command))
        application.add_handler(CommandHandler("status", self._status_command))
        application.add_handler(
            CallbackQueryHandler(
                self._callback,
                pattern=r"^gy:(?:p:\d+|j:\d+)$",
            )
        )
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._text_search))
        self.application = application

        try:
            await application.initialize()
            await application.start()
            try:
                me = await application.bot.get_me()
                self._bot_username = me.username
            except Exception:
                logger.exception("获取 Bot 用户名失败，资源详情链接将不可用")
                self._bot_username = None
            await self._set_bot_menu(application)
            if application.updater is None:
                raise RuntimeError("python-telegram-bot updater 不可用")
            await application.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
            logger.info("光鸭独立检索 Telegram Bot 已启动")
            self.metrics.mark_started(self._bot_username)
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
            self.metrics.mark_stopped()

    def _config(self) -> RuntimeConfig:
        return self.store.get()

    def _api(self) -> GuangyaApiClient:
        return GuangyaApiClient(self._config())

    @staticmethod
    def _user_fields(user: User | None) -> tuple[int | None, str | None, str | None]:
        if user is None:
            return None, None, None
        return user.id, user.username, user.full_name

    def _record(self, fn, user: User | None, *args) -> None:
        uid, uname, name = self._user_fields(user)
        try:
            fn(uid, uname, name, *args)
        except Exception:
            logger.debug("metrics 记录失败", exc_info=True)

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

    def _list_title(self, item: dict[str, Any]) -> str:
        title = self._clean_title(item)
        return title if len(title) <= MAX_LIST_TITLE_LENGTH else f"{title[:MAX_LIST_TITLE_LENGTH - 1]}..."

    def _detail_link(self, resource_id: Any) -> str | None:
        if not self._bot_username or resource_id is None:
            return None
        return f"https://t.me/{self._bot_username}?start={DETAIL_START_PREFIX}{resource_id}"

    def _menu_markup(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton(MENU_SEARCH_LABEL), KeyboardButton(MENU_HOT_LABEL)],
                [KeyboardButton(MENU_HELP_LABEL)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            one_time_keyboard=False,
            input_field_placeholder="输入关键词搜索资源",
        )

    def _page_list_text(self, session: SearchSession, page: SearchPage) -> str:
        start_index = page.shown_count - len(page.items) + 1
        kw = html.escape(session.keyword)
        if session.total > 0:
            header = f"关键词 \"{kw}\" │ 共 {session.total} 条结果"
        else:
            header = f"关键词 \"{kw}\""
        lines = [
            "🔍 <b>搜索结果</b>",
            "",
            f"<b>{header}</b>",
            "",
        ]
        for offset, item in enumerate(page.items):
            index = start_index + offset
            name = html.escape(self._list_title(item))
            link = self._detail_link(item.get("id"))
            if link:
                lines.append(f'{index}. <a href="{link}">{name}</a>')
            else:
                lines.append(f"{index}. {name}")
        return "\n".join(lines)

    def _page_nav_row(self, session: SearchSession) -> list[InlineKeyboardButton]:
        current = session.current_page
        if current <= 3:
            page_numbers = list(range(1, PAGE_WINDOW + 1))
        elif current == PAGE_WINDOW:
            page_numbers = list(range(1, PAGE_WINDOW + 2))
        else:
            page_numbers = [1]
            page_numbers.extend(range(current - 2, current + 3))

        last_page = max(session.pages) if session.pages else current
        if session.exhausted:
            page_numbers = [page_no for page_no in page_numbers if page_no <= last_page]

        visible: list[int | None] = []
        previous: int | None = None
        for page_no in sorted(set(page_numbers)):
            if page_no < 1:
                continue
            if previous is not None and page_no - previous > 1:
                visible.append(None)
            visible.append(page_no)
            previous = page_no

        if not session.exhausted:
            visible.append(None)
        elif last_page not in visible and last_page > 0:
            if visible and last_page - int(visible[-1]) > 1:
                visible.append(None)
            visible.append(last_page)

        buttons: list[InlineKeyboardButton] = []
        for idx, page_no in enumerate(visible):
            if page_no is None:
                if not buttons or buttons[-1].text == "…":
                    continue
                if idx == len(visible) - 1:
                    jump_to = current + 1
                elif idx > len(visible) // 2:
                    jump_to = current + PAGE_JUMP
                else:
                    jump_to = max(1, current - PAGE_JUMP)
                buttons.append(InlineKeyboardButton("…", callback_data=f"{CALLBACK_JUMP_PREFIX}{jump_to}"))
                continue
            label = f"✓{page_no}" if page_no == current else str(page_no)
            buttons.append(InlineKeyboardButton(label, callback_data=f"{CALLBACK_PAGE_PREFIX}{page_no}"))

        return buttons

    def _page_markup(self, session: SearchSession) -> InlineKeyboardMarkup | None:
        nav_row = self._page_nav_row(session)
        if not nav_row or len(nav_row) <= 1:
            return None
        return InlineKeyboardMarkup([nav_row])

    async def _render_page(
        self,
        chat_id: int,
        session: SearchSession,
        bot,
        *,
        message_id: int | None = None,
    ) -> None:
        page = session.pages.get(session.current_page)
        if not page:
            await bot.send_message(chat_id=chat_id, text="列表已过期，请重新搜索")
            return
        if not page.items:
            await bot.send_message(chat_id=chat_id, text=f"没有找到关键词「{session.keyword}」的结果")
            return

        text = self._page_list_text(session, page)
        reply_markup = self._page_markup(session)
        if message_id is None:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            session.list_message_id = sent.message_id
            return

        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            session.list_message_id = sent.message_id

    def _format_detail(self, item: dict[str, Any]) -> str:
        lines = [
            f"<b>名称：</b>{html.escape(str(item.get('name') or '-'))}",
            f"<b>标签：</b>{html.escape(str(item.get('tags') or '-'))}",
            f"<b>链接：</b>{html.escape(str(item.get('link') or '-'))}",
        ]
        extract_code = item.get("extract_code")
        if extract_code:
            lines.append(f"<b>提取码：</b>{html.escape(str(extract_code))}")
        return "\n".join(lines)

    def _detail_markup(self, item: dict[str, Any]) -> InlineKeyboardMarkup | None:
        link = str(item.get("link") or "")
        if link.startswith("http://") or link.startswith("https://"):
            return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 打开链接", url=link)]])
        return None

    async def _send_detail(self, chat_id: int, resource_id: int, bot, user: User | None = None) -> None:
        try:
            item = await self._api().detail(resource_id)
        except Exception as exc:
            logger.exception("获取资源详情失败")
            await bot.send_message(chat_id=chat_id, text=f"获取资源详情失败：{exc}")
            return
        self._record(self.metrics.record_detail, user, resource_id, item.get("name"))
        await bot.send_message(
            chat_id=chat_id,
            text=self._format_detail(item),
            reply_markup=self._detail_markup(item),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _fetch_page(
        self,
        keyword: str,
        bot,
        cursor: str | None,
        page_no: int,
        already_shown: int,
    ) -> tuple[SearchPage, int]:
        """返回 (SearchPage, total)，total 仅首页有值。"""
        limit = self._page_limit(already_shown)
        if limit <= 0:
            raise RuntimeError("已达到最大展示数量")
        result = await self._api().search(keyword, limit=limit, cursor=cursor)
        items = result.get("items") or []
        shown_count = already_shown + len(items)
        max_results = self._config().max_results
        has_more = bool(result.get("has_more")) and (max_results == 0 or shown_count < max_results)
        total = int(result.get("total") or 0)
        page = SearchPage(
            items=items,
            cursor=result.get("next_cursor"),
            has_more=has_more,
            shown_count=shown_count,
        )
        return page, total

    async def _create_session(self, chat_id: int, keyword: str, bot) -> SearchSession:
        session = SearchSession(keyword=keyword)
        page, total = await self._fetch_page(keyword, bot, cursor=None, page_no=1, already_shown=0)
        session.total = total
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
            next_page, _ = await self._fetch_page(
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
        await self._render_page(chat_id, session, bot)

    async def _hot_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return
        window = self._config().hot_window_hours
        top = self.metrics.top_resources(window_hours=window, limit=20)
        if not top:
            await message.reply_text(f"最近 {window} 小时内暂无热门资源", reply_markup=self._menu_markup())
            return
        lines = [
            f"🔥 <b>热门资源 TOP {len(top)}</b>",
            f"<i>最近 {window} 小时内点击最多</i>",
            "",
        ]
        for i, item in enumerate(top, 1):
            name = html.escape(str(item["name"]))
            clicks = item["clicks"]
            link = self._detail_link(item["resource_id"])
            if link:
                lines.append(f'{i}. <a href="{link}">{name}</a>  ({clicks}次)')
            else:
                lines.append(f"{i}. {name}  ({clicks}次)")
        await message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=self._menu_markup(),
        )

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return
        args = context.args or []
        if args and args[0].startswith(DETAIL_START_PREFIX):
            raw = args[0][len(DETAIL_START_PREFIX):]
            try:
                resource_id = int(raw)
            except ValueError:
                await message.reply_text("无效的资源链接")
                return
            await self._send_detail(chat.id, resource_id, context.bot, message.from_user)
            return
        await message.reply_text(
            "发送关键词即可搜索资源，也可以使用 /gy 关键词",
            reply_markup=self._menu_markup(),
        )

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
        if keyword == MENU_HOT_LABEL:
            await self._hot_command(update, context)
            return
        if keyword == MENU_SEARCH_LABEL:
            await message.reply_text(
                "直接发送关键词即可搜索资源，或者输入 /gy 关键词",
                reply_markup=self._menu_markup(),
            )
            return
        if keyword == MENU_HELP_LABEL:
            await message.reply_text(
                "可用菜单：搜索资源、热门资源、帮助",
                reply_markup=self._menu_markup(),
            )
            return
        if keyword:
            await self._safe_search(chat.id, keyword, context.bot, message)

    async def _safe_search(self, chat_id: int, keyword: str, bot, message) -> None:
        user = message.from_user if message else None
        try:
            session = await self._create_session(chat_id, keyword, bot)
            page = session.pages.get(1)
            result_count = len(page.items) if page else 0
            has_more = bool(page.has_more) if page else False
            self._record(self.metrics.record_search, user, keyword, result_count, has_more)
            await self._send_page(chat_id, session, bot)
        except Exception as exc:
            logger.exception("检索失败")
            self._record(self.metrics.record_error, user, keyword, str(exc))
            await message.reply_text(f"检索失败：{exc}")

    async def _callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.message:
            return
        chat_id = query.message.chat_id
        payload = query.data or ""
        session = self._sessions.get(chat_id)

        try:
            if payload.startswith(CALLBACK_PAGE_PREFIX) or payload.startswith(CALLBACK_JUMP_PREFIX):
                if not session:
                    await query.answer("列表已过期，请重新搜索", show_alert=False)
                    return
                prefix = (
                    CALLBACK_PAGE_PREFIX
                    if payload.startswith(CALLBACK_PAGE_PREFIX)
                    else CALLBACK_JUMP_PREFIX
                )
                target_page = int(payload.removeprefix(prefix))
                await query.answer("加载中", show_alert=False)
                page = await self._ensure_page(session, target_page, context.bot)
                if not page:
                    await query.answer("没有更多页了", show_alert=False)
                    return
                session.current_page = target_page
                session.updated_at = datetime.now(timezone.utc)
                self._record(self.metrics.record_page, query.from_user, target_page)
                await self._render_page(
                    chat_id,
                    session,
                    context.bot,
                    message_id=session.list_message_id,
                )
                return

            await query.answer("无效按钮", show_alert=False)
        except Exception as exc:
            logger.exception("按钮处理失败")
            await query.answer("处理失败", show_alert=False)
            await context.bot.send_message(chat_id=chat_id, text=f"处理失败：{exc}")
