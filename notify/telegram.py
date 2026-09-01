"""
Telegram notification layer.

Sends pipeline reports, pick sheets, and summaries to your chat using only
the Python standard library (urllib) — no extra dependency.

Setup (one time, 2 minutes):
    1. Telegram -> message @BotFather -> /newbot -> copy the bot token.
    2. Telegram -> message @userinfobot -> copy your chat id.
    3. Put both in config/settings.py TelegramSettings(...) with enabled=True,
       or set environment variables TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
       (environment variables take priority).
    4. Open your bot in Telegram and press Start once, so it can DM you.

Failure policy: Telegram problems NEVER crash the pipeline.  Everything is
still printed to the console; the notifier just reports and moves on.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request

from config.settings import TELEGRAM_SETTINGS
from slips.platform_slip import PlatformSheet

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_MESSAGE_LEN = 3900  # Telegram hard limit is 4096; leave headroom.
_TIMEOUT_SECONDS = 10


class TelegramNotifier:
    """Thin, resilient wrapper around the Telegram Bot API sendMessage."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        # Environment variables take priority over config file values.
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "") \
            or TELEGRAM_SETTINGS.bot_token
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "") \
            or TELEGRAM_SETTINGS.chat_id
        self.enabled = (
            enabled
            if enabled is not None
            else TELEGRAM_SETTINGS.enabled
        )

    # ------------------------------------------------------------------ #

    @property
    def is_configured(self) -> bool:
        """True when enabled and both credentials are present."""
        return bool(self.enabled and self.bot_token and self.chat_id)

    def send(self, text: str) -> bool:
        """Send ``text``, splitting into chunks if needed. Returns success."""
        if not self.is_configured:
            return False
        chunks = self._chunk(text)
        ok = True
        for chunk in chunks:
            ok = self._post(chunk) and ok
        return ok

    def send_sheet(self, sheet: PlatformSheet, bet_id: str = "") -> bool:
        """Send a rendered platform pick sheet."""
        return self.send(sheet.render(bet_id=bet_id))

    # ------------------------------------------------------------------ #

    @staticmethod
    def _chunk(text: str) -> list[str]:
        """Split long text on line boundaries under the Telegram limit."""
        if len(text) <= _MAX_MESSAGE_LEN:
            return [text]
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for line in text.splitlines():
            if size + len(line) + 1 > _MAX_MESSAGE_LEN and current:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _post(self, text: str) -> bool:
        """POST one message; never raises — returns False on any failure."""
        if not self.bot_token or not self.chat_id:
            return False
        payload = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text}
        ).encode("utf-8")
        url = _API_URL.format(token=self.bot_token)
        try:
            req = urllib.request.Request(
                url, data=payload, method="POST"
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            print("  !! Telegram send failed — continuing (console still has everything).")
            return False
