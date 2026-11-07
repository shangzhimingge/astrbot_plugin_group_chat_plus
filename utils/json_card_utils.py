"""Bounded renderer for known QQ JSON share-card shapes."""
import json
import re
from urllib.parse import urlsplit

try:
    from astrbot.api.message_components import Json
except ImportError:
    try:
        from astrbot.core.message.components import Json
    except ImportError:
        Json = None

MAX_RAW = 65536
MAX_OUTPUT = 1000
SECTIONS = ("detail_1", "detail", "news", "video", "music", "mannounce")


def is_json_component(value):
    return (Json is not None and isinstance(value, Json)) or (
        value.__class__.__name__ == "Json" and hasattr(value, "data")
    )


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    value = value[:limit * 4]
    value = "".join(c for c in value if c >= " " and c != "\x7f" and not (0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069))
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return ""
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return ""
        return f"{parsed.scheme.lower()}://{parsed.netloc}{parsed.path}"[:500]
    except (ValueError, UnicodeError):
        return ""


def render_json_card(payload):
    """Render only fixed known fields; never stringify unknown payloads."""
    try:
        if isinstance(payload, str):
            if len(payload) > MAX_RAW:
                return "[JSON卡片]"
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            return "[JSON卡片]"
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        sections = [meta.get(key) for key in SECTIONS if isinstance(meta.get(key), dict)]
        prompt = _clean(payload.get("prompt"), 300)
        app = _clean(payload.get("app"), 100)
        title = next((_clean(section.get("title"), 200) for section in sections if _clean(section.get("title"), 200)), "")
        desc = next((_clean(section.get("desc") or section.get("summary"), 500) for section in sections if _clean(section.get("desc") or section.get("summary"), 500)), "") or _clean(payload.get("desc"), 500)
        link = ""
        for key in ("qqdocurl", "jumpUrl", "url"):
            for candidate in [s.get(key) for s in sections] + [payload.get(key)]:
                link = _url(candidate)
                if link:
                    break
            if link:
                break
        values = []
        for value in (prompt, app, title, desc, link):
            if value and value not in values:
                values.append(value)
        if not values:
            return "[JSON卡片]"
        if link:
            head = "[JSON卡片] " + " | ".join(v for v in values if v != link)
            head = head[:max(0, MAX_OUTPUT - len(link) - 3)].rstrip(" |")
            return (head + " | " + link)[:MAX_OUTPUT]
        return ("[JSON卡片] " + " | ".join(values))[:MAX_OUTPUT]
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return "[JSON卡片]"


def render_json_component(component):
    return render_json_card(getattr(component, "data", None))
