from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .bot import TelegramSearchBot
from .config import ConfigStore
from .guangya_api import GuangyaApiClient
from .metrics import EventLogHandler, Metrics
from .push_bot import TelegramPushBot

store = ConfigStore()
metrics = Metrics()
bot = TelegramSearchBot(store, metrics)
push_bot = TelegramPushBot(store, metrics)
security = HTTPBasic()

logging.getLogger("app").addHandler(EventLogHandler(metrics))
logging.getLogger("app").setLevel(logging.INFO)


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    config = store.get()
    username_ok = secrets.compare_digest(credentials.username, config.web_username)
    password_ok = secrets.compare_digest(credentials.password, config.web_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证失败",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class ConfigPayload(BaseModel):
    telegram_bot_token: str | None = Field(default=None)
    guangya_api_base: str | None = Field(default=None)
    guangya_api_key: str | None = Field(default=None)
    page_size: int | None = Field(default=None, ge=1, le=50)
    max_results: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=32)
    request_timeout_seconds: int | None = Field(default=None, ge=3, le=120)
    hot_window_hours: int | None = Field(default=None, ge=1, le=720)
    bot_enabled: bool | None = Field(default=None)
    proxy_enabled: bool | None = Field(default=None)
    proxy_url: str | None = Field(default=None, max_length=512)
    web_username: str | None = Field(default=None, min_length=1, max_length=64)
    web_password: str | None = Field(default=None, min_length=1, max_length=128)
    # 推送 Bot
    push_bot_token: str | None = Field(default=None)
    push_chat_id: str | None = Field(default=None)
    push_enabled: bool | None = Field(default=None)
    push_api_base: str | None = Field(default=None)
    push_api_key: str | None = Field(default=None)
    push_proxy_enabled: bool | None = Field(default=None)
    push_proxy_url: str | None = Field(default=None, max_length=512)
    push_poll_interval: int | None = Field(default=None, ge=5, le=3600)
    push_batch_size: int | None = Field(default=None, ge=1, le=100)
    push_send_interval: float | None = Field(default=None, ge=0, le=60)
    push_lease_stale_minutes: int | None = Field(default=None, ge=5, le=1440)
    push_parse_mode: str | None = Field(default=None, max_length=32)

    def clean(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot.start_background()
    cfg = store.get()
    if cfg.push_enabled and cfg.push_bot_token and cfg.push_chat_id:
        push_bot.start_background()
    yield
    await bot.stop()
    await push_bot.stop()


app = FastAPI(title="光鸭 Telegram 检索机器人", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


@app.get("/api/config")
async def get_config(_: str = Depends(require_admin)):
    data = store.get().public_dict()
    data["bot_running"] = bot.running()
    data["push_bot_running"] = push_bot.running()
    return data


@app.put("/api/config")
async def update_config(payload: ConfigPayload, _: str = Depends(require_admin)):
    old_config = store.get()
    config = store.update(payload.clean())
    # 检索 Bot 重启检查
    should_restart = (
        old_config.telegram_bot_token != config.telegram_bot_token
        or old_config.proxy_enabled != config.proxy_enabled
        or old_config.proxy_url != config.proxy_url
        or old_config.request_timeout_seconds != config.request_timeout_seconds
    )
    if should_restart and bot.running():
        await bot.restart()
    # 推送 Bot 重启检查
    push_should_restart = (
        old_config.push_bot_token != config.push_bot_token
        or old_config.push_api_base != config.push_api_base
        or old_config.push_api_key != config.push_api_key
        or old_config.push_chat_id != config.push_chat_id
        or old_config.proxy_enabled != config.proxy_enabled
        or old_config.proxy_url != config.proxy_url
        or old_config.push_proxy_enabled != config.push_proxy_enabled
        or old_config.push_proxy_url != config.push_proxy_url
    )
    if push_should_restart and push_bot.running():
        await push_bot.restart()
    data = config.public_dict()
    data["bot_running"] = bot.running()
    data["push_bot_running"] = push_bot.running()
    return data


@app.post("/api/bot/start")
async def start_bot(_: str = Depends(require_admin)):
    message = bot.start_background()
    return {"message": message, "bot_running": bot.running()}


@app.post("/api/bot/stop")
async def stop_bot(_: str = Depends(require_admin)):
    await bot.stop()
    return {"message": "Bot 已停止", "bot_running": bot.running()}


@app.post("/api/bot/restart")
async def restart_bot(_: str = Depends(require_admin)):
    message = await bot.restart()
    return {"message": message, "bot_running": bot.running()}


@app.get("/api/health")
async def health(_: str = Depends(require_admin)):
    started = time.monotonic()
    try:
        api_health = await GuangyaApiClient(store.get()).health()
        api_ok = True
        api_error = ""
    except Exception as exc:
        api_health = {}
        api_ok = False
        api_error = str(exc)
    latency_ms = int((time.monotonic() - started) * 1000)
    metrics.record_health(api_ok, latency_ms if api_ok else None, api_health.get("key_name"), api_error)
    return {
        "ok": True,
        "bot_running": bot.running(),
        "push_bot_running": push_bot.running(),
        "guangya_api_ok": api_ok,
        "guangya_api": api_health,
        "guangya_api_error": api_error,
        "latency_ms": latency_ms,
    }


@app.get("/api/stats")
async def stats(_: str = Depends(require_admin)):
    return metrics.snapshot(bot.running(), push_bot.running())


@app.get("/api/logs")
async def logs(
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=400),
    _: str = Depends(require_admin),
):
    events = metrics.events_after(after_id=after, limit=limit)
    return {"events": events, "last_event_id": metrics.snapshot(bot.running(), push_bot.running())["last_event_id"]}


@app.get("/api/hot")
async def hot_resources(_: str = Depends(require_admin)):
    window = store.get().hot_window_hours
    top = metrics.top_resources(window_hours=window, limit=20)
    return {"window_hours": window, "resources": top}


# ---------- 推送 Bot API ----------

@app.post("/api/push-bot/start")
async def start_push_bot(_: str = Depends(require_admin)):
    message = push_bot.start_background()
    return {"message": message, "push_bot_running": push_bot.running()}


@app.post("/api/push-bot/stop")
async def stop_push_bot(_: str = Depends(require_admin)):
    await push_bot.stop()
    return {"message": "推送 Bot 已停止", "push_bot_running": push_bot.running()}


@app.post("/api/push-bot/restart")
async def restart_push_bot(_: str = Depends(require_admin)):
    message = await push_bot.restart()
    return {"message": message, "push_bot_running": push_bot.running()}


@app.post("/api/push-bot/push-once")
async def push_once(_: str = Depends(require_admin)):
    if not push_bot.running():
        raise HTTPException(status_code=400, detail="推送 Bot 未运行")
    count = await push_bot.push_once()
    return {"message": f"已推送 {count} 条资源", "pushed": count}


@app.middleware("http")
async def no_cache_for_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response
