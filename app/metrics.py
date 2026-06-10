from __future__ import annotations

import json
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque

LOG_LEVELS = ("info", "success", "warn", "error")
EVENTS_MAXLEN = 400
TOP_KEYWORDS = 10
TOP_RESOURCES = 20
SAVE_DEBOUNCE_SECONDS = 5.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_key() -> str:
    return _now().strftime("%Y-%m-%d")


def display_user(user_id: int | None, username: str | None, name: str | None) -> str:
    if username:
        return f"@{username}"
    if name:
        return name
    if user_id is not None:
        return f"#{user_id}"
    return "匿名"


@dataclass
class Event:
    id: int
    ts: str
    level: str
    action: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "level": self.level,
            "action": self.action,
            "text": self.text,
        }


class Metrics:
    """运营数据采集：累计/今日计数、独立用户、热门关键词、实时事件日志。"""

    def __init__(self, path: str | Path = "data/metrics.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.started_at: datetime | None = None
        self.totals = {
            "searches": 0,
            "details": 0,
            "pages": 0,
            "errors": 0,
            "api_calls": 0,
            "pushes": 0,
            "push_errors": 0,
            "push_batches": 0,
        }
        self.all_users: set[int] = set()
        self.today_date: str = _today_key()
        self.today_searches: int = 0
        self.today_pushes: int = 0
        self.today_users: set[int] = set()
        self.keywords: Counter[str] = Counter()
        self.resource_clicks: dict[str, list[float]] = {}  # resource_id -> [timestamp, ...]
        self.resource_names: dict[str, str] = {}  # resource_id -> name
        self.last_health: dict[str, Any] | None = None

        self._events: Deque[Event] = deque(maxlen=EVENTS_MAXLEN)
        self._event_seq: int = 0
        self._last_saved: float = 0.0

        self._load()

    # ---------- lifecycle ----------
    def mark_started(self, username: str | None = None) -> None:
        self.started_at = _now()
        who = f"@{username}" if username else "Bot"
        self.add_event("success", "系统", f"{who} 已启动，开始接收消息")

    def mark_stopped(self) -> None:
        self.started_at = None
        self.add_event("warn", "系统", "Bot 已停止")

    def uptime_seconds(self) -> int:
        if not self.started_at:
            return 0
        return int((_now() - self.started_at).total_seconds())

    # ---------- daily rollover ----------
    def _roll_day(self) -> None:
        today = _today_key()
        if today != self.today_date:
            self.today_date = today
            self.today_searches = 0
            self.today_pushes = 0
            self.today_users = set()

    # ---------- record events ----------
    def _touch_user(self, user_id: int | None) -> None:
        if user_id is None:
            return
        self.all_users.add(user_id)
        self.today_users.add(user_id)

    def record_search(
        self,
        user_id: int | None,
        username: str | None,
        name: str | None,
        keyword: str,
        result_count: int,
        has_more: bool,
    ) -> None:
        self._roll_day()
        self.totals["searches"] += 1
        self.totals["api_calls"] += 1
        self.today_searches += 1
        self._touch_user(user_id)
        kw = (keyword or "").strip()
        if kw:
            self.keywords[kw] += 1
        more = "+" if has_more else ""
        self.add_event(
            "info",
            "搜索",
            f"{display_user(user_id, username, name)} 搜索「{kw}」→ {result_count}{more} 条",
        )
        self._save_soon()

    def record_detail(
        self,
        user_id: int | None,
        username: str | None,
        name: str | None,
        resource_id: int,
        resource_name: str | None = None,
    ) -> None:
        self.totals["details"] += 1
        self.totals["api_calls"] += 1
        self._touch_user(user_id)
        # 记录资源点击时间戳
        rid = str(resource_id)
        self.resource_clicks.setdefault(rid, []).append(time.time())
        if resource_name:
            self.resource_names[rid] = resource_name
        label = resource_name or f"#{resource_id}"
        self.add_event(
            "info",
            "详情",
            f"{display_user(user_id, username, name)} 查看「{label}」",
        )
        self._save_soon()

    def record_page(
        self,
        user_id: int | None,
        username: str | None,
        name: str | None,
        page_no: int,
    ) -> None:
        self.totals["pages"] += 1
        self._touch_user(user_id)
        self.add_event(
            "info",
            "翻页",
            f"{display_user(user_id, username, name)} 跳转到第 {page_no} 页",
        )

    def record_error(
        self,
        user_id: int | None,
        username: str | None,
        name: str | None,
        keyword: str,
        error: str,
    ) -> None:
        self._roll_day()
        self.totals["errors"] += 1
        self._touch_user(user_id)
        self.add_event(
            "error",
            "失败",
            f"{display_user(user_id, username, name)} 搜索「{keyword}」失败：{error}",
        )
        self._save_soon()

    def record_health(self, ok: bool, latency_ms: int | None, key_name: str | None, error: str = "") -> None:
        self.totals["api_calls"] += 1
        self.last_health = {
            "ok": ok,
            "latency_ms": latency_ms,
            "key_name": key_name,
            "error": error,
            "at": _now().isoformat(),
        }

    # ---------- push metrics ----------
    def record_push(self, resource_id: int, resource_name: str) -> None:
        self.totals["pushes"] += 1
        self._roll_day()
        self.today_pushes += 1
        label = resource_name or f"#{resource_id}"
        self.add_event("success", "推送", f"推送成功「{label}」")
        self._save_soon()

    def record_push_error(self, resource_id: int, resource_name: str, error: str) -> None:
        self.totals["push_errors"] += 1
        label = resource_name or f"#{resource_id}" if resource_id else "未知"
        self.add_event("error", "推送", f"推送失败「{label}」: {error[:200]}")
        self._save_soon()

    def record_push_batch(self, leased: int, pushed: int) -> None:
        self.totals["push_batches"] += 1
        if leased > 0:
            self.add_event("info", "推送", f"批次完成：领取 {leased} 条，成功 {pushed} 条")

    def add_event(self, level: str, action: str, text: str) -> None:
        if level not in LOG_LEVELS:
            level = "info"
        self._event_seq += 1
        self._events.append(
            Event(
                id=self._event_seq,
                ts=_now().isoformat(),
                level=level,
                action=action,
                text=text,
            )
        )

    # ---------- hot resources ----------
    def _prune_clicks(self, window_seconds: float) -> None:
        """清理过期的点击记录。"""
        cutoff = time.time() - window_seconds
        to_delete = []
        for rid, timestamps in self.resource_clicks.items():
            filtered = [ts for ts in timestamps if ts >= cutoff]
            if filtered:
                self.resource_clicks[rid] = filtered
            else:
                to_delete.append(rid)
        for rid in to_delete:
            del self.resource_clicks[rid]
            self.resource_names.pop(rid, None)

    def top_resources(self, window_hours: int = 48, limit: int = TOP_RESOURCES) -> list[dict[str, Any]]:
        """返回时间窗口内点击最多的资源列表。"""
        window_seconds = window_hours * 3600
        self._prune_clicks(window_seconds)
        counted = [
            (rid, len(timestamps))
            for rid, timestamps in self.resource_clicks.items()
        ]
        counted.sort(key=lambda x: x[1], reverse=True)
        result = []
        for rid, count in counted[:limit]:
            result.append({
                "resource_id": int(rid),
                "name": self.resource_names.get(rid, f"#{rid}"),
                "clicks": count,
            })
        return result

    # ---------- read ----------
    def events_after(self, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        out = [e.to_dict() for e in self._events if e.id > after_id]
        if limit and len(out) > limit:
            out = out[-limit:]
        return out

    def snapshot(self, bot_running: bool, push_bot_running: bool = False) -> dict[str, Any]:
        self._roll_day()
        return {
            "bot_running": bot_running,
            "push_bot_running": push_bot_running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "uptime_seconds": self.uptime_seconds(),
            "totals": {**self.totals, "users": len(self.all_users)},
            "today": {
                "date": self.today_date,
                "searches": self.today_searches,
                "pushes": self.today_pushes,
                "users": len(self.today_users),
            },
            "keywords": [
                {"keyword": kw, "count": count}
                for kw, count in self.keywords.most_common(TOP_KEYWORDS)
            ],
            "health": self.last_health,
            "last_event_id": self._event_seq,
        }

    # ---------- persistence ----------
    def _save_soon(self) -> None:
        now = time.monotonic()
        if now - self._last_saved >= SAVE_DEBOUNCE_SECONDS:
            self.save()

    def save(self) -> None:
        self._last_saved = time.monotonic()
        data = {
            "totals": self.totals,
            "all_users": list(self.all_users),
            "today_date": self.today_date,
            "today_searches": self.today_searches,
            "today_pushes": self.today_pushes,
            "today_users": list(self.today_users),
            "keywords": dict(self.keywords),
            "resource_clicks": self.resource_clicks,
            "resource_names": self.resource_names,
        }
        try:
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logging.getLogger(__name__).warning("metrics 持久化失败", exc_info=True)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        totals = raw.get("totals") or {}
        for key in self.totals:
            if isinstance(totals.get(key), int):
                self.totals[key] = totals[key]
        self.all_users = set(int(u) for u in raw.get("all_users", []) if isinstance(u, int))
        self.keywords = Counter({str(k): int(v) for k, v in (raw.get("keywords") or {}).items()})
        # 恢复资源点击数据
        rc = raw.get("resource_clicks") or {}
        self.resource_clicks = {
            str(k): [float(ts) for ts in v if isinstance(ts, (int, float))]
            for k, v in rc.items() if isinstance(v, list)
        }
        rn = raw.get("resource_names") or {}
        self.resource_names = {str(k): str(v) for k, v in rn.items()}
        if raw.get("today_date") == _today_key():
            self.today_date = raw["today_date"]
            self.today_searches = int(raw.get("today_searches", 0) or 0)
            self.today_pushes = int(raw.get("today_pushes", 0) or 0)
            self.today_users = set(int(u) for u in raw.get("today_users", []) if isinstance(u, int))


class EventLogHandler(logging.Handler):
    """把 WARNING 及以上的日志同步进实时事件流，便于后台看到异常。"""

    def __init__(self, metrics: Metrics):
        super().__init__(level=logging.WARNING)
        self.metrics = metrics

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = "error" if record.levelno >= logging.ERROR else "warn"
            self.metrics.add_event(level, "系统", record.getMessage())
        except Exception:
            pass
