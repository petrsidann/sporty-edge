"""
setup_credentials.py — credential setup (re-runnable, Enter keeps values).

    python3 setup_credentials.py

Writes credentials to data/credentials.json (git-ignored).  Re-running is
always safe: pressing Enter keeps the existing value.

Get the values from:
    bot token : Telegram -> @BotFather -> /newbot  (or /mybots -> API Token)
    chat id   : Telegram -> @userinfobot
    api key   : https://the-odds-api.com  (free registration)
Then press START once on your bot in Telegram (bots cannot message you first).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CRED_PATH = Path("data") / "credentials.json"

_VERIFY_SNIPPET = (
    "from config.settings import TELEGRAM_SETTINGS as T, FEED_SETTINGS as F; "
    "print('settings OK | telegram:', bool(T.bot_token and T.chat_id), "
    "'| api key:', bool(F.odds_api_key), '| failover keys:', len(F.api_keys))"
)


def _load_existing() -> dict:
    """Existing credentials (if any) so Enter can keep them."""
    try:
        data = json.loads(CRED_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _ask(prompt: str, default: str, validate, hint: str) -> str:
    """Prompt with a keep-current default; empty input keeps/skips."""
    suffix = " [Enter = keep current]" if default else " [Enter = skip]"
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if not value:
            return default
        if validate(value):
            return value
        print(f"    ✗ {hint}")


def _valid_token(v: str) -> bool:
    return ":" in v and len(v) > 20


def _valid_chat_id(v: str) -> bool:
    return v.isdigit()


def _valid_key(v: str) -> bool:
    return len(v) >= 8


def main() -> None:
    print("=" * 66)
    print("  sporty-edge credential setup")
    print("=" * 66)
    print("  Stored in data/credentials.json (git-ignored, never committed).")
    print("  Press Enter to keep any existing value.\n")

    existing = _load_existing()

    token = _ask(
        "  1) Telegram BOT TOKEN",
        str(existing.get("telegram_bot_token", "")),
        _valid_token,
        "a bot token looks like 7123456789:AAH3... and contains a colon",
    )
    chat_id = _ask(
        "  2) Your CHAT ID",
        str(existing.get("telegram_chat_id", "")),
        _valid_chat_id,
        "a chat id is digits only, e.g. 123456789",
    )
    api_key = _ask(
        "  3) The Odds API KEY",
        str(existing.get("odds_api_key", "")),
        _valid_key,
        "an API key is a long string from your dashboard",
    )
    extra_raw = _ask(
        "  4) Extra feed keys for failover (comma-separated)",
        ",".join(existing.get("odds_api_keys_extra", [])),
        lambda v: all(_valid_key(k.strip()) for k in v.split(",") if k.strip()),
        "comma-separated API keys, each at least 8 chars",
    )
    extra_keys = [k.strip() for k in extra_raw.split(",") if k.strip()]

    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRED_PATH.write_text(
        json.dumps(
            {
                "telegram_bot_token": token,
                "telegram_chat_id": chat_id,
                "odds_api_key": api_key,
                "odds_api_keys_extra": extra_keys,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  ✓ credentials written to {CRED_PATH}")

    print("  Verifying settings load correctly...")
    result = subprocess.run(
        [sys.executable, "-c", _VERIFY_SNIPPET], check=False
    )
    if result.returncode != 0:
        print("  ✗ settings failed to import — paste the error above to support.")
        sys.exit(1)

    if token and chat_id:
        print("\n  Sending a live Telegram test...")
        subprocess.run([sys.executable, "-m", "notify.telegram"], check=False)

    print("\n  Next steps:")
    print("    python3 -m feeds.oddsapi --test    # one live fetch")
    print("    python3 run_daily.py               # full session")


if __name__ == "__main__":
    main()