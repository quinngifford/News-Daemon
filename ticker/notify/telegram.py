"""Telegram channel — fastest path to a buzzing phone, no store review needed.

Setup:
  1. Message @BotFather, /newbot, copy the token.
  2. Message your new bot once, then read your chat id from
     https://api.telegram.org/bot<TOKEN>/getUpdates
  3. export TICKER_TELEGRAM_TOKEN=... TICKER_TELEGRAM_CHAT_ID=...

Operational note: set a distinct notification sound for this chat, and allow-list
it in your phone's Focus/Do-Not-Disturb rules. Otherwise the 04:00 case — the
one you actually built this for — silently fails.
"""

from __future__ import annotations

import logging
import os

import httpx

from ticker.models import Alert
from ticker.notify.dispatcher import format_alert

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


class TelegramChannel:
    name = "telegram"

    def __init__(
        self,
        client: httpx.AsyncClient,
        token_env: str = "TICKER_TELEGRAM_TOKEN",
        chat_id_env: str = "TICKER_TELEGRAM_CHAT_ID",
        markets: list[dict] | None = None,
    ) -> None:
        self.client = client
        self.token = os.environ.get(token_env, "")
        self.chat_id = os.environ.get(chat_id_env, "")
        self.markets = markets or []
        if not self.token or not self.chat_id:
            log.warning(
                "telegram channel inactive: set %s and %s", token_env, chat_id_env
            )

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def warm(self) -> None:
        """Resolve DNS + establish TLS now, not at fire time."""
        if not self.configured:
            return
        await self.client.get(f"{API}/bot{self.token}/getMe", timeout=5.0)

    async def send_text(self, text: str) -> None:
        """Operational message (not an event alert).

        Used for things you must know about the monitor itself: a source going
        silent, or the X budget cutting the stream off. These are failures OF the
        watcher, which you would otherwise only discover by missing an event.
        """
        if not self.configured:
            log.warning("telegram not configured; ops message dropped: %s", text)
            return
        await self.client.post(
            f"{API}/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=5.0,
        )

    async def send(self, alert: Alert) -> None:
        if not self.configured:
            return
        title, body = format_alert(alert, self.markets)
        await self.client.post(
            f"{API}/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": f"*{title}*\n\n{body}",
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=5.0,
        )
