from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class RuntimeConfig:
    telegram_bot_token: str = ""
    guangya_api_base: str = ""
    guangya_api_key: str = ""
    page_size: int = 10
    max_results: int = 0
    status: str = ""
    request_timeout_seconds: int = 20
    bot_enabled: bool = True
    proxy_enabled: bool = False
    proxy_url: str = ""
    hot_window_hours: int = 48
    web_username: str = "admin"
    web_password: str = "admin123"
    # ---- 推送 Bot ----
    push_bot_token: str = ""
    push_chat_id: str = ""
    push_enabled: bool = False
    push_api_base: str = ""
    push_api_key: str = ""
    push_proxy_enabled: bool = False
    push_proxy_url: str = ""
    push_poll_interval: int = 30
    push_batch_size: int = 5
    push_send_interval: float = 1.0
    push_lease_stale_minutes: int = 30
    push_parse_mode: str = ""

    def normalized(self) -> "RuntimeConfig":
        self.guangya_api_base = self.guangya_api_base.rstrip("/")
        self.page_size = max(1, min(int(self.page_size or 10), 50))
        self.max_results = max(0, int(self.max_results or 0))
        self.hot_window_hours = max(1, int(self.hot_window_hours or 48))
        self.request_timeout_seconds = max(3, int(self.request_timeout_seconds or 20))
        self.status = (self.status or "").strip()
        self.proxy_url = (self.proxy_url or "").strip()
        # 推送 Bot 边界校验
        self.push_poll_interval = max(5, int(self.push_poll_interval or 30))
        self.push_batch_size = max(1, min(int(self.push_batch_size or 5), 100))
        self.push_send_interval = max(0.0, float(self.push_send_interval or 1.0))
        self.push_lease_stale_minutes = max(5, int(self.push_lease_stale_minutes or 30))
        self.push_api_base = (self.push_api_base or "").rstrip("/")
        self.push_parse_mode = (self.push_parse_mode or "").strip()
        self.push_chat_id = (self.push_chat_id or "").strip()
        self.push_proxy_url = (self.push_proxy_url or "").strip()
        if self.proxy_url.startswith("socks://"):
            self.proxy_url = "socks5://" + self.proxy_url.removeprefix("socks://")
        if self.push_proxy_url.startswith("socks://"):
            self.push_proxy_url = "socks5://" + self.push_proxy_url.removeprefix("socks://")
        return self

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self.normalized())
        data["telegram_bot_token_set"] = bool(self.telegram_bot_token)
        data["guangya_api_key_set"] = bool(self.guangya_api_key)
        data["proxy_url_set"] = bool(self.proxy_url)
        data["push_bot_token_set"] = bool(self.push_bot_token)
        data["push_api_key_set"] = bool(self.push_api_key)
        data["push_proxy_url_set"] = bool(self.push_proxy_url)
        for key in ("telegram_bot_token", "guangya_api_key", "proxy_url",
                     "web_password", "push_bot_token", "push_api_key", "push_proxy_url"):
            data.pop(key, None)
        return data


class ConfigStore:
    def __init__(self, path: str | Path = "data/config.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._config = self._load()

    def _defaults_from_env(self) -> RuntimeConfig:
        return RuntimeConfig(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            guangya_api_base=os.getenv("GUANGYA_API_BASE", ""),
            guangya_api_key=os.getenv("GUANGYA_API_KEY", ""),
            page_size=_int_env("PAGE_SIZE", 10),
            max_results=_int_env("MAX_RESULTS", 0),
            status=os.getenv("SEARCH_STATUS", ""),
            request_timeout_seconds=_int_env("REQUEST_TIMEOUT_SECONDS", 20),
            hot_window_hours=_int_env("HOT_WINDOW_HOURS", 48),
            bot_enabled=_bool_env("BOT_ENABLED", True),
            proxy_enabled=_bool_env("PROXY_ENABLED", False),
            proxy_url=os.getenv("PROXY_URL", ""),
            web_username=os.getenv("WEB_USERNAME", "admin"),
            web_password=os.getenv("WEB_PASSWORD", "admin123"),
            push_bot_token=os.getenv("PUSH_BOT_TOKEN", ""),
            push_chat_id=os.getenv("PUSH_CHAT_ID", ""),
            push_enabled=_bool_env("PUSH_ENABLED", False),
            push_api_base=os.getenv("PUSH_API_BASE", ""),
            push_api_key=os.getenv("PUSH_API_KEY", ""),
            push_proxy_enabled=_bool_env("PUSH_PROXY_ENABLED", False),
            push_proxy_url=os.getenv("PUSH_PROXY_URL", ""),
            push_poll_interval=_int_env("PUSH_POLL_INTERVAL", 30),
            push_batch_size=_int_env("PUSH_BATCH_SIZE", 5),
            push_send_interval=float(os.getenv("PUSH_SEND_INTERVAL", "1.0")),
            push_lease_stale_minutes=_int_env("PUSH_LEASE_STALE_MINUTES", 30),
            push_parse_mode=os.getenv("PUSH_PARSE_MODE", ""),
        ).normalized()

    def _load(self) -> RuntimeConfig:
        config = self._defaults_from_env()
        if not self.path.exists():
            self.save(config)
            return config
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return config
        for key, value in raw.items():
            if hasattr(config, key) and value not in (None, ""):
                setattr(config, key, value)
        return config.normalized()

    def get(self) -> RuntimeConfig:
        return self._config.normalized()

    def update(self, values: dict[str, Any]) -> RuntimeConfig:
        config = self.get()
        for key, value in values.items():
            if hasattr(config, key):
                setattr(config, key, value)
        self.save(config)
        self._config = config.normalized()
        return self._config

    def save(self, config: RuntimeConfig | None = None) -> None:
        current = (config or self._config).normalized()
        self.path.write_text(
            json.dumps(asdict(current), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
