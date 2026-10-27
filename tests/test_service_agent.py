import asyncio
from types import SimpleNamespace

import pytest

from hotelbot.agent import AgentResult, AgentRunner, load_system_prompt
from hotelbot.service import Job, Service


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, chat_id, text):
        self.messages.append(text)

    async def send_document(self, chat_id, filename, content, caption=""):
        self.documents.append((filename, content))


class FakeAgent:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, source, text=""):
        self.calls.append((source, text))
        return self.result


@pytest.fixture
def tg():
    return FakeTelegram()


def make_service(cfg, store, outbox, mail, tg, agent=None):
    return Service(cfg, store, outbox, mail, agent or FakeAgent(AgentResult("ок")), tg)


async def test_plain_text_is_queued_and_commands_are_handled_inline(cfg, store, outbox, mail, tg):
    service = make_service(cfg, store, outbox, mail, tg)
    await service.handle_owner_text("спроси про джакузи")
    assert service.queue.get_nowait().text == "спроси про джакузи"
    await service.handle_owner_text("/send")
    assert "Нет шаблона" in tg.messages[-1] and service.queue.empty()
    await service.handle_owner_text("/status")
    assert "Режим проверки" in tg.messages[-1]
    await service.handle_owner_text("/неизвестная")
    assert "Неизвестная команда" in tg.messages[-1]


async def test_send_with_batch_enqueues_job_and_reports(cfg, store, outbox, mail, tg):
    hotel = store.add_hotel("Aurora", "info@aurora.com")
    store.set_batch("Запрос", "Привет, {hotel_name}", [hotel["id"]])
    service = make_service(cfg, store, outbox, mail, tg)
    await service.handle_owner_text("/send")
    job = service.queue.get_nowait()
    assert job.kind == "send_batch"
    await service.process(job)
    assert "Отправлено: 1" in tg.messages[-1] and mail.sent[0]["to"] == "info@aurora.com"


async def test_owner_job_sends_notifications_then_reply(cfg, store, outbox, mail, tg):
    agent = FakeAgent(AgentResult("Итог", ["Шаблон письма"]))
    service = make_service(cfg, store, outbox, mail, tg, agent)
    await service.process(Job("owner", "привет"))
    assert tg.messages == ["Шаблон письма", "Итог"] and agent.calls == [("owner", "привет")]


async def test_mail_job_never_sends_final_text_to_owner(cfg, store, outbox, mail, tg):
    agent = FakeAgent(AgentResult("внутренний итог", ["Отель прислал предложение"]))
    service = make_service(cfg, store, outbox, mail, tg, agent)
    await service.process(Job("mail"))
    assert tg.messages == ["Отель прислал предложение"]


async def test_mail_jobs_are_coalesced(cfg, store, outbox, mail, tg):
    service = make_service(cfg, store, outbox, mail, tg)
    await service.enqueue_mail()
    await service.enqueue_mail()
    assert service.queue.qsize() == 1


async def test_worker_survives_agent_failure(cfg, store, outbox, mail, tg):
    class Broken:
        def run(self, source, text=""):
            raise RuntimeError("API down")

    service = make_service(cfg, store, outbox, mail, tg, Broken())
    worker = asyncio.create_task(service.worker_loop())
    await service.queue.put(Job("owner", "привет"))
    await asyncio.sleep(0.2)
    worker.cancel()
    assert any("внутренняя ошибка" in m for m in tg.messages)
    assert "API down" not in " ".join(tg.messages)


async def test_transcript_command_sends_text_or_document(cfg, store, outbox, mail, tg):
    from conftest import make_msg

    store.add_hotel("Aurora", "info@aurora.com")
    service = make_service(cfg, store, outbox, mail, tg)
    await service.handle_owner_text("/transcript aurora")
    assert "пока нет" in tg.messages[-1]
    store.record_inbound("aurora", make_msg(body="короткое письмо"))
    await service.handle_owner_text("/transcript aurora")
    assert "короткое письмо" in tg.messages[-1]
    store.record_inbound("aurora", make_msg("g2", body="x" * 6000))
    await service.handle_owner_text("/transcript Aurora")
    assert tg.documents and tg.documents[0][0] == "aurora.md"


# ---- агент ----------------------------------------------------------------------


class FakeRunner:
    def __init__(self, text):
        self.text = text

    def until_done(self):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


class FakeAnthropic:
    def __init__(self, text="Готово"):
        self.captured = {}
        text_ = text

        def tool_runner(**kwargs):
            self.captured.update(kwargs)
            return FakeRunner(text_)

        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=tool_runner))


def test_system_prompt_contains_security_rules():
    prompt = load_system_prompt()
    for phrase in ("недоверенн", "/send", "escalate", "notify_owner"):
        assert phrase in prompt


def test_agent_run_composes_context_and_records_chat(cfg, store, outbox):
    store.add_hotel("Aurora", "info@aurora.com")
    store.add_pending("aurora", "Нужна ли парковка?")
    store.append_chat("owner", "прошлое сообщение")
    client = FakeAnthropic("Готово, всё записал")
    runner = AgentRunner(cfg, store, outbox, client=client)
    result = runner.run("owner", "парковка нужна")
    sent = client.captured["messages"][0]["content"]
    assert "прошлое сообщение" in sent and "p1" in sent and "<owner_message>\nпарковка нужна" in sent
    assert client.captured["model"] == cfg.model
    assert len(client.captured["tools"]) == 17
    assert "DRY RUN" in client.captured["system"]
    assert result.reply == "Готово, всё записал"
    assert store.recent_chat(2)[-1] == {**store.recent_chat(2)[-1], "role": "assistant"}


def test_mail_run_does_not_pollute_owner_chat(cfg, store, outbox):
    client = FakeAnthropic("внутренний итог")
    runner = AgentRunner(cfg, store, outbox, client=client)
    runner.run("mail")
    assert store.recent_chat() == []
    assert "get_unread" in client.captured["messages"][0]["content"]


def test_real_tool_runner_loop_executes_our_tools(cfg, store, outbox):
    """Прогоняет настоящий цикл tool_runner из SDK против поддельного HTTP-сервера."""
    import json

    import anthropic
    import httpx2 as httpx

    store.add_hotel("Aurora", "info@aurora.com")
    seen = {}

    def message(content, stop):
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": cfg.model,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        last = body["messages"][-1]["content"]
        if isinstance(last, list) and last and last[0].get("type") == "tool_result":
            seen["tool_result"] = last[0]["content"]
            return httpx.Response(200, json=message([{"type": "text", "text": "Один отель в списке."}], "end_turn"))
        seen["tools"] = [t["name"] for t in body["tools"]]
        return httpx.Response(
            200, json=message([{"type": "tool_use", "id": "toolu_1", "name": "list_hotels", "input": {}}], "tool_use")
        )

    client = anthropic.Anthropic(
        api_key="test", base_url="http://fake.local", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = AgentRunner(cfg, store, outbox, client=client).run("owner", "что по отелям?")
    assert result.reply == "Один отель в списке."
    assert "list_hotels" in seen["tools"] and len(seen["tools"]) == 17
    assert "info@aurora.com" in json.dumps(seen["tool_result"], ensure_ascii=False)
