import asyncio
import ast
import functools
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def stdlib_async_test(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapped


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


@stdlib_async_test
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


@stdlib_async_test
async def test_complete_quotes_and_non_onebot_events_do_not_fetch(hydrator):
    calls = []
    async def forbidden(**kwargs):
        calls.append(kwargs)
        return {"status": "failed", "retcode": 1}
    readable = Reply(1, [Plain("already useful")])
    await hydrator.ensure_quoted_content(event(readable, forbidden))
    empty = Reply(2, [])
    await hydrator.ensure_quoted_content(event(empty, forbidden, "other"))
    assert empty.chain == []
    complete_json = Reply(3, [Json({"prompt": "already present"})])
    await hydrator.ensure_quoted_content(event(complete_json, forbidden))
    complete_image = Reply(5, [Image(file="https://example.org/complete")])
    await hydrator.ensure_quoted_content(event(complete_image, forbidden))
    no_reply = event(Plain("ordinary"), forbidden)
    no_reply.message_obj.message = [Plain("ordinary")]
    await hydrator.ensure_quoted_content(no_reply)
    legacy = Reply(4, [], message_str="visible legacy")
    await hydrator.ensure_quoted_content(event(legacy, forbidden))
    assert calls == []


@stdlib_async_test
async def test_failure_and_malformed_payload_preserve_original(hydrator):
    async def action(**kwargs): return {"status": "failed", "retcode": 1, "data": {"message": []}}
    quote = Reply("-4", [])
    await hydrator.ensure_quoted_content(event(quote, action))
    assert quote.chain == []


@stdlib_async_test
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


@stdlib_async_test
async def test_canonical_event_context_uses_getters_and_checks_message_type(hydrator):
    calls = []
    async def valid(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 31, "self_id": "BOT", "group_id": "GROUP", "message_type": "group", "message": [{"type": "text", "data": {"text": "ok"}}]}}
    quote = Reply(31, [])
    ev = event(quote, valid)
    del ev.self_id
    ev.get_self_id = lambda: "BOT"
    ev.get_group_id = lambda: "GROUP"
    ev.is_private_chat = lambda: False
    await hydrator.ensure_quoted_content(ev)
    assert calls[0]["self_id"] == "BOT" and quote.chain[0].text == "ok"

    async def wrong_bot(**kwargs):
        return {"message_id": 32, "self_id": "OTHER", "message_type": "group", "message": [{"type": "text", "data": {"text": "wrong"}}]}
    quote = Reply(32, [])
    ev = event(quote, wrong_bot); del ev.self_id
    ev.get_self_id = lambda: "BOT"; ev.get_group_id = lambda: "GROUP"; ev.is_private_chat = lambda: False
    await hydrator.ensure_quoted_content(ev)
    assert quote.chain == []

    async def wrong_envelope(**kwargs):
        return {"status": "ok", "retcode": 0, "message_type": "private",
                "data": {"message_id": 37, "message": [{"type": "text", "data": {"text": "wrong"}}]}}
    quote = Reply(37, []); ev = event(quote, wrong_envelope)
    ev.is_private_chat = lambda: False
    await hydrator.ensure_quoted_content(ev)
    assert quote.chain == []

    async def wrong_group(**kwargs):
        return {"message_id": 36, "self_id": "BOT", "group_id": "OTHER", "message_type": "group", "message": [{"type": "text", "data": {"text": "wrong"}}]}
    quote = Reply(36, []); ev = event(quote, wrong_group); del ev.self_id
    ev.get_self_id = lambda: "BOT"; ev.get_group_id = lambda: "GROUP"; ev.is_private_chat = lambda: False
    await hydrator.ensure_quoted_content(ev)
    assert quote.chain == []

    async def wrong_type(**kwargs):
        return {"message_id": 33, "message_type": "private", "message": [{"type": "text", "data": {"text": "wrong"}}]}
    quote = Reply(33, [])
    ev = event(quote, wrong_type); ev.is_private_chat = lambda: False; ev.get_group_id = lambda: "GROUP"
    await hydrator.ensure_quoted_content(ev)
    assert quote.chain == []


@stdlib_async_test
async def test_canonical_json_subclass_and_malformed_chains_are_safe(hydrator):
    class CardSubclass(Json): pass
    calls = []
    async def action(**kwargs):
        calls.append(kwargs)
        return {"message_id": kwargs["message_id"], "message": [{"type": "text", "data": {"text": "replacement"}}]}
    quote = Reply(34, [CardSubclass({"prompt": "already complete"})])
    original = quote.chain[0]
    await hydrator.ensure_quoted_content(event(quote, action))
    assert calls == [] and quote.chain[0] is original
    malformed = Reply(35, 123)
    ev = event(malformed, action)
    await hydrator.ensure_quoted_content(ev)
    assert malformed.chain == 123
    assert len(calls) <= 1


@stdlib_async_test
async def test_reply_depth_budget_stops_at_configured_level(hydrator):
    calls = []
    async def action(**kwargs):
        calls.append(kwargs["message_id"])
        return {"retcode": 9}
    root = Reply(100, [])
    current = root
    for message_id in range(101, 110):
        child = Reply(message_id, [])
        current.chain = [child]
        current = child
    await hydrator.ensure_quoted_content(event(root, action))
    assert len(calls) == hydrator.MAX_DEPTH + 1


@stdlib_async_test
async def test_timeout_is_bounded_and_cancellation_propagates(hydrator, monkeypatch):
    monkeypatch.setattr(hydrator, "CALL_TIMEOUT", .01)
    slow_calls = []
    async def slow(**kwargs):
        slow_calls.append(kwargs)
        await asyncio.sleep(10)
    quote = Reply(11, [])
    timeout_event = event(quote, slow)
    await hydrator.ensure_quoted_content(timeout_event)
    assert quote.chain == []
    assert all(task.done() for task in timeout_event._gcp_quote_hydration["tasks"].values())
    await hydrator.ensure_quoted_content(timeout_event)
    assert all(task.done() for task in timeout_event._gcp_quote_hydration["tasks"].values())
    assert len(slow_calls) == 1

    monkeypatch.setattr(hydrator, "CALL_TIMEOUT", 2.0)
    cancelled_event = event(Reply(12, []), slow)
    task = asyncio.create_task(hydrator.ensure_quoted_content(cancelled_event))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(t.done() for t in cancelled_event._gcp_quote_hydration["tasks"].values())
    await hydrator.ensure_quoted_content(cancelled_event)
    assert all(t.done() for t in cancelled_event._gcp_quote_hydration["tasks"].values())
    assert len(slow_calls) == 2


@stdlib_async_test
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


@stdlib_async_test
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


@stdlib_async_test
async def test_concurrent_consumers_share_one_request_and_wait_for_result(hydrator):
    calls = []
    started = asyncio.Event()
    async def action(**kwargs):
        calls.append(kwargs)
        started.set()
        await asyncio.sleep(.02)
        return {"message_id": 40, "message": [{"type": "text", "data": {"text": "recovered"}}]}
    quote = Reply(40, [])
    ev = event(quote, action)
    first = asyncio.create_task(hydrator.ensure_quoted_content(ev))
    await started.wait()
    second = asyncio.create_task(hydrator.ensure_quoted_content(ev))
    await asyncio.gather(first, second)
    assert len(calls) == 1 and quote.chain[0].text == "recovered"
    assert all(t.done() for t in ev._gcp_quote_hydration["tasks"].values())


@stdlib_async_test
async def test_cancel_one_shared_consumer_does_not_cancel_the_other(hydrator):
    calls = []
    started = asyncio.Event()
    async def action(**kwargs):
        calls.append(kwargs)
        started.set()
        await asyncio.sleep(.05)
        return {"message_id": 41, "message": [{"type": "text", "data": {"text": "survived"}}]}
    quote = Reply(41, [])
    ev = event(quote, action)
    first = asyncio.create_task(hydrator.ensure_quoted_content(ev))
    await started.wait()
    second = asyncio.create_task(hydrator.ensure_quoted_content(ev))
    await asyncio.sleep(.001)
    first.cancel()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(outcomes[0], asyncio.CancelledError) and outcomes[1] is None
    assert len(calls) == 1 and quote.chain[0].text == "survived"
    assert all(t.done() for t in ev._gcp_quote_hydration["tasks"].values())


@stdlib_async_test
async def test_event_call_budget_and_failure_memoization(hydrator, monkeypatch):
    monkeypatch.setattr(hydrator, "MAX_CALLS", 2)
    calls = []
    async def action(**kwargs):
        calls.append(kwargs["message_id"])
        return {"status": "failed", "retcode": 1}
    ev = event(Reply(50, []), action)
    ev.message_obj.message = [Reply(i, []) for i in range(10)]
    await hydrator.ensure_quoted_content(ev)
    await hydrator.ensure_quoted_content(ev)
    assert len(calls) == 2 and all(t.done() for t in ev._gcp_quote_hydration["tasks"].values())


@stdlib_async_test
async def test_event_total_deadline_and_node_budget_cancel_every_task(hydrator, monkeypatch):
    monkeypatch.setattr(hydrator, "MAX_NODES", 3)
    monkeypatch.setattr(hydrator, "MAX_CALLS", 10)
    monkeypatch.setattr(hydrator, "CALL_TIMEOUT", .02)
    monkeypatch.setattr(hydrator, "TOTAL_TIMEOUT", .03)
    calls = []
    async def slow(**kwargs):
        calls.append(kwargs["message_id"])
        await asyncio.sleep(1)
    ev = event(Reply(70, []), slow)
    ev.message_obj.message = [Reply(i, []) for i in range(20)]
    started = asyncio.get_running_loop().time()
    await hydrator.ensure_quoted_content(ev)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < .1 and len(calls) <= 3
    assert all(task.done() for task in ev._gcp_quote_hydration["tasks"].values())


def test_remote_file_image_fallback_and_local_path_rejection(hydrator):
    preferred = hydrator._convert({"type": "image", "data": {"url": "https://example.org/url", "file": "opaque-id"}})
    file_only = hydrator._convert({"type": "image", "data": {"file": "https://example.org/file.png?sig=abc"}})
    local = hydrator._convert({"type": "image", "data": {"file": "C:\\private\\image.png"}})
    assert preferred.url == "https://example.org/url"
    assert file_only.url == "https://example.org/file.png?sig=abc"
    assert local is None


def test_main_event_boundaries_precede_cache_and_preserve_private_disablement():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    chat = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChatPlus")
    methods = {node.name: node for node in chat.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    group = methods["on_group_message"]
    hydrate = next(node for node in ast.walk(group) if isinstance(node, ast.Await) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "ensure_quoted_content")
    gate = next(node for node in ast.walk(group) if isinstance(node, ast.If) and any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("_is_enabled", "_is_user_blacklisted") for n in ast.walk(node.test)) and any(isinstance(n, ast.Await) and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "ensure_quoted_content" for n in ast.walk(node)))
    assert "_is_enabled" in ast.dump(gate.test) and "_is_user_blacklisted" in ast.dump(gate.test)
    early_cache = next(node for node in ast.walk(group) if isinstance(node, ast.If) and any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "getattr" and len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and n.args[1].value == "_has_send_oper" for n in ast.walk(node.test)))
    assert hydrate.lineno < early_cache.lineno
    assert hydrate.lineno < next(node.lineno for node in ast.walk(group) if isinstance(node, ast.If) and "enable_idle_cache_flush" in ast.dump(node.test))

    private = methods["on_private_message"]
    private_hydrate = next(node for node in ast.walk(private) if isinstance(node, ast.Await) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "ensure_quoted_content")
    forward = next(node for node in ast.walk(private) if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "try_parse_and_replace")
    assert private_hydrate.lineno < forward.lineno

    private_tree = ast.parse((ROOT / "private_chat/private_chat_main.py").read_text(encoding="utf-8"))
    private_main = next(node for node in ast.walk(private_tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_message")
    body = private_main.body[1:] if isinstance(private_main.body[0], ast.Expr) and isinstance(private_main.body[0].value, ast.Constant) else private_main.body
    assert isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Call)
    assert isinstance(body[1], ast.Return)


def test_actual_astrbot_4294_component_constructor_smoke(monkeypatch):
    component_file = os.environ.get("GCP_TARGET_COMPONENTS")
    if not component_file:
        pytest.skip("set GCP_TARGET_COMPONENTS to an extracted components.py for optional target smoke")
    source = Path(component_file)
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

    api = types.ModuleType("astrbot.api")
    api_components = types.ModuleType("astrbot.api.message_components")
    for name in ("Plain", "Image", "Reply", "Json", "At", "AtAll", "Face"):
        setattr(api_components, name, getattr(components, name))
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", api_components)
    package = types.ModuleType("target_smoke")
    package.__path__ = [str(ROOT)]
    utils = types.ModuleType("target_smoke.utils")
    utils.__path__ = [str(ROOT / "utils")]
    monkeypatch.setitem(sys.modules, "target_smoke", package)
    monkeypatch.setitem(sys.modules, "target_smoke.utils", utils)

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    renderer = load("target_smoke.utils.json_card_utils", ROOT / "utils/json_card_utils.py")
    hydration = load("target_smoke.utils.quoted_message_hydrator", ROOT / "utils/quoted_message_hydrator.py")
    shared = load("target_smoke.utils.reply_chain_utils", ROOT / "utils/reply_chain_utils.py")
    quote = components.Reply(id=-20)
    async def action(**kwargs):
        return {"message_id": -20, "message": [
            {"type": "json", "data": {"data": {"prompt": "target smoke", "meta": {"detail_1": {"title": "real JSON"}}}}},
            {"type": "image", "data": {"file": "https://example.org/target.png"}},
        ]}
    ev = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[quote]),
        bot=types.SimpleNamespace(call_action=action), self_id="BOT",
        get_self_id=lambda: "BOT", get_platform_name=lambda: "OneBot V11")
    asyncio.run(hydration.ensure_quoted_content(ev))
    assert isinstance(quote.chain[0], components.Json)
    assert isinstance(quote.chain[1], components.Image)
    assert renderer.is_json_component(quote.chain[0])
    assert "real JSON" in shared.render_chain_text(quote.chain)
