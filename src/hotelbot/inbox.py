"""Опрос почты без участия LLM: забирает новые письма и раскладывает их по отелям."""

from __future__ import annotations

import logging
from typing import Any

from .gmail_client import IncomingMessage
from .store import Store

log = logging.getLogger(__name__)


def poll_once(store: Store, gmail: Any) -> list[IncomingMessage]:
    hotels = [h for h in store.list_hotels() if h["contacted_at"]]
    if not hotels:
        return []
    own = gmail.own_email
    by_email = {h["email"]: h for h in hotels}
    by_thread = {h["thread_id"]: h for h in hotels if h.get("thread_id")}
    known = store.processed_ids()

    found: dict[str, IncomingMessage] = {}
    for thread_id in by_thread:
        for msg in gmail.fetch_thread_messages(thread_id):
            found[msg.id] = msg
    for msg in gmail.fetch_from_senders(list(by_email), skip_ids=known):
        found[msg.id] = msg

    new: list[IncomingMessage] = []
    for msg in sorted(found.values(), key=lambda m: m.date):
        if msg.from_email == own or msg.id in known:
            continue
        hotel = by_thread.get(msg.thread_id) or by_email.get(msg.from_email)
        if hotel is None:
            continue
        store.record_inbound(hotel["id"], msg)
        new.append(msg)
    if new:
        log.info("получено новых писем: %d", len(new))
    return new
