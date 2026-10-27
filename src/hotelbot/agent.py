"""Запуск Claude с инструментами: один запуск = одна задача, состояние хранится в файлах."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from zoneinfo import ZoneInfo

import anthropic

from .config import Config
from .outbox import Outbox
from .store import Store
from .tools import RunContext, build_tools

log = logging.getLogger(__name__)


@dataclass
class AgentResult:
    reply: str
    notifications: list[str] = field(default_factory=list)


def load_system_prompt() -> str:
    return resources.files("hotelbot").joinpath("prompts/system.md").read_text(encoding="utf-8")


class AgentRunner:
    def __init__(self, cfg: Config, store: Store, outbox: Outbox, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.store = store
        self.outbox = outbox
        self.client = client or anthropic.Anthropic()

    def _system(self, source: str) -> str:
        now = datetime.now(ZoneInfo(self.cfg.owner_tz))
        runtime = (
            f"\n\n# Текущие условия\nЗапуск: {'по сообщению владельца' if source == 'owner' else 'по новым письмам отелей'}.\n"
            f"Сейчас у владельца: {now:%Y-%m-%d %H:%M} ({self.cfg.owner_tz}).\n"
            f"Режим проверки DRY RUN: {'включён, письма не отправляются' if self.cfg.dry_run else 'выключен, письма отправляются по-настоящему'}."
        )
        return load_system_prompt() + runtime

    def _compose(self, source: str, text: str) -> str:
        if source == "mail":
            return "Пришли новые письма отелей. Вызови get_unread и обработай их по правилам."
        chat = self.store.recent_chat(8)
        history = "\n".join(
            f"{'Владелец' if item['role'] == 'owner' else 'Ассистент'}: {item['text']}" for item in chat
        )
        pending = self.store.list_pending()
        parts = []
        if history:
            parts.append(f"<recent_dialog>\n{history}\n</recent_dialog>")
        if pending:
            lines = [f"{p['id']} (отель {p['hotel_id']}): {p['text']}" for p in pending]
            parts.append("<open_pending>\n" + "\n".join(lines) + "\n</open_pending>")
        parts.append(f"<owner_message>\n{text}\n</owner_message>")
        return "\n\n".join(parts)

    def run(self, source: str, text: str = "") -> AgentResult:
        ctx = RunContext(source=source, owner_text=text if source == "owner" else "")
        tools = build_tools(self.store, self.outbox, ctx)
        runner = self.client.beta.messages.tool_runner(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=self._system(source),
            tools=tools,
            messages=[{"role": "user", "content": self._compose(source, text)}],
            max_iterations=self.cfg.max_iterations,
        )
        final = runner.until_done()
        reply = "".join(block.text for block in final.content if getattr(block, "type", "") == "text").strip()
        if source == "owner":
            self.store.append_chat("owner", text)
            self.store.append_chat("assistant", reply or "; ".join(ctx.notifications))
        return AgentResult(reply=reply, notifications=ctx.notifications)
