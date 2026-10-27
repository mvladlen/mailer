"""Минимальный клиент Telegram Bot API (long polling, только исходящие соединения)."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)
CHUNK = 3900


def extract_owner_text(update: dict, owner_id: int) -> str | None:
    """Возвращает текст сообщения, только если оно пришло лично от владельца."""
    message = update.get("message")
    if not message:
        return None
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if sender.get("id") != owner_id or chat.get("id") != owner_id or chat.get("type") != "private":
        log.warning("сообщение от постороннего пользователя id=%s проигнорировано", sender.get("id"))
        return None
    text = message.get("text")
    return text.strip() if isinstance(text, str) and text.strip() else None


def split_text(text: str, limit: int = CHUNK) -> list[str]:
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class TelegramApi:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=15.0))

    async def _call(self, method: str, **kwargs) -> dict:
        response = await self._client.post(f"{self._base}/{method}", **kwargs)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', 'ошибка')}")
        return data

    async def prepare(self) -> None:
        # long polling не работает, если у бота остался вебхук
        await self._call("deleteWebhook", data={"drop_pending_updates": "false"})

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        params: dict = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        data = await self._call("getUpdates", data=params)
        return data["result"]

    async def send_message(self, chat_id: int, text: str) -> None:
        for chunk in split_text(text or "(пусто)"):
            await self._call("sendMessage", data={"chat_id": chat_id, "text": chunk})

    async def send_document(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
        await self._call(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (filename, content, "text/markdown")},
        )
