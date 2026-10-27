from __future__ import annotations

import asyncio
import logging
import sys

from .agent import AgentRunner
from .config import Config, ConfigError
from .gmail_client import GmailClient
from .outbox import Outbox
from .service import Service
from .store import Store
from .telegram import TelegramApi


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx пишет в журнал полный URL запроса, а в нём лежит токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        sys.exit(f"Ошибка конфигурации: {exc}")
    if not cfg.gmail_token_path.exists():
        sys.exit(f"Не найден токен Gmail: {cfg.gmail_token_path}. Выполните scripts/gmail_auth.py и скопируйте token.json.")

    store = Store(cfg.data_dir)
    gmail = GmailClient(cfg.gmail_token_path)
    outbox = Outbox(cfg, store, gmail)
    agent = AgentRunner(cfg, store, outbox)
    service = Service(cfg, store, outbox, gmail, agent, TelegramApi(cfg.telegram_token))
    logging.getLogger(__name__).info("запуск; DRY_RUN=%s, данные: %s", cfg.dry_run, cfg.data_dir)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
