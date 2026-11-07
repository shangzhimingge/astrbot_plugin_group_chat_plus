"""Bounded, event-local OneBot recovery for unusable quoted chains."""
import asyncio
import base64
import inspect
import json
import time
from urllib.parse import urlsplit

from astrbot.api.message_components import Plain, Image, Reply
from .json_card_utils import is_json_component, render_json_card

try:
    from astrbot.core.message.components import At, AtAll, Face, Video, Record, File
except ImportError:
    try:
        from astrbot.api.message_components import At, AtAll, Face, Video, Record, File
    except ImportError:
        At = AtAll = Face = Video = Record = File = None

MAX_DEPTH, MAX_NODES, MAX_CALLS = 3, 200, 4
CALL_TIMEOUT, TOTAL_TIMEOUT = 2.0, 4.0


def _useful(chain, depth=0, visited=None, budget=None):
    if not isinstance(chain, (list, tuple)):
        return False
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
        if isinstance(item, Image) or is_json_component(item):
            return True
        if any(cls is not None and isinstance(item, cls) for cls in (At, AtAll, Face, Video, Record, File)):
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


def _remote_source(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(ord(char) < 33 or ord(char) == 127 or ord(char) in {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)} for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return None
        return value
    except (ValueError, UnicodeError):
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
            source = _remote_source(data.get("url"))
            if source:
                return Image(file=source, url=source)
            source = _remote_source(data.get("file"))
            if source:
                return Image(file=source, url=source)
            source = data.get("file")
            if isinstance(source, str) and source.startswith("base64://") and 9 < len(source) <= 8_000_000:
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
            return _optional_component("At", qq=str(data["qq"])[:64])
        if kind == "at_all": return _optional_component("AtAll")
        if kind == "face": return _optional_component("Face", id=data.get("id")) if data.get("id") is not None else None
        if kind in ("video", "record"):
            source = _remote_source(data.get("url")) or _remote_source(data.get("file"))
            return _optional_component("Video" if kind == "video" else "Record", file=source, url=source) if source else None
        if kind == "file":
            name = data.get("name")
            name = name[:256] if isinstance(name, str) else None
            source = _remote_source(data.get("url")) or _remote_source(data.get("file"))
            return _optional_component("File", file=source, name=name) if source or name else None
    except Exception:
        return None
    return None


def _matches_event(event, result):
    expected_self, expected_group, expected_type = _event_context(event)
    for key, expected in (("self_id", expected_self), ("group_id", expected_group)):
        actual = result.get(key)
        if expected is not None and actual is not None and str(expected) != str(actual):
            return False
    actual_type = result.get("message_type")
    if expected_type and actual_type is not None:
        actual_type = str(getattr(actual_type, "value", actual_type)).lower()
        if actual_type != expected_type:
            return False
    return True


def _event_context(event):
    self_id = getattr(event, "self_id", None)
    if self_id is None:
        try: self_id = event.get_self_id()
        except Exception: pass
    try: group_id = event.get_group_id()
    except Exception: group_id = getattr(event, "group_id", None)
    message_type = None
    try:
        is_private = event.is_private_chat()
        message_type = "private" if is_private else "group"
    except Exception:
        pass
    if message_type is None:
        for getter in ("get_message_type",):
            try:
                raw = getattr(event, getter)()
                raw = str(getattr(raw, "value", raw)).lower()
                if "private" in raw: message_type = "private"
                elif "group" in raw: message_type = "group"
            except Exception: pass
    if message_type is None and group_id is not None:
        message_type = "group"
    return self_id, group_id, message_type


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
            if nested is not None and not isinstance(nested, (list, tuple)):
                continue
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
                async def invoke():
                    result = action(**kwargs)
                    return await result if inspect.isawaitable(result) else result
                task = asyncio.create_task(invoke())
                state["tasks"][key] = task
            task = state["tasks"].get(key)
            if task is None: continue
            remain = min(CALL_TIMEOUT, state["deadline"] - time.monotonic())
            if remain <= 0:
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                continue
            try: result = await asyncio.wait_for(task, timeout=remain)
            except asyncio.CancelledError:
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception: continue
            calls_this_turn += 1
            if calls_this_turn > MAX_CALLS: continue
            if not isinstance(result, dict): continue
            if "status" in result and result.get("status") != "ok": continue
            if "retcode" in result and result.get("retcode") not in (0, "0"): continue
            if "data" in result and isinstance(result.get("data"), dict):
                envelope = result
                if not _matches_event(event, envelope): continue
                result = {**envelope, **envelope["data"]}
            if not _matches_event(event, result): continue
            if result.get("message_id") is not None and str(result["message_id"]) != str(msg_id): continue
            segments = result.get("message")
            if not isinstance(segments, list): continue
            converted = [obj for obj in (_convert(seg) for seg in segments[:MAX_NODES]) if obj is not None]
            if converted:
                reply.chain = converted
                sender = result.get("sender")
                if isinstance(sender, dict) and not getattr(reply, "sender_id", None):
                    sender_id = sender.get("user_id")
                    if isinstance(sender_id, (str, int)) and not isinstance(sender_id, bool) and 0 < len(str(sender_id)) <= 64:
                        reply.sender_id = sender_id
                    nickname = sender.get("nickname")
                    if isinstance(nickname, str) and nickname.strip():
                        reply.sender_nickname = nickname[:128]
                await hydrate(converted, depth + 1)
    await hydrate(chain)
