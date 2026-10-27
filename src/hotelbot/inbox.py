"""Опрос почты без участия LLM: забирает новые письма и раскладывает их по отелям."""

from __future__ import annotations

import logging
from typing import Any

from .mail_client import IncomingMessage
from .store import Store

log = logging.getLogger(__name__)


def poll_once(store: Store, mail: Any) -> list[IncomingMessage]:
    hotels = [h for h in store.list_hotels() if h["contacted_at"]]
    if not hotels:
        return []
    own = mail.own_email
    by_email = {h["email"]: h for h in hotels}
    sent_ids = store.sent_message_ids()
    known = store.processed_ids()

    candidates = mail.fetch_relevant(set(by_email), set(sent_ids), known)
    new: list[IncomingMessage] = []
    for msg in sorted(candidates, key=lambda m: m.date):
        if msg.from_email == own or msg.id in known:
            continue
        # ответ на наше письмо (даже с другого адреса отеля) или письмо с адреса из реестра
        replied_to = next((sent_ids[ref] for ref in msg.references if ref in sent_ids), None)
        hotel = store.get_hotel(replied_to) if replied_to else by_email.get(msg.from_email)
        if hotel is None:
            continue
        store.record_inbound(hotel["id"], msg)
        known.add(msg.id)
        new.append(msg)
    if new:
        log.info("получено новых писем: %d", len(new))
    return new
