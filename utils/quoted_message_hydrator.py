"""Bounded, event-local OneBot recovery for unusable quoted chains."""
import asyncio
import base64
import json
import time

from astrbot.api.message_components import Plain, Image, Reply
from .json_card_utils import render_json_card

MAX_DEPTH, MAX_NODES, MAX_CALLS = 3, 200, 4
CALL_TIMEOUT, TOTAL_TIMEOUT = 2.0, 4.0


def _useful(chain, depth=0, visited=None, budget=None):
    visited = visited if visited is not None else set()
    budget = budget if budget is not None else [0]
    if depth > MAX_DEPTH or budget[0] >= MAX_NODES:
        return False
    for item in chain or ():
        budget[0] += 1
        if budget[0] > MAX_NODES:
            return False
        if isinstance(item, Plain) and isinstance(getattr(item, "text", None), str) and item.text.strip():
            return True
        if isinstance(item, (Image,)) or item.__class__.__name__ in ("Json", "Video", "Record", "File", "At", "AtAll", "Face"):
            return True
        if isinstance(item, Reply):
            if id(item) in visited:
                continue
            visited.add(id(item))
            if _legacy(item) or _useful(getattr(item, "chain", None), depth + 1, visited, budget):
                return True
    return False


def _legacy(reply):
    return any(isinstance(getattr(reply, name, None), str) and getattr(reply, name).strip() for name in ("message_str", "message"))


def _mk_json(data):
    try:
        from astrbot.api.message_components import Json
        return Json(data=data)
    except Exception:
        try:
            from astrbot.core.message.components import Json
            return Json(data=data)
        except Exception:
            return Plain(render_json_card(data))


def _optional_component(name, **kwargs):
    try:
        from astrbot.core.message import components
        cls = getattr(components, name, None)
        return cls(**kwargs) if cls else None
    except Exception:
        return None


def _convert(segment):
    if not isinstance(segment, dict):
        return None
    kind, data = segment.get("type"), segment.get("data")
    if not isinstance(data, dict):
        return None
    try:
        if kind == "text" and isinstance(data.get("text"), str):
            return Plain(data["text"][:8000])
        if kind == "image":
            source = data.get("url")
            if isinstance(source, str) and source.startswith(("http://", "https://")) and len(source) <= 4096:
                return Image(file=source, url=source)
            source = data.get("file")
            if isinstance(source, str) and source.startswith("base64://") and len(source) <= 8_000_000:
                try: base64.b64decode(source[9:], validate=True)
                except (ValueError, base64.binascii.Error): return None
                return Image(file=source)
        if kind == "json":
            raw = data.get("data")
            if isinstance(raw, str):
                if len(raw) > 65536:
                    return Plain("[JSON卡片]")
                try: raw = json.loads(raw)
                except (ValueError, RecursionError): raw = None
            return _mk_json(raw) if isinstance(raw, dict) else Plain("[JSON卡片]")
        if kind == "reply" and data.get("id") is not None:
            return Reply(id=data["id"])
        if kind == "at" and data.get("qq") is not None:
            return _optional_component("At", qq=data.get("qq"))
        if kind == "video": return _optional_component("Video", file=data.get("file"), url=data.get("url"))
        if kind == "record": return _optional_component("Record", file=data.get("file"), url=data.get("url"))
        if kind == "file": return _optional_component("File", file=data.get("file"), name=data.get("name"))
    except Exception:
        return None
    return None


def _matches_event(event, result):
    expected_self = getattr(event, "self_id", None)
    expected_group = None
    try: expected_group = event.get_group_id()
    except Exception: pass
    for key, expected in (("self_id", expected_self), ("group_id", expected_group)):
        actual = result.get(key)
        if expected is not None and actual is not None and str(expected) != str(actual):
            return False
    return True


async def ensure_quoted_content(event):
    """Hydrate only empty/unsupported Reply.chain nodes through get_msg."""
    state = getattr(event, "_gcp_quote_hydration", None)
    if state is None:
        state = {"attempted": set(), "tasks": {}, "calls": 0, "deadline": time.monotonic() + TOTAL_TIMEOUT}
        try: setattr(event, "_gcp_quote_hydration", state)
        except Exception: return
    chain = getattr(getattr(event, "message_obj", None), "message", None)
    if not isinstance(chain, (list, tuple)):
        return
    try: platform = event.get_platform_name()
    except Exception: platform = ""
    if not any(tag in str(platform).lower() for tag in ("onebot", "aiocqhttp", "napcat")):
        return
    bot = getattr(event, "bot", None)
    action = getattr(bot, "call_action", None) or getattr(getattr(bot, "api", None), "call_action", None)
    if not callable(action): return
    calls_this_turn, nodes_this_turn = 0, 0

    async def hydrate(items, depth=0):
        nonlocal calls_this_turn, nodes_this_turn
        if depth > MAX_DEPTH or nodes_this_turn >= MAX_NODES:
            return
        for reply in items or ():
            if nodes_this_turn >= MAX_NODES: break
            nodes_this_turn += 1
            if not isinstance(reply, Reply): continue
            nested = getattr(reply, "chain", None)
            if isinstance(nested, (list, tuple)):
                await hydrate(nested, depth + 1)
            if _useful(nested) or _legacy(reply): continue
            try: msg_id = int(getattr(reply, "id", None))
            except (ValueError, TypeError, OverflowError): continue
            self_id = getattr(event, "self_id", None)
            if self_id is None:
                try: self_id = event.get_self_id()
                except Exception: pass
            key = (msg_id, str(self_id or ""))
            if key not in state["attempted"]:
                if state["calls"] >= MAX_CALLS or time.monotonic() >= state["deadline"]:
                    continue
                kwargs = {"action": "get_msg", "message_id": msg_id}
                if self_id is not None: kwargs["self_id"] = self_id
                state["attempted"].add(key)
                state["calls"] += 1
                task = asyncio.create_task(action(**kwargs))
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                state["tasks"][key] = task
            task = state["tasks"].get(key)
            if task is None: continue
            remain = min(CALL_TIMEOUT, state["deadline"] - time.monotonic())
            if remain <= 0: continue
            try: result = await asyncio.wait_for(asyncio.shield(task), timeout=remain)
            except asyncio.CancelledError: raise
            except Exception: continue
            calls_this_turn += 1
            if calls_this_turn > MAX_CALLS: continue
            if not isinstance(result, dict): continue
            if "status" in result and result.get("status") != "ok": continue
            if "retcode" in result and result.get("retcode") not in (0, "0"): continue
            if "data" in result and isinstance(result.get("data"), dict): result = result["data"]
            if not _matches_event(event, result): continue
            if result.get("message_id") is not None and str(result["message_id"]) != str(msg_id): continue
            segments = result.get("message")
            if not isinstance(segments, list): continue
            converted = [obj for obj in (_convert(seg) for seg in segments[:MAX_NODES]) if obj is not None]
            if converted:
                reply.chain = converted
                sender = result.get("sender")
                if isinstance(sender, dict) and not getattr(reply, "sender_id", None):
                    reply.sender_id = sender.get("user_id")
                    reply.sender_nickname = sender.get("nickname")
                await hydrate(converted, depth + 1)
    await hydrate(chain)
