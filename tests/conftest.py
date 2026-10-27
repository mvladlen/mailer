from datetime import datetime, timedelta, timezone

import pytest

from hotelbot.config import Config
from hotelbot.gmail_client import IncomingMessage
from hotelbot.outbox import Outbox
from hotelbot.store import Store


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class FakeGmail:
    own_email = "me@gmail.com"

    def __init__(self):
        self.sent = []
        self.inbox = []

    def send(self, to, subject, body, thread_id=None, in_reply_to=None):
        self.sent.append(
            {"to": to, "subject": subject, "body": body, "thread_id": thread_id, "in_reply_to": in_reply_to}
        )
        return {"id": f"sent{len(self.sent)}", "thread_id": thread_id or f"thread{len(self.sent)}"}

    def fetch_thread_messages(self, thread_id):
        return [m for m in self.inbox if m.thread_id == thread_id]

    def fetch_from_senders(self, senders, skip_ids=None):
        skip = skip_ids or set()
        return [m for m in self.inbox if m.from_email in senders and m.id not in skip]


def make_msg(msg_id="g1", thread_id="thread1", sender="info@aurora.com", subject="Re: Запрос", body="Да, есть.", when=None):
    return IncomingMessage(
        id=msg_id,
        thread_id=thread_id,
        from_email=sender,
        subject=subject,
        date=when or datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc),
        body=body,
        message_id_header=f"<{msg_id}@mail>",
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_cfg(tmp_path):
    def factory(**overrides):
        values = dict(
            telegram_token="123:abc",
            owner_id=42,
            data_dir=tmp_path / "data",
            gmail_token_path=tmp_path / "token.json",
            dry_run=False,
            max_emails_per_hour=5,
            max_emails_per_day=10,
        )
        values.update(overrides)
        return Config(**values)

    return factory


@pytest.fixture
def cfg(make_cfg):
    return make_cfg()


@pytest.fixture
def store(cfg, clock):
    return Store(cfg.data_dir, clock=clock)


@pytest.fixture
def gmail():
    return FakeGmail()


@pytest.fixture
def outbox(cfg, store, gmail):
    return Outbox(cfg, store, gmail, delay=0)
