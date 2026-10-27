"""Почта по обычному IMAP/SMTP с паролем приложения (без OAuth и облачных консолей).

* Отправка: SMTP. Мы сами задаём Message-ID, чтобы потом узнавать ответы по заголовкам
  In-Reply-To / References.
* Чтение: IMAP, папка INBOX открывается только для чтения, письма не помечаются прочитанными
  и не удаляются.
* Сначала скачиваются только заголовки; письмо целиком загружается, только если оно от
  отеля из реестра или является ответом на наше письмо.
"""

from __future__ import annotations

import email
import html
import imaplib
import logging
import re
import smtplib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime

log = logging.getLogger(__name__)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_HEADER_FIELDS = "MESSAGE-ID FROM IN-REPLY-TO REFERENCES"
_BATCH = 100
_ID_RE = re.compile(r"<[^<>\s]+>")


@dataclass
class IncomingMessage:
    id: str
    thread_id: str
    from_email: str
    subject: str
    date: datetime
    body: str
    message_id_header: str | None
    references: list[str] = field(default_factory=list)


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _references(msg: email.message.Message) -> list[str]:
    found: list[str] = []
    for name in ("References", "In-Reply-To"):
        for token in _ID_RE.findall(str(msg.get(name, ""))):
            if token not in found:
                found.append(token)
    return found


def parse_email(raw: bytes, fallback_id: str) -> IncomingMessage:
    msg = email.message_from_bytes(raw, policy=policy.default)
    message_id = str(msg.get("Message-ID", "")).strip() or None
    refs = _references(msg)
    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = part.get_payload(decode=True).decode("utf-8", errors="replace")
        body = content.strip() if part.get_content_type() == "text/plain" else _html_to_text(content)
    try:
        date = parsedate_to_datetime(str(msg["Date"]))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        date = datetime.now(timezone.utc)
    mid = message_id or fallback_id
    return IncomingMessage(
        id=mid,
        thread_id=refs[0] if refs else mid,
        from_email=parseaddr(str(msg.get("From", "")))[1].lower(),
        subject=" ".join(str(msg.get("Subject", "(без темы)")).split()),
        date=date,
        body=body,
        message_id_header=message_id,
        references=refs,
    )


class MailClient:
    def __init__(
        self,
        address: str,
        user: str,
        password: str,
        imap_host: str,
        imap_port: int = 993,
        smtp_host: str = "",
        smtp_port: int = 465,
        from_name: str = "",
        timeout: int = 30,
    ):
        self.address = address.strip().lower()
        self.user = user
        self._password = password
        self.imap_host, self.imap_port = imap_host, imap_port
        self.smtp_host, self.smtp_port = smtp_host, smtp_port
        self.from_name = from_name
        self.timeout = timeout
        self._lock = threading.Lock()
        self._meta_cache: dict[tuple[str, str], dict] = {}

    @property
    def own_email(self) -> str:
        return self.address

    # ---- отправка ----------------------------------------------------------------

    def _smtp(self) -> smtplib.SMTP:
        if self.smtp_port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self.timeout)
        else:
            server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)
            server.starttls()
        server.login(self.user, self._password)
        return server

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict:
        message = EmailMessage()
        message["From"] = formataddr((self.from_name, self.address)) if self.from_name else self.address
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message_id = make_msgid(domain=self.address.split("@", 1)[1])
        message["Message-ID"] = message_id
        references = [x for x in dict.fromkeys([thread_id, in_reply_to]) if x]
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = " ".join(references)
        message.set_content(body)
        with self._lock:
            server = self._smtp()
            try:
                server.send_message(message)
            finally:
                try:
                    server.quit()
                except Exception:  # соединение могло уже закрыться
                    pass
        return {"id": message_id, "thread_id": thread_id or message_id}

    # ---- чтение ------------------------------------------------------------------

    def _imap(self) -> imaplib.IMAP4:
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=self.timeout)
        conn.login(self.user, self._password)
        return conn

    @staticmethod
    def _check(result: tuple) -> list:
        status, data = result
        if status != "OK":
            raise RuntimeError(f"IMAP: команда завершилась со статусом {status}")
        return data

    def _headers(self, conn: imaplib.IMAP4, validity: str, uids: list[bytes]) -> dict[bytes, dict]:
        """Заголовки писем с кэшем: каждое письмо запрашивается один раз за время работы."""
        result: dict[bytes, dict] = {}
        missing = []
        for uid in uids:
            cached = self._meta_cache.get((validity, uid.decode()))
            if cached is not None:
                result[uid] = cached
            else:
                missing.append(uid)
        for start in range(0, len(missing), _BATCH):
            batch = missing[start : start + _BATCH]
            data = self._check(
                conn.uid("FETCH", b",".join(batch), f"(UID BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})])")
            )
            for item in data:
                if not isinstance(item, tuple):
                    continue
                uid_match = re.search(rb"UID (\d+)", item[0])
                if not uid_match:
                    continue
                parsed = email.message_from_bytes(item[1], policy=policy.default)
                meta = {
                    "message_id": str(parsed.get("Message-ID", "")).strip() or None,
                    "from": parseaddr(str(parsed.get("From", "")))[1].lower(),
                    "refs": _references(parsed),
                }
                self._meta_cache[(validity, uid_match.group(1).decode())] = meta
                result[uid_match.group(1)] = meta
        return result

    def fetch_relevant(
        self,
        senders: set[str],
        our_message_ids: set[str],
        skip_ids: set[str],
        days: int = 14,
    ) -> list[IncomingMessage]:
        """Письма за последние дни от отелей из реестра или ответы на наши письма."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        since_str = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
        found: list[IncomingMessage] = []
        with self._lock:
            conn = self._imap()
            try:
                self._check(conn.select("INBOX", readonly=True))
                validity_raw = conn.response("UIDVALIDITY")[1]
                validity = validity_raw[0].decode() if validity_raw and validity_raw[0] else "0"
                uids = self._check(conn.uid("SEARCH", None, "SINCE", since_str))[0].split()
                for uid, meta in self._headers(conn, validity, uids).items():
                    if meta["message_id"] and meta["message_id"] in skip_ids:
                        continue
                    if meta["from"] not in senders and not (set(meta["refs"]) & our_message_ids):
                        continue
                    data = self._check(conn.uid("FETCH", uid, "(BODY.PEEK[])"))
                    raw = next((i[1] for i in data if isinstance(i, tuple)), None)
                    if raw:
                        found.append(parse_email(raw, fallback_id=f"imap-{validity}-{uid.decode()}"))
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        return found
