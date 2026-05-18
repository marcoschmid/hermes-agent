"""v4c → MarkdownV2 renderer for Telegram-Hub-Adapter."""
from typing import Any

# Per Telegram docs: these chars must be escaped in MarkdownV2
_MD2_SPECIALS = set(r"_*[]()~`>#+-=|{}.!" + "\\")


def _escape(text: str) -> str:
    return "".join(f"\\{c}" if c in _MD2_SPECIALS else c for c in text)


_SEVERITY_PREFIX = {
    "info": "ℹ️",
    "notice": "📝",
    "warn": "⚠️",
    "error": "❌",
    "crit": "🚨",
}

_LIFECYCLE_INDICATOR = {
    "FIRING": "",  # default, no extra indicator
    "ACKED": "👀 _Acked_",
    "SNOOZED": "💤 _Snoozed_",
    "RECOVERED": "✅ _Recovered_",
}


def render(event: dict[str, Any]) -> str:
    """Render a notification event to MarkdownV2 text for Telegram.

    Behavior depends on render_version:
    - "v4a" (or unset): returns escaped body only (legacy contract)
    - "v4c": returns rich layout with severity-prefix, title, body, service-badge,
            impact, action, context-list, links-list, lifecycle-indicator
    """
    rv = event.get("render_version", "v4a")
    if rv != "v4c":
        return _escape(event.get("body", "") or "")

    parts: list[str] = []

    prefix = _SEVERITY_PREFIX.get(event.get("severity", "info"), "")
    title = event.get("title", "") or ""
    parts.append(f"{prefix} *{_escape(title)}*".strip())

    if body := event.get("body"):
        parts.append(_escape(body))

    if svc := event.get("service"):
        parts.append(f"_Service:_ `{_escape(svc)}`")

    if impact := event.get("impact"):
        parts.append(f"*Impact:* {_escape(impact)}")

    if action := event.get("action_required"):
        parts.append(f"*Action:* {_escape(action)}")

    context = event.get("context") or {}
    if isinstance(context, dict) and context:
        ctx_lines = [
            f"  • _{_escape(str(k))}:_ {_escape(str(v))}" for k, v in context.items()
        ]
        parts.append("*Context:*\n" + "\n".join(ctx_lines))

    links = event.get("links") or {}
    if isinstance(links, dict) and links:
        link_lines = [
            f"  • [{_escape(str(k))}]({_escape(str(v))})" for k, v in links.items()
        ]
        parts.append("*Links:*\n" + "\n".join(link_lines))

    lc = event.get("lifecycle", "FIRING")
    if indicator := _LIFECYCLE_INDICATOR.get(lc):
        parts.append(indicator)

    return "\n\n".join(parts)
