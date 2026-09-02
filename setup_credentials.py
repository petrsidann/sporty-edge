"""
setup_credentials.py — interactive credential setup (run once).

    python3 setup_credentials.py

Asks for your Telegram bot token, chat id, and The Odds API key, then
writes them into config/settings.py automatically (between managed
markers, so re-running this script updates instead of duplicating).

Get the values from:
    bot token : Telegram -> @BotFather -> /newbot -> the token it replies with
    chat id   : Telegram -> @userinfobot -> the number it replies with
    api key   : https://the-odds-api.com  (free registration, 500 credits/month)

Then press START once on your bot in Telegram (bots cannot message you first).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SETTINGS_PATH = Path("config") / "settings.py"

START_MARKER = "# === sporty-edge credentials (auto-managed by setup_credentials.py) ==="
END_MARKER = "# === end credentials ==="


def _ask(prompt: str, validate, retry_hint: str) -> str:
    """Ask until the answer passes validation. Empty answer is allowed (skip)."""
    while True:
        value = input(prompt).strip()
        if not value:
            return ""
        if validate(value):
            return value
        print(f"    ✗ {retry_hint} (or press Enter to skip for now)")


def _valid_token(value: str) -> bool:
    return ":" in value and len(value) > 20


def _valid_chat_id(value: str) -> bool:
    return value.isdigit()


def _valid_api_key(value: str) -> bool:
    return len(value) >= 8


def build_block(token: str, chat_id: str, api_key: str) -> str:
    lines = [START_MARKER]
    lines.append("TELEGRAM_SETTINGS = TelegramSettings(")
    lines.append(f"    bot_token={json.dumps(token)},")
    lines.append(f"    chat_id={json.dumps(chat_id)},")
    lines.append("    enabled=True,")
    lines.append(")")
    lines.append("FEED_SETTINGS = FeedSettings(")
    lines.append(f"    odds_api_key={json.dumps(api_key)},")
    lines.append(")")
    lines.append(END_MARKER)
    return "\n".join(lines)


def write_credentials(token: str, chat_id: str, api_key: str) -> None:
    """Insert (or replace) the managed credential block in settings.py."""
    text = SETTINGS_PATH.read_text(encoding="utf-8")
    block = build_block(token, chat_id, api_key)

    if START_MARKER in text and END_MARKER in text:
        pre = text.split(START_MARKER)[0]
        post = text.split(END_MARKER, 1)[1]
        text = pre + block + post
        mode = "updated"
    else:
        text = text.rstrip() + "\n\n\n" + block + "\n"
        mode = "added"

    SETTINGS_PATH.write_text(text, encoding="utf-8")
    print(f"  ✓ credentials {mode} in {SETTINGS_PATH}")


def main() -> None:
    print("=" * 66)
    print("  sporty-edge credential setup")
    print("=" * 66)
    print("  Press Enter to skip any question (you can re-run this later).\n")

    token = _ask(
        "  1) Telegram BOT TOKEN (from @BotFather): ",
        _valid_token,
        "a bot token looks like 7123456789:AAH3... and contains a colon",
    )
    chat_id = _ask(
        "  2) Your CHAT ID (from @userinfobot): ",
        _valid_chat_id,
        "a chat id is digits only, e.g. 123456789",
    )
    api_key = _ask(
        "  3) The Odds API KEY (from the-odds-api.com): ",
        _valid_api_key,
        "an API key is a long string from your account dashboard",
    )

    if not SETTINGS_PATH.exists():
        print(f"  ✗ {SETTINGS_PATH} not found — run this from the repo root.")
        sys.exit(1)

    write_credentials(token, chat_id, api_key)

    print("\n  Verifying settings load correctly...")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from config.settings import TELEGRAM_SETTINGS, FEED_SETTINGS; "
            "print('  ✓ settings OK — telegram enabled:', TELEGRAM_SETTINGS.enabled)",
        ],
        check=False,
    )
    if result.returncode != 0:
        print("  ✗ settings failed to import — paste the error above to support.")
        sys.exit(1)

    if token and chat_id:
        print("\n  Sending a live Telegram test...")
        tg = subprocess.run([sys.executable, "-m", "notify.telegram"], check=False)
        if tg.returncode == 0:
            print("  → If your phone buzzed, Telegram is DONE.")
        print("    (If it failed: open your bot in Telegram and press START, "
              "then re-run this script or `python3 -m notify.telegram`.)")
    else:
        print("\n  Skipped Telegram — run this script again anytime.")

    if api_key:
        print("\n  Next: test the odds feed with:")
        print("    python3 -m feeds.oddsapi --test")
    else:
        print("\n  No API key yet — free one at https://the-odds-api.com")
        print("  (Without it, the system runs on your CSV files instead.)")

    print("\n  All set. Run a session with:")
    print("    python3 run_daily.py --session morning")


if __name__ == "__main__":
    main()