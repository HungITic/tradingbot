"""
Unit tests for Telegram utilities (link parsing, extracting, and formatting).
"""

from app.telegram.utils import (
    construct_telegram_message_link,
    extract_telegram_link,
    format_notification_with_link,
    parse_telegram_message_link,
)


def test_parse_public_channel_link():
    link = "https://t.me/MegaLodonGOLDForex/1241"
    entity, msg_id = parse_telegram_message_link(link)
    assert entity == "MegaLodonGOLDForex"
    assert msg_id == 1241


def test_parse_public_channel_link_with_query_or_topic():
    # With query parameter
    link = "https://t.me/MegaLodonGOLDForex/1241?single"
    entity, msg_id = parse_telegram_message_link(link)
    assert entity == "MegaLodonGOLDForex"
    assert msg_id == 1241

    # With forum topic id
    link_topic = "https://t.me/MegaLodonGOLDForex/5/1241"
    entity, msg_id = parse_telegram_message_link(link_topic)
    assert entity == "MegaLodonGOLDForex"
    assert msg_id == 1241

    # Without protocol
    link_raw = "t.me/MegaLodonGOLDForex/1241"
    entity, msg_id = parse_telegram_message_link(link_raw)
    assert entity == "MegaLodonGOLDForex"
    assert msg_id == 1241


def test_parse_private_channel_link():
    link = "https://t.me/c/1904907955/1241"
    entity, msg_id = parse_telegram_message_link(link)
    assert entity == -1001904907955
    assert msg_id == 1241

    # Private with forum topic
    link_topic = "https://t.me/c/3725831625/2/999"
    entity, msg_id = parse_telegram_message_link(link_topic)
    assert entity == -1003725831625
    assert msg_id == 999


def test_extract_telegram_link():
    text = "Vui lòng xem lệnh tại https://t.me/MegaLodonGOLDForex/1241 giúp tôi"
    extracted = extract_telegram_link(text)
    assert extracted == "https://t.me/MegaLodonGOLDForex/1241"

    text_no_proto = "check link t.me/c/1904907955/555 nha"
    extracted_no_proto = extract_telegram_link(text_no_proto)
    assert extracted_no_proto == "https://t.me/c/1904907955/555"


def test_construct_telegram_message_link():
    # With username
    link1 = construct_telegram_message_link(
        chat_id=-1001904907955, message_id=1241, username="MegaLodonGOLDForex"
    )
    assert link1 == "https://t.me/MegaLodonGOLDForex/1241"

    # Private without username
    link2 = construct_telegram_message_link(
        chat_id=-1001904907955, message_id=1241
    )
    assert link2 == "https://t.me/c/1904907955/1241"


def test_format_notification_with_link():
    msg = "✅ <b>Order Executed</b>"
    link = "https://t.me/MegaLodonGOLDForex/1241"
    formatted = format_notification_with_link(msg, link)
    assert "🔗 <b>Signal:</b>" in formatted
    assert link in formatted

    # Idempotent: don't duplicate
    reformatted = format_notification_with_link(formatted, link)
    assert reformatted == formatted
