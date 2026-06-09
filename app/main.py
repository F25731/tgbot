from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .bot import TelegramSearchBot
from .config import ConfigStore
from .guangya_api import GuangyaApiClient

store = ConfigStore()
bot = TelegramSearchBot(store)
security = HTTPBasic()


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
    bot_enabled: bool | None = Field(default=None)
    web_username: str | None = Field(default=None, min_length=1, max_length=64)
    web_password: str | None = Field(default=None, min_length=1, max_length=128)

    def clean(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot.start_background()
    yield
    await bot.stop()


app = FastAPI(title="光鸭 Telegram 检索机器人", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


@app.get("/api/config")
async def get_config(_: str = Depends(require_admin)):
    data = store.get().public_dict()
    data["bot_running"] = bot.running()
    return data


@app.put("/api/config")
async def update_config(payload: ConfigPayload, _: str = Depends(require_admin)):
    old_token = store.get().telegram_bot_token
    config = store.update(payload.clean())
    token_changed = old_token != config.telegram_bot_token
    if token_changed and bot.running():
        await bot.restart()
    data = config.public_dict()
    data["bot_running"] = bot.running()
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
    try:
        api_health = await GuangyaApiClient(store.get()).health()
        api_ok = True
        api_error = ""
    except Exception as exc:
        api_health = {}
        api_ok = False
        api_error = str(exc)
    return {
        "ok": True,
        "bot_running": bot.running(),
        "guangya_api_ok": api_ok,
        "guangya_api": api_health,
        "guangya_api_error": api_error,
    }


@app.middleware("http")
async def no_cache_for_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response
