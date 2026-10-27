from datetime import timedelta

import pytest

from conftest import make_msg
from hotelbot.outbox import Outbox, SendRefused


def contacted_hotel(store, outbox, name="Aurora", email="info@aurora.com"):
    hotel = store.add_hotel(name, email)
    outbox.send_first(hotel["id"], "Запрос", f"Здравствуйте, {name}!")
    return store.get_hotel(hotel["id"])


def test_dry_run_records_but_does_not_send(make_cfg, store, mail):
    outbox = Outbox(make_cfg(dry_run=True), store, mail, delay=0)
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    result = outbox.send_first(hotel["id"], "Запрос", "Привет")
    assert result == {"sent": False, "dry_run": True}
    assert mail.sent == []
    assert "DRY RUN" in store.read_transcript(hotel["id"])
    assert store.get_hotel(hotel["id"])["contacted_at"]
    assert store.count_sent_since(timedelta(days=1)) == 0  # в лимиты не входит


def test_real_send_goes_only_to_registered_address(store, mail, outbox):
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    outbox.send_first(hotel["id"], "Запрос\nBcc: evil@x.com", "Привет")
    assert mail.sent[0]["to"] == "info@aurora.com"
    assert "\n" not in mail.sent[0]["subject"]
    saved = store.get_hotel(hotel["id"])
    assert saved["thread_id"] == "<sent1@fake>" and saved["status"] == "contacted"
    with pytest.raises(Exception):
        outbox.send_first("unknown-id", "S", "B")


def test_first_email_cannot_be_sent_twice(store, outbox):
    hotel = contacted_hotel(store, outbox)
    with pytest.raises(SendRefused):
        outbox.send_first(hotel["id"], "Запрос", "Другой текст")


def test_reply_uses_thread_subject_and_blocks_duplicates(store, mail, outbox):
    hotel = contacted_hotel(store, outbox)
    store.record_inbound(hotel["id"], make_msg(thread_id="<sent1@fake>"))
    outbox.send_in_thread(hotel["id"], "Спасибо, уточните цену.")
    reply = mail.sent[-1]
    assert reply["thread_id"] == "<sent1@fake>"
    assert reply["subject"] == "Re: Запрос"
    assert reply["in_reply_to"] == "<g1@mail>"
    with pytest.raises(SendRefused, match="уже отправлялось"):
        outbox.send_in_thread(hotel["id"], "Спасибо,   уточните цену.")


def test_reply_requires_prior_contact(store, outbox):
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    with pytest.raises(SendRefused):
        outbox.send_in_thread(hotel["id"], "Привет")


def test_empty_and_oversized_bodies_refused(store, outbox):
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    with pytest.raises(SendRefused):
        outbox.send_first(hotel["id"], "S", "   ")
    with pytest.raises(SendRefused):
        outbox.send_first(hotel["id"], "S", "x" * 9000)


def test_hourly_limit_and_recovery(make_cfg, store, mail, clock):
    outbox = Outbox(make_cfg(max_emails_per_hour=2, max_emails_per_day=10), store, mail, delay=0)
    ids = [store.add_hotel(f"H{i}", f"h{i}@x.com")["id"] for i in range(3)]
    outbox.send_first(ids[0], "S", "one")
    outbox.send_first(ids[1], "S", "two")
    with pytest.raises(SendRefused, match="лимит"):
        outbox.send_first(ids[2], "S", "three")
    clock.advance(hours=2)
    outbox.send_first(ids[2], "S", "three")
    assert len(mail.sent) == 3


def test_mail_failure_is_reported_without_recording(store, mail, outbox):
    def boom(*args, **kwargs):
        raise OSError("network down")

    mail.send = boom
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    with pytest.raises(SendRefused, match="не отправлено"):
        outbox.send_first(hotel["id"], "S", "Привет")
    assert store.get_hotel(hotel["id"])["contacted_at"] is None
    assert store.read_transcript(hotel["id"]) == ""


def test_batch_personalizes_sends_once_and_clears(store, mail, outbox):
    a = store.add_hotel("Aurora", "a@a.com")
    b = store.add_hotel("Borealis", "b@b.com")
    store.set_batch("Запрос номера", "Здравствуйте, {hotel_name}! Есть ли джакузи?", [a["id"], b["id"]])
    report = outbox.approve_batch()
    assert report["sent"] == ["Aurora", "Borealis"] and report["remaining"] == 0
    assert [m["body"] for m in mail.sent] == [
        "Здравствуйте, Aurora! Есть ли джакузи?",
        "Здравствуйте, Borealis! Есть ли джакузи?",
    ]
    assert store.get_batch() is None
    assert outbox.approve_batch() == {"error": "нет черновика рассылки"}


def test_batch_stops_on_limit_and_can_resume(make_cfg, store, mail, clock):
    outbox = Outbox(make_cfg(max_emails_per_hour=1), store, mail, delay=0)
    ids = [store.add_hotel(f"H{i}", f"h{i}@x.com")["id"] for i in range(3)]
    store.set_batch("S", "Привет, {hotel_name}", ids)
    report = outbox.approve_batch()
    assert report["sent"] == ["H0"] and report["remaining"] == 2 and report["stopped"]
    assert store.get_batch() is not None
    clock.advance(hours=2)
    outbox.cfg = make_cfg(max_emails_per_hour=5)
    report = outbox.approve_batch()
    assert report["sent"] == ["H1", "H2"] and store.get_batch() is None
