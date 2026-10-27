from __future__ import annotations

import asyncio
import logging
import sys

from .agent import AgentRunner
from .config import Config, ConfigError
from .mail_client import MailClient
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

    store = Store(cfg.data_dir)
    mail = MailClient(
        address=cfg.mail_address,
        user=cfg.mail_user,
        password=cfg.mail_password,
        imap_host=cfg.imap_host,
        imap_port=cfg.imap_port,
        smtp_host=cfg.smtp_host,
        smtp_port=cfg.smtp_port,
        from_name=cfg.mail_from_name,
    )
    outbox = Outbox(cfg, store, mail)
    agent = AgentRunner(cfg, store, outbox)
    service = Service(cfg, store, outbox, mail, agent, TelegramApi(cfg.telegram_token))
    logging.getLogger(__name__).info("запуск; DRY_RUN=%s, данные: %s", cfg.dry_run, cfg.data_dir)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
