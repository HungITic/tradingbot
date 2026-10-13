"""
Telegram Utility functions: link parsing, extraction, and formatting.
"""

from __future__ import annotations

import re
from typing import Tuple


RESERVED_SLUGS = {
    "c",
    "joinchat",
    "addstickers",
    "addtheme",
    "share",
    "invoice",
    "socks",
    "proxy",
    "setlanguage",
    "bg",
    "login",
}


def parse_telegram_message_link(link: str) -> Tuple[str | int | None, int | None]:
    """
    Parse a Telegram message link into its entity (slug or channel_id) and message_id.

    Supported formats:
    - Public: https://t.me/MegaLodonGOLDForex/1241 -> ("MegaLodonGOLDForex", 1241)
    - Public with topic: https://t.me/MegaLodonGOLDForex/2/1241 -> ("MegaLodonGOLDForex", 1241)
    - Private: https://t.me/c/1904907955/1241 -> (-1001904907955, 1241)
    - Private with topic: https://t.me/c/1904907955/2/1241 -> (-1001904907955, 1241)
    - telegram.me, t.me with or without http(s)://
    - Optional query params (e.g. ?single)
    """
    if not link:
        return None, None

    clean_link = link.strip().split("?")[0].rstrip("/")

    # 1. Private channel / supergroup: /c/<channel_id>/[<topic_id>/]<message_id>
    m_private = re.search(
        r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/c/(\d+)(?:/\d+)?/(\d+)$",
        clean_link,
        re.IGNORECASE,
    )
    if m_private:
        channel_num = m_private.group(1)
        msg_id = int(m_private.group(2))
        channel_id = int(f"-100{channel_num}")
        return channel_id, msg_id

    # 2. Public channel / group: /<slug>/[<topic_id>/]<message_id>
    m_public = re.search(
        r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+)(?:/\d+)?/(\d+)$",
        clean_link,
        re.IGNORECASE,
    )
    if m_public:
        slug = m_public.group(1)
        if slug.lower() not in RESERVED_SLUGS:
            msg_id = int(m_public.group(2))
            return slug, msg_id

    return None, None


def extract_telegram_link(text: str) -> str | None:
    """
    Search text for a Telegram message link and return the first matched link.
    """
    if not text:
        return None

    # Find candidate url
    pattern = r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:c/\d+|[a-zA-Z0-9_]+)(?:/\d+)?/\d+(?:\?[^\s]*)?"
    matches = re.findall(pattern, text)
    for candidate in matches:
        entity, msg_id = parse_telegram_message_link(candidate)
        if entity is not None and msg_id is not None:
            # Ensure proper https:// prefix
            if not candidate.startswith("http://") and not candidate.startswith("https://"):
                return f"https://{candidate}"
            return candidate

    return None


def construct_telegram_message_link(
    chat_id: int | None,
    message_id: int | None,
    username: str | None = None,
) -> str | None:
    """
    Construct a canonical Telegram message link.
    """
    if not message_id:
        return None

    if username:
        clean_user = username.lstrip("@").strip()
        if clean_user:
            return f"https://t.me/{clean_user}/{message_id}"

    if chat_id is not None:
        raw_id = str(abs(chat_id))
        if raw_id.startswith("100"):
            raw_id = raw_id[3:]
        return f"https://t.me/c/{raw_id}/{message_id}"

    return None


def format_notification_with_link(text: str, message_link: str | None = None) -> str:
    """
    Append clickable source message link to notification text if present.
    Ensures at most 1 signal link per message.
    """
    if not message_link:
        return text

    if message_link in text or "<b>Signal:</b>" in text or "Signal:" in text or "Tin nhắn gốc" in text:
        return text

    return f"{text}\n\n🔗 <b>Signal:</b> <a href=\"{message_link}\">{message_link}</a>"
