import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


class Plain:
    def __init__(self, text): self.text = text
class Image:
    def __init__(self, file=None, url=None): self.file, self.url = file, url
class Reply:
    def __init__(self, id=None, chain=None, message_str=None): self.id, self.chain, self.message_str = id, chain, message_str
class Json:
    def __init__(self, data): self.data = data


@pytest.fixture
def hydrator(monkeypatch):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    components = types.ModuleType("astrbot.api.message_components")
    for name, value in dict(Plain=Plain, Image=Image, Reply=Reply, Json=Json).items(): setattr(components, name, value)
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", components)
    pkg = types.ModuleType("hydrate_test")
    pkg.__path__ = [str(ROOT)]
    utils = types.ModuleType("hydrate_test.utils")
    utils.__path__ = [str(ROOT / "utils")]
    monkeypatch.setitem(sys.modules, "hydrate_test", pkg)
    monkeypatch.setitem(sys.modules, "hydrate_test.utils", utils)
    spec = importlib.util.spec_from_file_location("hydrate_test.utils.quoted_message_hydrator", ROOT / "utils/quoted_message_hydrator.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def event(reply, action, platform="aiocqhttp"):
    bot = types.SimpleNamespace(call_action=action)
    return types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[reply]), bot=bot,
                                 get_platform_name=lambda: platform, self_id="BOT")


@pytest.mark.asyncio
async def test_recovers_negative_id_image_once_with_universal_self_id(hydrator):
    calls = []
    async def action(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "retcode": 0, "data": {"message_id": -10, "message": [{"type": "image", "data": {"url": "https://example.org/i"}}]}}
    quote = Reply(-10, [])
    ev = event(quote, action)
    await hydrator.ensure_quoted_content(ev)
    await hydrator.ensure_quoted_content(ev)
    assert len(calls) == 1 and calls[0]["self_id"] == "BOT" and calls[0]["message_id"] == -10
    assert isinstance(quote.chain[0], Image)


@pytest.mark.asyncio
async def test_complete_quotes_and_non_onebot_events_do_not_fetch(hydrator):
    async def forbidden(**kwargs): raise AssertionError("unexpected fetch")
    readable = Reply(1, [Plain("already useful")])
    await hydrator.ensure_quoted_content(event(readable, forbidden))
    empty = Reply(2, [])
    await hydrator.ensure_quoted_content(event(empty, forbidden, "other"))
    assert empty.chain == []
    complete_json = Reply(3, [Json({"prompt": "already present"})])
    await hydrator.ensure_quoted_content(event(complete_json, forbidden))


@pytest.mark.asyncio
async def test_failure_and_malformed_payload_preserve_original(hydrator):
    async def action(**kwargs): return {"status": "failed", "retcode": 1, "data": {"message": []}}
    quote = Reply("-4", [])
    await hydrator.ensure_quoted_content(event(quote, action))
    assert quote.chain == []


@pytest.mark.asyncio
async def test_rejects_bad_retcode_mismatched_session_and_stops_cycles(hydrator):
    calls = []
    async def action(**kwargs):
        calls.append(kwargs)
        return {"retcode": 9, "data": {"message_id": kwargs["message_id"], "message": [{"type": "text", "data": {"text": "bad"}}]}}
    cycle = Reply(9, [])
    cycle.chain = [cycle]
    ev = event(cycle, action)
    await hydrator.ensure_quoted_content(ev)
    assert len(calls) == 1 and cycle.chain == [cycle]

    async def wrong_session(**kwargs):
        return {"status": "ok", "retcode": 0, "data": {"message_id": 10, "self_id": "OTHER", "message": [{"type": "text", "data": {"text": "bad"}}]}}
    quote = Reply(10, [])
    await hydrator.ensure_quoted_content(event(quote, wrong_session))
    assert quote.chain == []


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_cancellation_propagates(hydrator, monkeypatch):
    monkeypatch.setattr(hydrator, "CALL_TIMEOUT", .01)
    async def slow(**kwargs): await asyncio.sleep(10)
    quote = Reply(11, [])
    await hydrator.ensure_quoted_content(event(quote, slow))
    assert quote.chain == []

    task = asyncio.create_task(hydrator.ensure_quoted_content(event(Reply(12, []), slow)))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_api_action_direct_payload_and_json_recovery(hydrator):
    calls = []
    async def action(**kwargs):
        calls.append(kwargs)
        return {"message_id": 13, "message": [{"type": "json", "data": {"data": {"prompt": "restored"}}}]}
    quote = Reply(13, [])
    ev = event(quote, None, "OneBot V11")
    ev.bot = types.SimpleNamespace(api=types.SimpleNamespace(call_action=action))
    await hydrator.ensure_quoted_content(ev)
    assert len(calls) == 1 and isinstance(quote.chain[0], Json)


@pytest.mark.asyncio
async def test_nested_reply_recovery_is_finite_and_ordered(hydrator):
    calls = []
    async def action(**kwargs):
        calls.append(kwargs["message_id"])
        if kwargs["message_id"] == 20:
            return {"message_id": 20, "message": [{"type": "text", "data": {"text": "before"}}, {"type": "reply", "data": {"id": 21}}]}
        return {"message_id": 21, "message": [{"type": "image", "data": {"url": "https://example.org/nested"}}]}
    quote = Reply(20, [])
    await hydrator.ensure_quoted_content(event(quote, action))
    assert calls == [20, 21]
    assert isinstance(quote.chain[0], Plain) and isinstance(quote.chain[1], Reply)
    assert isinstance(quote.chain[1].chain[0], Image)


def test_main_event_boundaries_precede_cache_and_preserve_private_disablement():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    group = source[source.index("async def on_group_message"):source.index("async def _process_message(")]
    assert group.index("await ensure_quoted_content(event)") < group.index("if self.enable_idle_cache_flush")
    private = source[source.index("async def on_private_message"):source.index("async def on_group_message")]
    assert private.index("await ensure_quoted_content(event)") < private.index("ForwardMessageParser.try_parse_and_replace")
    disabled = (ROOT / "private_chat/private_chat_main.py").read_text(encoding="utf-8")
    assert "async def handle_message" in disabled
    assert "logger.warning" in disabled


def test_actual_astrbot_4294_component_constructor_smoke(monkeypatch):
    wheel = Path(r"D:\Documents\杂项\output\gcp_fixes_20261107\astrbot-4.29.4")
    source = wheel / "astrbot/core/message/components.py"
    assert source.is_file()
    names = ("astrbot", "astrbot.core", "astrbot.core.utils", "astrbot.core.utils.astrbot_path",
             "astrbot.core.utils.io", "astrbot.core.utils.media_utils")
    modules = {name: types.ModuleType(name) for name in names}
    modules["astrbot"].__path__ = []
    modules["astrbot.core"].__path__ = []
    modules["astrbot.core"].astrbot_config = types.SimpleNamespace(get=lambda *a: None)
    modules["astrbot.core"].file_token_service = types.SimpleNamespace(register_file=None)
    modules["astrbot.core"].logger = types.SimpleNamespace(debug=lambda *a: None)
    modules["astrbot.core.utils"].__path__ = []
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_temp_path = lambda: ""
    modules["astrbot.core.utils.io"].download_file = lambda *a, **k: None
    media = modules["astrbot.core.utils.media_utils"]
    media.MediaResolver = object
    media.file_uri_to_path = lambda x: x
    media.is_file_uri = lambda x: False
    for key, value in modules.items(): monkeypatch.setitem(sys.modules, key, value)
    components_pkg = types.ModuleType("astrbot.core.message")
    components_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "astrbot.core.message", components_pkg)
    spec = importlib.util.spec_from_file_location("astrbot.core.message.components", source)
    components = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, components)
    spec.loader.exec_module(components)
    image = components.Image(file="https://example.org/i", url="https://example.org/i")
    card = components.Json(data={"prompt": "smoke"})
    quote = components.Reply(id=-10)
    assert image.file == "https://example.org/i" and card.data == {"prompt": "smoke"} and quote.id == "-10"
