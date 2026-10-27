import pytest

from conftest import make_msg
from hotelbot.store import Store, StoreError


def test_add_hotel_is_idempotent_by_email_and_validates(store):
    first = store.add_hotel("Hotel Aurora", "Info@Aurora.com")
    again = store.add_hotel("Другое имя", "info@aurora.com")
    assert first["id"] == again["id"] == "hotel-aurora"
    assert len(store.list_hotels()) == 1
    with pytest.raises(StoreError):
        store.add_hotel("X", "not-an-email")
    with pytest.raises(StoreError):
        store.add_hotel("X", "a@b.com, c@d.com")


def test_hotel_ids_are_unique_and_name_defaults_to_domain(store):
    a = store.add_hotel("Aurora", "a@one.com")
    b = store.add_hotel("Aurora", "b@two.com")
    c = store.add_hotel("", "reservations@grandplaza.com")
    assert a["id"] != b["id"]
    assert c["name"] == "Grandplaza"


def test_resolve_hotel_by_id_email_name_and_substring(store):
    store.add_hotel("Hotel Aurora", "a@aurora.com")
    store.add_hotel("Hotel Borealis", "b@borealis.com")
    assert store.resolve_hotel("hotel-aurora")["email"] == "a@aurora.com"
    assert store.resolve_hotel("B@borealis.com")["name"] == "Hotel Borealis"
    assert store.resolve_hotel("borealis")["id"] == "hotel-borealis"
    with pytest.raises(StoreError):
        store.resolve_hotel("hotel")  # неоднозначно
    with pytest.raises(StoreError):
        store.resolve_hotel("нет такого")


def test_questions_cover_existing_and_future_hotels(store):
    store.add_hotel("A", "a@a.com")
    q = store.add_question("Есть ли джакузи?")
    store.add_hotel("B", "b@b.com")
    assert store.add_question("есть ли джакузи?")["id"] == q["id"]
    for hotel in store.list_hotels():
        assert hotel["questions"][q["id"]]["state"] == "not_asked"
    store.set_question_state("a", q["id"], "answered", "да")
    assert store.get_hotel("a")["questions"][q["id"]] == {"state": "answered", "note": "да"}
    with pytest.raises(StoreError):
        store.set_question_state("a", q["id"], "bogus")
    with pytest.raises(StoreError):
        store.set_question_state("a", "q99", "asked")


def test_offer_merges_and_rejects_unknown_fields(store):
    store.add_hotel("A", "a@a.com")
    store.set_offer("a", price_per_night="100", currency="EUR", notes="")
    store.set_offer("a", total_price_with_taxes="500")
    offer = store.get_hotel("a")["offer"]
    assert offer["price_per_night"] == "100" and offer["total_price_with_taxes"] == "500"
    assert "notes" not in offer
    with pytest.raises(StoreError):
        store.set_offer("a", hacked="1")


def test_pending_resolution_writes_fact_and_is_deduplicated(store):
    store.add_hotel("A", "a@a.com")
    p1 = store.add_pending("a", "Нужна ли парковка?")
    assert store.add_pending("a", "Нужна ли парковка?")["id"] == p1["id"]
    assert store.get_hotel("a")["status"] == "waiting_owner"
    resolved = store.resolve_pending([p1["id"]], "Парковка нужна на 1 машину")
    assert [r["id"] for r in resolved] == [p1["id"]]
    assert store.list_pending() == []
    assert "Парковка нужна на 1 машину" in store.read_facts()
    with pytest.raises(StoreError):
        store.resolve_pending(["p77"], "x")


def test_record_inbound_updates_everything(store):
    store.add_hotel("Aurora", "info@aurora.com")
    msg = make_msg(body="Добрый день! Есть номер с джакузи.")
    store.record_inbound("aurora", msg)
    hotel = store.get_hotel("aurora")
    assert hotel["status"] == "replied"
    assert hotel["thread_id"] == "thread1"
    assert hotel["last_subject"] == "Запрос"
    assert hotel["last_inbound_message_id"] == "<g1@mail>"
    assert [u["message_id"] for u in hotel["unread"]] == ["g1"]
    assert store.is_processed("g1")
    assert "джакузи" in store.read_transcript("aurora")
    store.mark_read("aurora", "g1")
    assert store.get_hotel("aurora")["unread"] == []


def test_transcript_tail_and_persistence_across_instances(store, cfg):
    store.add_hotel("Aurora", "info@aurora.com")
    store.record_inbound("aurora", make_msg(body="x" * 5000))
    assert store.read_transcript("aurora", tail_chars=100).startswith("…(начало пропущено)…")
    reopened = Store(cfg.data_dir)
    assert reopened.get_hotel("aurora")["status"] == "replied"
    assert len(reopened.read_transcript("aurora")) > 5000


def test_chat_history_is_trimmed_to_recent(store):
    for i in range(15):
        store.append_chat("owner", f"m{i}")
    recent = store.recent_chat(8)
    assert len(recent) == 8 and recent[-1]["text"] == "m14"
