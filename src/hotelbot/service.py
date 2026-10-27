"""Оркестрация: Telegram-слушатель, опрос почты и один воркер, который выполняет задачи по очереди."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .agent import AgentRunner
from .config import Config
from .inbox import poll_once
from .outbox import Outbox
from .store import Store, StoreError
from .telegram import TelegramApi, extract_owner_text

log = logging.getLogger(__name__)

HELP = (
    "Пишите обычным текстом, например: «спроси у отелей, есть ли номера с джакузи» и пришлите email.\n\n"
    "Команды:\n"
    "/send — отправить утверждённый шаблон первого письма\n"
    "/cancel — отменить черновик рассылки\n"
    "/check — проверить почту сейчас\n"
    "/hotels — список отелей и статусы\n"
    "/transcript название — переписка с отелем\n"
    "/status — состояние сервиса"
)


@dataclass
class Job:
    kind: str  # "owner" | "mail" | "send_batch"
    text: str = ""


class Service:
    def __init__(self, cfg: Config, store: Store, outbox: Outbox, gmail: Any, agent: AgentRunner, tg: TelegramApi):
        self.cfg = cfg
        self.store = store
        self.outbox = outbox
        self.gmail = gmail
        self.agent = agent
        self.tg = tg
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self._mail_queued = False

    async def say(self, text: str) -> None:
        try:
            await self.tg.send_message(self.cfg.owner_id, text)
        except Exception:
            log.exception("не удалось отправить сообщение в Telegram")

    # ---- Telegram ----------------------------------------------------------------

    async def telegram_loop(self) -> None:
        await self.tg.prepare()
        offset: int | None = None
        while True:
            try:
                updates = await self.tg.get_updates(offset)
            except Exception as exc:
                log.warning("ошибка опроса Telegram: %s", type(exc).__name__)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                text = extract_owner_text(update, self.cfg.owner_id)
                if text is not None:
                    await self.handle_owner_text(text)

    async def handle_owner_text(self, text: str) -> None:
        if not text.startswith("/"):
            await self.queue.put(Job("owner", text))
            return
        command, _, arg = text.partition(" ")
        command = command.split("@")[0].lower()
        arg = arg.strip()
        if command in ("/start", "/help"):
            await self.say(HELP)
        elif command == "/status":
            await self.say(self.format_status())
        elif command == "/hotels":
            await self.say(self.format_hotels())
        elif command == "/send":
            if self.store.get_batch() is None:
                await self.say("Нет шаблона на отправку. Сначала попросите меня подготовить письмо.")
            else:
                await self.queue.put(Job("send_batch"))
        elif command == "/cancel":
            self.store.clear_batch()
            await self.say("Черновик рассылки отменён.")
        elif command == "/check":
            await self.check_mail_now()
        elif command == "/transcript":
            await self.send_transcript(arg)
        else:
            await self.say("Неизвестная команда.\n\n" + HELP)

    def format_status(self) -> str:
        hotels = self.store.list_hotels()
        by_status: dict[str, int] = {}
        for hotel in hotels:
            by_status[hotel["status"]] = by_status.get(hotel["status"], 0) + 1
        statuses = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items())) or "отелей нет"
        unread = len(self.store.all_unread())
        return (
            f"Режим проверки (DRY RUN): {'включён, письма не отправляются' if self.cfg.dry_run else 'выключен'}\n"
            f"Отели: {statuses}\n"
            f"Ждут вашего ответа: {len(self.store.list_pending())}\n"
            f"Непрочитанных писем: {unread}\n"
            f"Отправлено за час / за сутки: {self.store.count_sent_since(timedelta(hours=1))} / "
            f"{self.store.count_sent_since(timedelta(days=1))}\n"
            f"Задач в очереди: {self.queue.qsize()}\n"
            f"Черновик рассылки: {'есть, ждёт /send' if self.store.get_batch() else 'нет'}"
        )

    def format_hotels(self) -> str:
        hotels = self.store.list_hotels()
        if not hotels:
            return "Отелей пока нет."
        return "\n".join(f"{h['id']} · {h['name']} <{h['email']}> · {h['status']}" for h in hotels)

    async def send_transcript(self, ref: str) -> None:
        if not ref:
            await self.say("Укажите отель: /transcript название\n\n" + self.format_hotels())
            return
        try:
            hotel = self.store.resolve_hotel(ref)
        except StoreError as exc:
            await self.say(str(exc))
            return
        text = self.store.read_transcript(hotel["id"])
        if not text.strip():
            await self.say(f"Переписки с {hotel['name']} пока нет.")
        elif len(text) <= 3900:
            await self.say(text)
        else:
            await self.tg.send_document(self.cfg.owner_id, f"{hotel['id']}.md", text.encode("utf-8"), hotel["name"])

    # ---- почта -------------------------------------------------------------------

    async def check_mail_now(self) -> None:
        new = await asyncio.to_thread(poll_once, self.store, self.gmail)
        if new or self.store.all_unread():
            await self.enqueue_mail()
            await self.say(f"Новых писем: {len(new)}. Обрабатываю.")
        else:
            await self.say("Новых писем нет.")

    async def enqueue_mail(self) -> None:
        if not self._mail_queued:
            self._mail_queued = True
            await self.queue.put(Job("mail"))

    async def mail_loop(self) -> None:
        if self.store.all_unread():
            await self.enqueue_mail()  # после перезапуска дообработать необработанное
        while True:
            await asyncio.sleep(self.cfg.poll_interval)
            try:
                new = await asyncio.to_thread(poll_once, self.store, self.gmail)
                if new:
                    await self.enqueue_mail()
            except Exception as exc:
                log.warning("ошибка опроса Gmail: %s", type(exc).__name__)

    # ---- воркер ------------------------------------------------------------------

    async def worker_loop(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self.process(job)
            except Exception:
                log.exception("ошибка при выполнении задачи %s", job.kind)
                await self.say("Не удалось выполнить задачу: произошла внутренняя ошибка. Подробности в журнале сервера.")
            finally:
                if job.kind == "mail":
                    self._mail_queued = False

    async def process(self, job: Job) -> None:
        if job.kind == "send_batch":
            report = await asyncio.to_thread(self.outbox.approve_batch)
            await self.say(self.format_batch_report(report))
            return
        result = await asyncio.to_thread(self.agent.run, job.kind, job.text)
        for note in result.notifications:
            await self.say(note)
        if job.kind == "owner" and result.reply:
            await self.say(result.reply)
        elif job.kind == "owner" and not result.notifications:
            await self.say("Готово.")

    @staticmethod
    def format_batch_report(report: dict) -> str:
        if "error" in report:
            return report["error"]
        lines = []
        if report["dry_run"]:
            lines.append("РЕЖИМ ПРОВЕРКИ: письма записаны в переписку, но не отправлены.")
        lines.append(f"Отправлено: {len(report['sent'])}" + (": " + ", ".join(report["sent"]) if report["sent"] else ""))
        for name, reason in report["skipped"]:
            lines.append(f"Пропущено {name}: {reason}")
        if report["remaining"]:
            lines.append(f"Не разослано: {report['remaining']}. Напишите /send ещё раз позже.")
        return "\n".join(lines)

    async def run(self) -> None:
        await asyncio.gather(self.telegram_loop(), self.mail_loop(), self.worker_loop())
