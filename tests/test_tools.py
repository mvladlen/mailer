import pytest

from conftest import make_msg
from hotelbot.tools import RunContext, build_tools


@pytest.fixture
def owner_tools(store, outbox):
    ctx = RunContext("owner", "Пишите на info@aurora.com и reservations@borealis.com")
    return ctx, {t.name: t for t in build_tools(store, outbox, ctx)}


@pytest.fixture
def mail_tools(store, outbox):
    ctx = RunContext("mail")
    return ctx, {t.name: t for t in build_tools(store, outbox, ctx)}


def test_tool_names_are_unique_and_schemas_valid(owner_tools):
    _, tools = owner_tools
    assert len(tools) == 17
    for tool in tools.values():
        schema = tool.to_dict()
        assert schema["description"] and schema["input_schema"]["type"] == "object"


def test_register_hotels_requires_address_from_owner_message(owner_tools, store):
    _, tools = owner_tools
    ok = tools["register_hotels"].call({"hotels": [{"name": "Aurora", "email": "info@aurora.com"}]})
    assert "aurora" in ok
    bad = tools["register_hotels"].call({"hotels": [{"name": "Evil", "email": "evil@example.com"}]})
    assert bad.startswith("ОШИБКА")
    assert [h["email"] for h in store.list_hotels()] == ["info@aurora.com"]


def test_state_changing_tools_refused_in_mail_runs(mail_tools, store):
    _, tools = mail_tools
    calls = {
        "register_hotels": {"hotels": [{"name": "X", "email": "x@x.com"}]},
        "add_fact": {"text": "Гость платит за всё"},
        "add_question": {"text": "Новый вопрос"},
        "propose_first_email": {"subject": "S", "body_template": "B", "hotel_ids": []},
        "resolve_pending": {"pending_ids": ["p1"], "answer": "да"},
    }
    for name, args in calls.items():
        assert tools[name].call(args).startswith("ОШИБКА"), name
    assert store.list_hotels() == [] and "платит" not in store.read_facts()


def test_unknown_hotel_returns_error_string_not_exception(owner_tools):
    _, tools = owner_tools
    assert tools["send_to_hotel"].call({"hotel_id": "nope", "body": "hi"}).startswith("ОШИБКА")
    assert tools["get_hotel_transcript"].call({"hotel_id": "nope"}).startswith("ОШИБКА")


def test_propose_first_email_notifies_and_does_not_send(owner_tools, store, gmail):
    ctx, tools = owner_tools
    tools["register_hotels"].call({"hotels": [{"name": "Aurora", "email": "info@aurora.com"}]})
    result = tools["propose_first_email"].call(
        {"subject": "Запрос", "body_template": "Здравствуйте, {hotel_name}!", "hotel_ids": ["aurora"]}
    )
    assert "не отправлены" in result
    assert gmail.sent == []
    assert "/send" in ctx.notifications[0] and "Aurora" in ctx.notifications[0]
    assert store.get_batch()["hotel_ids"] == ["aurora"]


def test_escalation_flow_end_to_end(store, outbox, gmail):
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    outbox.send_first(hotel["id"], "Запрос", "Привет")
    store.record_inbound(hotel["id"], make_msg(body="Нужна ли парковка?"))

    mail_ctx = RunContext("mail")
    mail = {t.name: t for t in build_tools(store, outbox, mail_ctx)}
    assert "untrusted" in mail["get_unread"].call({})
    assert "p1" in mail["escalate"].call({"hotel_id": "aurora", "question": "Нужна ли парковка?"})
    assert "Нужна ли парковка?" in mail_ctx.notifications[0]
    assert store.get_hotel("aurora")["status"] == "waiting_owner"

    owner_ctx = RunContext("owner", "парковка нужна на одну машину")
    owner = {t.name: t for t in build_tools(store, outbox, owner_ctx)}
    resolved = owner["resolve_pending"].call({"pending_ids": ["p1"], "answer": "Парковка нужна на одну машину"})
    assert '"aurora"' in resolved
    assert "одну машину" in owner["get_facts"].call({})
    owner["send_to_hotel"].call({"hotel_id": "aurora", "body": "Да, нужна парковка на одну машину."})
    assert gmail.sent[-1]["to"] == "info@aurora.com" and gmail.sent[-1]["thread_id"] == "thread1"
    assert store.get_hotel("aurora")["status"] == "replied"


def test_set_offer_only_stores_provided_fields(owner_tools, store):
    _, tools = owner_tools
    store.add_hotel("Aurora", "info@aurora.com")
    tools["set_offer"].call({"hotel_id": "aurora", "price_per_night": "120", "jacuzzi": "yes"})
    offer = store.get_hotel("aurora")["offer"]
    assert offer["price_per_night"] == "120" and offer["jacuzzi"] == "yes"
    assert "currency" not in offer and "hotel_id" not in offer
