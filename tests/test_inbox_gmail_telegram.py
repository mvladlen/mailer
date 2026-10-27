import base64
from datetime import timezone

from conftest import make_msg
from hotelbot.config import Config, ConfigError
from hotelbot.gmail_client import parse_message
from hotelbot.inbox import poll_once
from hotelbot.telegram import extract_owner_text, split_text

import pytest


def b64(text: str, encoding: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip("=")


# ---- опрос почты ----------------------------------------------------------------


def test_poll_records_new_messages_once_and_ignores_strangers(store, outbox, gmail):
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    outbox.send_first(hotel["id"], "Запрос", "Привет")
    gmail.inbox = [
        make_msg("g1", "thread1", "info@aurora.com"),
        make_msg("g2", "thread1", "me@gmail.com", body="наше собственное письмо"),
        make_msg("g3", "thread9", "spam@random.com", body="купите"),
        make_msg("g4", "thread1", "reservations@aurora.com", body="пишет другой сотрудник отеля"),
    ]
    first = poll_once(store, gmail)
    assert sorted(m.id for m in first) == ["g1", "g4"]
    assert poll_once(store, gmail) == []
    assert len(store.all_unread()) == 2
    transcript = store.read_transcript(hotel["id"])
    assert "пишет другой сотрудник" in transcript and "купите" not in transcript


def test_poll_does_nothing_before_first_contact(store, gmail):
    store.add_hotel("Aurora", "info@aurora.com")
    gmail.inbox = [make_msg("g1", "t", "info@aurora.com")]
    assert poll_once(store, gmail) == []


# ---- разбор писем Gmail ---------------------------------------------------------


def test_parse_message_plain_cp1251_and_html_fallback():
    plain = {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Aurora Hotel <Info@Aurora.com>"},
                {"name": "Subject", "value": "Re:  Запрос\n"},
                {"name": "Date", "value": "Tue, 27 Oct 2026 10:00:00 +0300"},
                {"name": "Message-ID", "value": "<abc@aurora>"},
                {"name": "Content-Type", "value": 'text/plain; charset="windows-1251"'},
            ],
            "body": {"data": b64("Есть номер с джакузи", "cp1251")},
        },
    }
    msg = parse_message(plain)
    assert msg.from_email == "info@aurora.com"
    assert msg.body == "Есть номер с джакузи"
    assert msg.subject == "Re: Запрос"
    assert msg.date.astimezone(timezone.utc).hour == 7

    html_only = {
        "id": "m2",
        "threadId": "t1",
        "internalDate": "1790000000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": "a@a.com"}],
            "parts": [
                {"mimeType": "text/html", "body": {"data": b64("<p>Цена &amp; условия</p><script>x()</script><br>Итого 500</p>")}}
            ],
        },
    }
    msg = parse_message(html_only)
    assert "Цена & условия" in msg.body and "Итого 500" in msg.body and "x()" not in msg.body


# ---- Telegram -------------------------------------------------------------------


def update(user_id=42, chat_id=42, chat_type="private", text="привет"):
    return {"update_id": 1, "message": {"from": {"id": user_id}, "chat": {"id": chat_id, "type": chat_type}, "text": text}}


def test_only_owner_private_messages_are_accepted():
    assert extract_owner_text(update(), 42) == "привет"
    assert extract_owner_text(update(user_id=7, chat_id=7), 42) is None
    assert extract_owner_text(update(chat_id=-100, chat_type="group"), 42) is None
    assert extract_owner_text(update(user_id=7), 42) is None
    assert extract_owner_text({"update_id": 2, "edited_message": {}}, 42) is None
    assert extract_owner_text(update(text="   "), 42) is None
    assert extract_owner_text({"update_id": 3, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}}}, 42) is None


def test_split_text_respects_limit_and_loses_nothing():
    text = "\n".join(f"строка {i} " + "x" * 80 for i in range(200))
    chunks = split_text(text, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")
    assert split_text("коротко") == ["коротко"]


# ---- конфигурация ---------------------------------------------------------------


def test_config_requires_secrets_and_defaults_to_dry_run(tmp_path):
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env({})
    env = {"TELEGRAM_BOT_TOKEN": "1:a", "OWNER_TELEGRAM_ID": "42", "ANTHROPIC_API_KEY": "k", "HOTELBOT_DATA_DIR": str(tmp_path / "d")}
    cfg = Config.from_env(env)
    assert cfg.dry_run is True and cfg.owner_id == 42
    assert cfg.gmail_token_path == tmp_path / "gmail_token.json"
    assert Config.from_env({**env, "DRY_RUN": "0"}).dry_run is False
    with pytest.raises(ConfigError):
        Config.from_env({**env, "OWNER_TELEGRAM_ID": "abc"})
