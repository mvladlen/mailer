from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(Exception):
    pass


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом") from exc


@dataclass(frozen=True)
class Config:
    telegram_token: str
    owner_id: int
    data_dir: Path
    gmail_token_path: Path
    model: str = "claude-sonnet-5-5"
    owner_tz: str = "Asia/Yekaterinburg"
    dry_run: bool = True
    poll_interval: int = 120
    max_emails_per_hour: int = 15
    max_emails_per_day: int = 60
    max_iterations: int = 25
    max_tokens: int = 4096

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        missing = [
            name
            for name in ("TELEGRAM_BOT_TOKEN", "OWNER_TELEGRAM_ID", "ANTHROPIC_API_KEY")
            if not env.get(name, "").strip()
        ]
        if missing:
            raise ConfigError("не заданы переменные окружения: " + ", ".join(missing))
        try:
            owner_id = int(env["OWNER_TELEGRAM_ID"].strip())
        except ValueError as exc:
            raise ConfigError("OWNER_TELEGRAM_ID должен быть числом") from exc
        data_dir = Path(env.get("HOTELBOT_DATA_DIR") or Path.home() / "hotelbot-data").expanduser()
        token_path = Path(
            env.get("GMAIL_TOKEN_PATH") or data_dir.parent / "gmail_token.json"
        ).expanduser()
        return cls(
            telegram_token=env["TELEGRAM_BOT_TOKEN"].strip(),
            owner_id=owner_id,
            data_dir=data_dir,
            gmail_token_path=token_path,
            model=env.get("MODEL", "").strip() or "claude-sonnet-5-5",
            owner_tz=env.get("OWNER_TZ", "").strip() or "Asia/Yekaterinburg",
            dry_run=_flag(env.get("DRY_RUN"), True),
            poll_interval=max(30, _int(env, "POLL_INTERVAL_SECONDS", 120)),
            max_emails_per_hour=_int(env, "MAX_EMAILS_PER_HOUR", 15),
            max_emails_per_day=_int(env, "MAX_EMAILS_PER_DAY", 60),
            max_iterations=_int(env, "MAX_AGENT_ITERATIONS", 25),
        )
