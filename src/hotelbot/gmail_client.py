"""Тонкая обёртка над Gmail API. Права: только чтение и отправка, удаления нет."""

from __future__ import annotations

import base64
import html
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

# Те же scopes использует scripts/gmail_auth.py: при изменении правьте оба места.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


@dataclass
class IncomingMessage:
    id: str
    thread_id: str
    from_email: str
    subject: str
    date: datetime
    body: str
    message_id_header: str | None


def _decode(data: str, charset: str | None) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _charset(part: dict) -> str | None:
    for header in part.get("headers", []) or []:
        if header.get("name", "").lower() == "content-type":
            match = re.search(r"charset=\"?([\w-]+)", header.get("value", ""), flags=re.I)
            if match:
                return match.group(1)
    return None


def _walk(part: dict, plain: list[str], rich: list[str]) -> None:
    mime = part.get("mimeType", "")
    data = (part.get("body") or {}).get("data")
    if data and mime == "text/plain":
        plain.append(_decode(data, _charset(part)))
    elif data and mime == "text/html":
        rich.append(_decode(data, _charset(part)))
    for child in part.get("parts", []) or []:
        _walk(child, plain, rich)


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_message(raw: dict) -> IncomingMessage:
    payload = raw.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain: list[str] = []
    rich: list[str] = []
    _walk(payload, plain, rich)
    body = "\n".join(plain).strip() or _html_to_text("\n".join(rich))
    try:
        date = parsedate_to_datetime(headers["date"])
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        date = datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, tz=timezone.utc)
    return IncomingMessage(
        id=raw["id"],
        thread_id=raw["threadId"],
        from_email=parseaddr(headers.get("from", ""))[1].lower(),
        subject=" ".join(headers.get("subject", "(без темы)").split()),
        date=date,
        body=body,
        message_id_header=headers.get("message-id"),
    )


class GmailClient:
    def __init__(self, token_path: Path | str):
        self.token_path = Path(token_path)
        self._service: Any = None
        self._own_email: str | None = None
        self._lock = threading.Lock()  # клиент googleapiclient не потокобезопасен

    def _svc(self) -> Any:
        if self._service is None:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            if not creds.valid:
                if creds.refresh_token:
                    creds.refresh(Request())
                else:
                    raise RuntimeError("токен Gmail недействителен: пройдите авторизацию заново")
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    @property
    def own_email(self) -> str:
        if self._own_email is None:
            with self._lock:
                profile = self._svc().users().getProfile(userId="me").execute()
            self._own_email = profile["emailAddress"].lower()
        return self._own_email

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(body)
        payload: dict[str, Any] = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
        if thread_id:
            payload["threadId"] = thread_id
        with self._lock:
            result = self._svc().users().messages().send(userId="me", body=payload).execute()
        return {"id": result["id"], "thread_id": result["threadId"]}

    def fetch_thread_messages(self, thread_id: str) -> list[IncomingMessage]:
        from googleapiclient.errors import HttpError

        try:
            with self._lock:
                thread = self._svc().users().threads().get(userId="me", id=thread_id, format="full").execute()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                return []
            raise
        return [parse_message(m) for m in thread.get("messages", [])]

    def fetch_from_senders(self, senders: list[str], skip_ids: set[str] | None = None) -> list[IncomingMessage]:
        if not senders:
            return []
        skip_ids = skip_ids or set()
        query = "in:inbox newer_than:14d from:(" + " OR ".join(senders) + ")"
        with self._lock:
            listing = self._svc().users().messages().list(userId="me", q=query, maxResults=50).execute()
        result = []
        for ref in listing.get("messages", []):
            if ref["id"] in skip_ids:
                continue
            with self._lock:
                raw = self._svc().users().messages().get(userId="me", id=ref["id"], format="full").execute()
            result.append(parse_message(raw))
        return result
