import imaplib
import smtplib
from email import policy
from email.message import EmailMessage

import pytest

from conftest import make_msg
from hotelbot.config import Config, ConfigError
from hotelbot.inbox import poll_once
from hotelbot.mail_client import MailClient, parse_email
from hotelbot.telegram import extract_owner_text, split_text


def raw_email(frm, subject, body, message_id, refs=None, in_reply_to=None, charset=None, date="Tue, 27 Oct 2026 10:00:00 +0300"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"], msg["Date"] = frm, "me@example.org", subject, message_id, date
    if refs:
        msg["References"] = " ".join(refs)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(body, charset=charset) if charset else msg.set_content(body)
    return msg.as_bytes(policy=policy.SMTP)


# ---- опрос почты ----------------------------------------------------------------


def test_poll_records_new_messages_once_and_matches_replies_by_headers(store, outbox, mail):
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    outbox.send_first(hotel["id"], "Запрос", "Привет")  # Message-ID нашего письма: <sent1@fake>
    mail.inbox = [
        make_msg("g1", sender="info@aurora.com"),
        make_msg("g2", sender="me@example.org", body="наше собственное письмо"),
        make_msg("g3", sender="spam@random.com", body="купите"),
        make_msg("g4", sender="reservations@aurora.com", body="пишет другой сотрудник отеля", references=["<sent1@fake>"]),
        make_msg("g5", sender="spam@random.com", body="ответ на чужое письмо", references=["<foreign@x>"]),
    ]
    first = poll_once(store, mail)
    assert sorted(m.id for m in first) == ["g1", "g4"]
    assert poll_once(store, mail) == []
    assert len(store.all_unread()) == 2
    transcript = store.read_transcript(hotel["id"])
    assert "пишет другой сотрудник" in transcript
    assert "купите" not in transcript and "чужое письмо" not in transcript


def test_poll_does_nothing_before_first_contact(store, mail):
    store.add_hotel("Aurora", "info@aurora.com")
    mail.inbox = [make_msg("g1", sender="info@aurora.com")]
    assert poll_once(store, mail) == []


def test_dry_run_messages_are_not_used_for_matching(make_cfg, store, mail):
    from hotelbot.outbox import Outbox

    outbox = Outbox(make_cfg(dry_run=True), store, mail, delay=0)
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    outbox.send_first(hotel["id"], "Запрос", "Привет")
    assert store.sent_message_ids() == {}


# ---- разбор писем ---------------------------------------------------------------


def test_parse_email_plain_cp1251_headers_and_references():
    raw = raw_email(
        "Aurora Hotel <Info@Aurora.com>",
        "Re:  Запрос",
        "Есть номер с джакузи",
        "<abc@aurora>",
        refs=["<root@me>", "<sent2@me>"],
        in_reply_to="<sent2@me>",
        charset="cp1251",
    )
    msg = parse_email(raw, "fallback")
    assert msg.from_email == "info@aurora.com"
    assert msg.body == "Есть номер с джакузи"
    assert msg.subject == "Re: Запрос"
    assert msg.id == msg.message_id_header == "<abc@aurora>"
    assert msg.references == ["<root@me>", "<sent2@me>"]
    assert msg.thread_id == "<root@me>"
    assert msg.date.utcoffset().total_seconds() == 3 * 3600


def test_parse_email_html_only_and_missing_message_id():
    message = EmailMessage()
    message["From"], message["Subject"] = "a@a.com", "Цены"
    message.set_content("fallback")
    message.clear_content()
    message.set_content("<p>Цена &amp; условия</p><script>x()</script><br>Итого 500</p>", subtype="html")
    msg = parse_email(message.as_bytes(policy=policy.SMTP), "imap-1-5")
    assert "Цена & условия" in msg.body and "Итого 500" in msg.body and "x()" not in msg.body
    assert msg.id == "imap-1-5" and msg.thread_id == "imap-1-5" and msg.message_id_header is None


# ---- SMTP / IMAP клиент (на подменённых imaplib и smtplib) ----------------------


class FakeSmtp:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.starttls_called, self.sent, self.quit_called = host, port, False, [], False
        self.ssl = type(self).__name__ == "FakeSmtpSsl"
        FakeSmtp.instances.append(self)

    def starttls(self):
        self.starttls_called = True

    def login(self, user, password):
        self.creds = (user, password)

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        self.quit_called = True


class FakeSmtpSsl(FakeSmtp):
    pass


class FakeImap:
    mailbox: list = []
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.calls, self.logged_out = [], False
        FakeImap.instances.append(self)

    def login(self, user, password):
        self.creds = (user, password)
        return "OK", [b""]

    def select(self, name, readonly=False):
        self.selected = (name, readonly)
        return "OK", [b"3"]

    def response(self, name):
        return name, [b"777"]

    def uid(self, command, *args):
        self.calls.append((command, args))
        boxes = dict(FakeImap.mailbox)
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in boxes)]
        uidset, spec = args
        uids = [int(x) for x in uidset.decode().split(",")] if isinstance(uidset, bytes) else [int(uidset)]
        data = []
        for uid in uids:
            raw = boxes[uid]
            if "HEADER.FIELDS" in spec:
                chunk = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                data.append((f"{uid} (UID {uid} BODY[HEADER.FIELDS (X)] {{{len(chunk)}}}".encode(), chunk))
            else:
                data.append((f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
            data.append(b")")
        return "OK", data

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


@pytest.fixture
def client(monkeypatch):
    FakeSmtp.instances, FakeImap.instances, FakeImap.mailbox = [], [], []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtpSsl)
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    return MailClient("Me@Example.org", "me", "secret", "imap.example.org", smtp_host="smtp.example.org", from_name="Влад")


def test_send_sets_threading_headers_and_uses_ssl_on_465(client):
    first = client.send("info@aurora.com", "Запрос", "Привет")
    reply = client.send("info@aurora.com", "Re: Запрос", "Уточнение", thread_id=first["thread_id"], in_reply_to="<hotel1@aurora>")
    assert first["thread_id"] == first["id"]
    assert reply["thread_id"] == first["id"] and reply["id"] != first["id"]
    smtp = FakeSmtp.instances[0]
    assert smtp.ssl and not smtp.starttls_called and smtp.creds == ("me", "secret") and smtp.quit_called
    message = smtp.sent[0]
    assert message["To"] == "info@aurora.com" and "me@example.org" in message["From"]
    assert message["Message-ID"] == first["id"] and message["Message-ID"].endswith("@example.org>")
    second = FakeSmtp.instances[1].sent[0]
    assert second["In-Reply-To"] == "<hotel1@aurora>"
    assert second["References"] == f"{first['id']} <hotel1@aurora>"


def test_send_uses_starttls_on_587(client):
    client.smtp_port = 587
    client.send("info@aurora.com", "S", "B")
    smtp = FakeSmtp.instances[0]
    assert not smtp.ssl and smtp.starttls_called and smtp.port == 587


def test_fetch_relevant_downloads_only_matching_mail_readonly_and_caches_headers(client):
    FakeImap.mailbox = [
        (1, raw_email("Hotel <info@aurora.com>", "Re: Запрос", "Есть джакузи", "<h1@aurora>")),
        (2, raw_email("promo@shop.com", "Скидки", "Купите", "<s1@shop>")),
        (3, raw_email("reservations@aurora.com", "Re: Запрос", "От коллеги", "<h2@aurora>", refs=["<sent1@me>"])),
        (4, raw_email("info@aurora.com", "Старое", "Уже обработано", "<old@aurora>")),
    ]
    found = client.fetch_relevant({"info@aurora.com"}, {"<sent1@me>"}, {"<old@aurora>"})
    assert sorted(m.id for m in found) == ["<h1@aurora>", "<h2@aurora>"]
    conn = FakeImap.instances[0]
    assert conn.selected == ("INBOX", True) and conn.logged_out and conn.creds == ("me", "secret")
    search = next(args for cmd, args in conn.calls if cmd == "SEARCH")
    assert search[1] == "SINCE" and len(search[2].split("-")) == 3
    full_fetches = [args for cmd, args in conn.calls if cmd == "FETCH" and args[1] == "(BODY.PEEK[])"]
    assert len(full_fetches) == 2  # письма 2 и 4 целиком не скачивались
    header_calls = len([1 for cmd, args in conn.calls if cmd == "FETCH" and "HEADER.FIELDS" in args[1]])
    client.fetch_relevant({"info@aurora.com"}, {"<sent1@me>"}, {"<old@aurora>", "<h1@aurora>", "<h2@aurora>"})
    second_conn = FakeImap.instances[1]
    assert not [1 for cmd, args in second_conn.calls if cmd == "FETCH"]  # заголовки взяты из кэша
    assert header_calls >= 1


def test_check_reports_success_and_does_not_send(client):
    FakeImap.mailbox = [(1, raw_email("a@a.com", "S", "B", "<x@a>")), (2, raw_email("b@b.com", "S", "B", "<y@b>"))]
    report = client.check()
    assert report == {"imap": "ок, писем в INBOX: 3", "smtp": "ок"}
    assert FakeSmtp.instances[0].sent == [] and FakeImap.instances[0].selected == ("INBOX", True)


def test_check_reports_login_errors_without_leaking_password(client, monkeypatch):
    def deny(self, user, password):
        raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    def smtp_deny(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr(FakeImap, "login", deny)
    monkeypatch.setattr(FakeSmtp, "login", smtp_deny)
    report = client.check()
    assert "AUTHENTICATIONFAILED" in report["imap"] and "SMTPAuthenticationError" in report["smtp"]
    assert "secret" not in str(report)


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


def base_env(tmp_path):
    return {
        "TELEGRAM_BOT_TOKEN": "1:a",
        "OWNER_TELEGRAM_ID": "42",
        "ANTHROPIC_API_KEY": "k",
        "MAIL_ADDRESS": "me@yandex.ru",
        "MAIL_PASSWORD": "app-password",
        "IMAP_HOST": "imap.yandex.ru",
        "SMTP_HOST": "smtp.yandex.ru",
        "HOTELBOT_DATA_DIR": str(tmp_path / "d"),
    }


def test_config_requires_secrets_and_defaults_to_dry_run(tmp_path):
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env({})
    env = base_env(tmp_path)
    cfg = Config.from_env(env)
    assert cfg.dry_run is True and cfg.owner_id == 42
    assert (cfg.imap_port, cfg.smtp_port, cfg.mail_user) == (993, 465, "me@yandex.ru")
    assert Config.from_env({**env, "DRY_RUN": "0", "MAIL_USER": "me", "SMTP_PORT": "587"}).smtp_port == 587
    with pytest.raises(ConfigError):
        Config.from_env({**env, "OWNER_TELEGRAM_ID": "abc"})
    with pytest.raises(ConfigError, match="IMAP_HOST"):
        Config.from_env({k: v for k, v in env.items() if k != "IMAP_HOST"})
    with pytest.raises(ConfigError, match="MAIL_ADDRESS"):
        Config.from_env({**env, "MAIL_ADDRESS": "no-at-sign"})


def test_secrets_are_not_shown_in_config_repr(tmp_path):
    cfg = Config.from_env(base_env(tmp_path))
    assert "app-password" not in repr(cfg) and "1:a" not in repr(cfg)
