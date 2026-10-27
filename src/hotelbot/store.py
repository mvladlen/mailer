"""Хранилище состояния: JSON для машинных данных, Markdown для переписки и фактов.

Структура каталога данных:
    facts.md            факты по поездке и ответы владельца (читает Claude)
    hotels.json         реестр отелей: email, статус, предложение, статусы вопросов
    questions.json      список вопросов к отелям
    pending.json        вопросы, ожидающие ответа владельца
    batch.json          черновик первой рассылки, ожидающий /send
    state.json          id уже обработанных писем
    sent.jsonl          журнал отправок (лимиты, защита от дублей, сопоставление ответов)
    chat.jsonl          недавний диалог с владельцем
    hotels/<id>.md      полная переписка с отелем (только дописывается)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")
QUESTION_STATES = ("not_asked", "asked", "answered", "declined", "unclear")
HOTEL_STATUSES = ("new", "contacted", "replied", "waiting_owner", "declined", "done")
OFFER_FIELDS = (
    "jacuzzi",
    "room_type",
    "price_per_night",
    "total_price_with_taxes",
    "currency",
    "discount",
    "cancellation",
    "breakfast",
    "valid_until",
    "notes",
)
MAX_UNREAD_BODY = 20000
MAX_PROCESSED_IDS = 5000


class StoreError(Exception):
    """Ошибка, текст которой можно показать модели."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def body_hash(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _slug(text: str) -> str:
    return re.sub(r"[\W_]+", "-", text.lower()).strip("-")[:40]


class Store:
    def __init__(self, data_dir: Path | str, clock: Callable[[], datetime] = utcnow):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        (self.dir / "hotels").mkdir(exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        facts = self.dir / "facts.md"
        if not facts.exists():
            facts.write_text("# Факты по поездке\n\n", encoding="utf-8")

    # ---- низкоуровневые операции -------------------------------------------------

    def _load(self, name: str, default: Any) -> Any:
        path = self.dir / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, name: str, data: Any) -> None:
        path = self.dir / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _hotels(self) -> dict[str, dict]:
        return self._load("hotels.json", {})

    def _questions(self) -> list[dict]:
        return self._load("questions.json", [])

    # ---- отели -------------------------------------------------------------------

    def add_hotel(self, name: str, email: str) -> dict:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise StoreError(f"некорректный email: {email!r}")
        name = " ".join(name.split())
        if not name:
            name = email.split("@", 1)[1].split(".")[0].title()
        with self._lock:
            hotels = self._hotels()
            for hotel in hotels.values():
                if hotel["email"] == email:
                    return hotel
            base = _slug(name) or _slug(email.split("@")[0]) or "hotel"
            hotel_id, n = base, 2
            while hotel_id in hotels:
                hotel_id = f"{base}-{n}"
                n += 1
            hotel = {
                "id": hotel_id,
                "name": name,
                "email": email,
                "status": "new",
                "thread_id": None,
                "last_subject": None,
                "last_inbound_message_id": None,
                "contacted_at": None,
                "offer": {},
                "questions": {
                    q["id"]: {"state": "not_asked", "note": ""} for q in self._questions()
                },
                "unread": [],
            }
            hotels[hotel_id] = hotel
            self._save("hotels.json", hotels)
            return hotel

    def list_hotels(self) -> list[dict]:
        return list(self._hotels().values())

    def get_hotel(self, hotel_id: str) -> dict:
        hotel = self._hotels().get(hotel_id)
        if hotel is None:
            raise StoreError(f"отель с id {hotel_id!r} не найден")
        return hotel

    def resolve_hotel(self, ref: str) -> dict:
        """Находит отель по id, email или названию (точно, затем по подстроке)."""
        ref_norm = ref.strip().lower()
        hotels = self.list_hotels()
        for hotel in hotels:
            if ref_norm in (hotel["id"].lower(), hotel["email"], hotel["name"].lower()):
                return hotel
        matches = [h for h in hotels if ref_norm and ref_norm in h["name"].lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise StoreError(f"под {ref!r} подходит несколько отелей: " + ", ".join(h["id"] for h in matches))
        raise StoreError(f"отель {ref!r} не найден")

    def _update_hotel(self, hotel_id: str, mutate: Callable[[dict], None]) -> dict:
        with self._lock:
            hotels = self._hotels()
            hotel = hotels.get(hotel_id)
            if hotel is None:
                raise StoreError(f"отель с id {hotel_id!r} не найден")
            mutate(hotel)
            self._save("hotels.json", hotels)
            return hotel

    def set_status(self, hotel_id: str, status: str) -> dict:
        if status not in HOTEL_STATUSES:
            raise StoreError(f"статус должен быть одним из: {', '.join(HOTEL_STATUSES)}")
        return self._update_hotel(hotel_id, lambda h: h.__setitem__("status", status))

    def set_offer(self, hotel_id: str, **fields: str | None) -> dict:
        unknown = set(fields) - set(OFFER_FIELDS)
        if unknown:
            raise StoreError("неизвестные поля предложения: " + ", ".join(sorted(unknown)))
        clean = {k: str(v).strip() for k, v in fields.items() if v is not None and str(v).strip()}

        def mutate(hotel: dict) -> None:
            hotel["offer"].update(clean)
            hotel["offer"]["updated_at"] = iso(self.clock())

        return self._update_hotel(hotel_id, mutate)

    # ---- вопросы -----------------------------------------------------------------

    def add_question(self, text: str) -> dict:
        text = " ".join(text.split())
        if not text:
            raise StoreError("пустой вопрос")
        with self._lock:
            questions = self._questions()
            for q in questions:
                if q["text"].lower() == text.lower():
                    return q
            question = {"id": f"q{len(questions) + 1}", "text": text, "created_at": iso(self.clock())}
            questions.append(question)
            self._save("questions.json", questions)
            hotels = self._hotels()
            for hotel in hotels.values():
                hotel["questions"].setdefault(question["id"], {"state": "not_asked", "note": ""})
            self._save("hotels.json", hotels)
            return question

    def list_questions(self) -> list[dict]:
        return self._questions()

    def set_question_state(self, hotel_id: str, question_id: str, state: str, note: str = "") -> dict:
        if state not in QUESTION_STATES:
            raise StoreError(f"состояние должно быть одним из: {', '.join(QUESTION_STATES)}")
        if question_id not in {q["id"] for q in self._questions()}:
            raise StoreError(f"вопрос {question_id!r} не найден")

        def mutate(hotel: dict) -> None:
            hotel["questions"][question_id] = {"state": state, "note": note.strip()}

        return self._update_hotel(hotel_id, mutate)

    # ---- факты -------------------------------------------------------------------

    def read_facts(self) -> str:
        return (self.dir / "facts.md").read_text(encoding="utf-8")

    def append_fact(self, text: str) -> None:
        text = " ".join(text.split())
        if not text:
            raise StoreError("пустой факт")
        with self._lock:
            with (self.dir / "facts.md").open("a", encoding="utf-8") as fh:
                fh.write(f"- [{self.clock():%Y-%m-%d}] {text}\n")

    # ---- ожидающие вопросы владельцу ---------------------------------------------

    def add_pending(self, hotel_id: str, text: str) -> dict:
        self.get_hotel(hotel_id)
        text = " ".join(text.split())
        if not text:
            raise StoreError("пустой вопрос")
        with self._lock:
            items = self._load("pending.json", [])
            for item in items:
                if not item["resolved"] and item["hotel_id"] == hotel_id and item["text"] == text:
                    return item
            item = {
                "id": f"p{len(items) + 1}",
                "hotel_id": hotel_id,
                "text": text,
                "created_at": iso(self.clock()),
                "resolved": False,
                "answer": None,
            }
            items.append(item)
            self._save("pending.json", items)
        self.set_status(hotel_id, "waiting_owner")
        return item

    def list_pending(self, open_only: bool = True) -> list[dict]:
        items = self._load("pending.json", [])
        return [i for i in items if not i["resolved"]] if open_only else items

    def resolve_pending(self, pending_ids: list[str], answer: str) -> list[dict]:
        answer = " ".join(answer.split())
        if not answer:
            raise StoreError("пустой ответ")
        with self._lock:
            items = self._load("pending.json", [])
            by_id = {i["id"]: i for i in items}
            missing = [p for p in pending_ids if p not in by_id]
            if missing:
                raise StoreError("не найдены вопросы: " + ", ".join(missing))
            resolved = []
            for pid in pending_ids:
                item = by_id[pid]
                if item["resolved"]:
                    continue
                item["resolved"] = True
                item["answer"] = answer
                resolved.append(item)
            self._save("pending.json", items)
            if resolved:
                questions = "; ".join(sorted({i["text"] for i in resolved}))
                self.append_fact(f"Вопрос: {questions} Ответ владельца: {answer}")
            return resolved

    # ---- черновик первой рассылки ------------------------------------------------

    def set_batch(self, subject: str, body_template: str, hotel_ids: list[str]) -> dict:
        for hotel_id in hotel_ids:
            self.get_hotel(hotel_id)
        batch = {
            "subject": " ".join(subject.split()),
            "body_template": body_template.strip(),
            "hotel_ids": list(hotel_ids),
            "created_at": iso(self.clock()),
        }
        self._save("batch.json", batch)
        return batch

    def get_batch(self) -> dict | None:
        return self._load("batch.json", None)

    def clear_batch(self) -> None:
        path = self.dir / "batch.json"
        if path.exists():
            path.unlink()

    # ---- переписка ---------------------------------------------------------------

    def transcript_path(self, hotel_id: str) -> Path:
        self.get_hotel(hotel_id)
        return self.dir / "hotels" / f"{hotel_id}.md"

    def append_transcript(
        self, hotel_id: str, direction: str, when: datetime, subject: str, body: str, note: str = ""
    ) -> None:
        hotel = self.get_hotel(hotel_id)
        suffix = f" [{note}]" if note else ""
        entry = (
            f"## {when.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC · {direction} · "
            f"{hotel['name']} <{hotel['email']}>{suffix}\n"
            f"Тема: {subject}\n\n{body.strip()}\n\n---\n\n"
        )
        with self._lock:
            with self.transcript_path(hotel_id).open("a", encoding="utf-8") as fh:
                fh.write(entry)

    def read_transcript(self, hotel_id: str, tail_chars: int | None = None) -> str:
        path = self.transcript_path(hotel_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        if tail_chars and len(text) > tail_chars:
            return "…(начало пропущено)…\n" + text[-tail_chars:]
        return text

    # ---- входящая почта ----------------------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        return message_id in set(self._load("state.json", {}).get("processed", []))

    def processed_ids(self) -> set[str]:
        return set(self._load("state.json", {}).get("processed", []))

    def record_inbound(self, hotel_id: str, msg: Any) -> None:
        """Сохраняет входящее письмо: в переписку, в непрочитанные и в обработанные."""
        self.append_transcript(hotel_id, "IN", msg.date, msg.subject, msg.body, note=f"от {msg.from_email}")
        body = msg.body if len(msg.body) <= MAX_UNREAD_BODY else msg.body[:MAX_UNREAD_BODY] + "\n…(обрезано)"

        def mutate(hotel: dict) -> None:
            hotel["unread"].append(
                {
                    "message_id": msg.id,
                    "from": msg.from_email,
                    "subject": msg.subject,
                    "received": iso(msg.date),
                    "text": body,
                }
            )
            if hotel["status"] in ("new", "contacted"):
                hotel["status"] = "replied"
            if not hotel.get("thread_id"):
                hotel["thread_id"] = msg.thread_id
            hotel["last_inbound_message_id"] = msg.message_id_header or hotel.get("last_inbound_message_id")
            hotel["last_subject"] = re.sub(r"^(re|fwd?)\s*:\s*", "", msg.subject, flags=re.I).strip() or hotel.get(
                "last_subject"
            )

        with self._lock:
            self._update_hotel(hotel_id, mutate)
            state = self._load("state.json", {})
            processed = state.setdefault("processed", [])
            processed.append(msg.id)
            state["processed"] = processed[-MAX_PROCESSED_IDS:]
            self._save("state.json", state)

    def mark_read(self, hotel_id: str, message_id: str) -> None:
        self._update_hotel(
            hotel_id,
            lambda h: h.__setitem__("unread", [u for u in h["unread"] if u["message_id"] != message_id]),
        )

    def all_unread(self) -> list[dict]:
        result = []
        for hotel in self.list_hotels():
            for item in hotel["unread"]:
                result.append({"hotel_id": hotel["id"], "hotel_name": hotel["name"], **item})
        return result

    # ---- журнал отправок ---------------------------------------------------------

    def log_send(self, hotel_id: str, kind: str, text_hash: str, message_id: str | None, dry_run: bool) -> None:
        record = {
            "ts": iso(self.clock()),
            "hotel_id": hotel_id,
            "kind": kind,
            "hash": text_hash,
            "message_id": message_id,
            "dry_run": dry_run,
        }
        with self._lock:
            with (self.dir / "sent.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _sent_records(self) -> list[dict]:
        path = self.dir / "sent.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def sent_message_ids(self) -> dict[str, str]:
        """Message-ID наших реально отправленных писем -> id отеля (для сопоставления ответов)."""
        return {
            r["message_id"]: r["hotel_id"]
            for r in self._sent_records()
            if not r["dry_run"] and r.get("message_id")
        }

    def count_sent_since(self, delta: timedelta) -> int:
        threshold = iso(self.clock() - delta)
        return sum(1 for r in self._sent_records() if not r["dry_run"] and r["ts"] >= threshold)

    def was_sent_recently(self, hotel_id: str, text_hash: str, delta: timedelta = timedelta(hours=24)) -> bool:
        threshold = iso(self.clock() - delta)
        return any(
            r["hotel_id"] == hotel_id and r["hash"] == text_hash and r["ts"] >= threshold
            for r in self._sent_records()
        )

    def mark_contacted(self, hotel_id: str, thread_id: str | None, subject: str) -> None:
        def mutate(hotel: dict) -> None:
            if not hotel["contacted_at"]:
                hotel["contacted_at"] = iso(self.clock())
            if hotel["status"] == "new":
                hotel["status"] = "contacted"
            if thread_id and not hotel.get("thread_id"):
                hotel["thread_id"] = thread_id
            if not hotel.get("last_subject"):
                hotel["last_subject"] = subject

        self._update_hotel(hotel_id, mutate)

    # ---- диалог с владельцем -----------------------------------------------------

    def append_chat(self, role: str, text: str) -> None:
        record = {"ts": iso(self.clock()), "role": role, "text": text[:1500]}
        with self._lock:
            with (self.dir / "chat.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def recent_chat(self, n: int = 10) -> list[dict]:
        path = self.dir / "chat.jsonl"
        if not path.exists():
            return []
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines[-n:]]
