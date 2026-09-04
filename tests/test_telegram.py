"""Telegram notifier tests — offline (only the pure chunking logic)."""

from __future__ import annotations

from notify.telegram import TelegramNotifier, _MAX_MESSAGE_LEN


def test_chunk_short_text_single_message() -> None:
    assert TelegramNotifier._chunk("hello") == ["hello"]


def test_chunk_long_text_splits_at_line_boundaries() -> None:
    line = "x" * 100
    text = "\n".join([line] * 100)  # 10_100 chars -> several chunks
    chunks = TelegramNotifier._chunk(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= _MAX_MESSAGE_LEN
    # No data lost or duplicated.
    assert "\n".join(chunks) == text


def test_chunk_never_drops_an_oversized_single_line() -> None:
    monster = "y" * (_MAX_MESSAGE_LEN + 500)
    chunks = TelegramNotifier._chunk(monster)
    assert sum(len(c) for c in chunks) >= len(monster) - len(chunks)


def test_notifier_disabled_send_is_safe() -> None:
    # Empty strings would fall through to env/config credentials, so disable
    # explicitly: the send must be a safe no-op returning False.
    notifier = TelegramNotifier(enabled=False)
    assert not notifier.is_configured
    assert notifier.send("should not crash") is False
