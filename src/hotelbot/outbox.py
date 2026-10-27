"""Единственное место, где письма уходят наружу. Все защитные правила собраны здесь:

* получатель берётся только из реестра отелей (модель передаёт id, а не адрес);
* повторная отправка одного и того же текста отелю блокируется;
* лимиты писем в час и в сутки;
* режим DRY_RUN: письма записываются в переписку, но не отправляются.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any

from .config import Config
from .store import Store, StoreError, body_hash

log = logging.getLogger(__name__)
MAX_BODY_CHARS = 8000


class SendRefused(Exception):
    """Отправка отклонена правилами; текст можно показать модели."""


def _clean_subject(subject: str) -> str:
    subject = " ".join(subject.split())
    if not subject:
        raise SendRefused("пустая тема письма")
    return subject[:200]


class Outbox:
    def __init__(self, cfg: Config, store: Store, gmail: Any, delay: float = 2.0):
        self.cfg = cfg
        self.store = store
        self.gmail = gmail
        self.delay = delay

    def _check(self, hotel: dict, body: str) -> str:
        body = body.strip()
        if not body:
            raise SendRefused("пустой текст письма")
        if len(body) > MAX_BODY_CHARS:
            raise SendRefused(f"письмо длиннее {MAX_BODY_CHARS} символов")
        digest = body_hash(body)
        if self.store.was_sent_recently(hotel["id"], digest):
            raise SendRefused("такое письмо этому отелю уже отправлялось за последние 24 часа")
        if not self.cfg.dry_run:
            if self.store.count_sent_since(timedelta(hours=1)) >= self.cfg.max_emails_per_hour:
                raise SendRefused("достигнут лимит писем в час; повторите позже")
            if self.store.count_sent_since(timedelta(days=1)) >= self.cfg.max_emails_per_day:
                raise SendRefused("достигнут лимит писем в сутки")
        return digest

    def _deliver(
        self,
        hotel: dict,
        subject: str,
        body: str,
        kind: str,
        thread_id: str | None,
        in_reply_to: str | None,
    ) -> dict:
        digest = self._check(hotel, body)
        when = self.store.clock()
        if self.cfg.dry_run:
            self.store.append_transcript(hotel["id"], "OUT", when, subject, body, note="DRY RUN, не отправлено")
            self.store.log_send(hotel["id"], kind, digest, None, dry_run=True)
            self.store.mark_contacted(hotel["id"], None, subject)
            return {"sent": False, "dry_run": True}
        try:
            result = self.gmail.send(hotel["email"], subject, body, thread_id=thread_id, in_reply_to=in_reply_to)
        except Exception as exc:  # сетевые ошибки и ошибки API не должны ронять сервис
            log.exception("не удалось отправить письмо отелю %s", hotel["id"])
            raise SendRefused(f"Gmail вернул ошибку, письмо не отправлено: {type(exc).__name__}") from exc
        self.store.append_transcript(hotel["id"], "OUT", when, subject, body)
        self.store.log_send(hotel["id"], kind, digest, result["id"], dry_run=False)
        self.store.mark_contacted(hotel["id"], result.get("thread_id"), subject)
        return {"sent": True, "dry_run": False}

    def send_first(self, hotel_id: str, subject: str, body: str) -> dict:
        hotel = self.store.get_hotel(hotel_id)
        if hotel["contacted_at"]:
            raise SendRefused(f"отелю {hotel['name']} уже писали")
        return self._deliver(hotel, _clean_subject(subject), body, "first", None, None)

    def send_in_thread(self, hotel_id: str, body: str) -> dict:
        hotel = self.store.get_hotel(hotel_id)
        if not hotel["contacted_at"]:
            raise SendRefused(f"отелю {hotel['name']} ещё не отправлено первое письмо")
        if not self.cfg.dry_run and not hotel.get("thread_id"):
            raise SendRefused("нет ветки переписки с этим отелем")
        base = hotel.get("last_subject") or "Запрос"
        subject = base if re.match(r"(?i)^re\s*:", base) else f"Re: {base}"
        return self._deliver(
            hotel,
            _clean_subject(subject),
            body,
            "reply",
            hotel.get("thread_id"),
            hotel.get("last_inbound_message_id"),
        )

    def approve_batch(self) -> dict:
        """Отправляет утверждённую владельцем первую рассылку. LLM здесь не участвует."""
        batch = self.store.get_batch()
        if not batch:
            return {"error": "нет черновика рассылки"}
        sent: list[str] = []
        skipped: list[tuple[str, str]] = []
        stopped: str | None = None
        for hotel_id in batch["hotel_ids"]:
            try:
                hotel = self.store.get_hotel(hotel_id)
            except StoreError as exc:
                skipped.append((hotel_id, str(exc)))
                continue
            if hotel["contacted_at"]:
                skipped.append((hotel["name"], "уже писали"))
                continue
            body = batch["body_template"].replace("{hotel_name}", hotel["name"])
            try:
                self.send_first(hotel_id, batch["subject"], body)
            except SendRefused as exc:
                skipped.append((hotel["name"], str(exc)))
                if "лимит" in str(exc):
                    stopped = str(exc)
                    break
                continue
            sent.append(hotel["name"])
            if self.delay:
                time.sleep(self.delay)
        remaining = [
            h for h in batch["hotel_ids"] if not self.store.get_hotel(h)["contacted_at"]
        ]
        if not remaining:
            self.store.clear_batch()
        return {
            "sent": sent,
            "skipped": skipped,
            "stopped": stopped,
            "dry_run": self.cfg.dry_run,
            "remaining": len(remaining),
        }
