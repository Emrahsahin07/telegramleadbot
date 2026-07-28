"""Test bootstrap that prevents imports from touching production runtime files."""

from __future__ import annotations

import logging
import logging.handlers
import os
from typing import Any, Callable

import pytest
import telethon


class NoopTelegramClient:
    """Import-safe stand-in used only while config.py is loaded by tests."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._connected = False

    def on(self, *_args: Any, **_kwargs: Any) -> Callable:
        def decorator(func: Callable) -> Callable:
            return func

        return decorator

    def is_connected(self) -> bool:
        return self._connected


# config.py constructs clients and a RotatingFileHandler at import time. Replacing
# these constructors before test modules are imported keeps all tests offline and
# prevents writes to real Telethon sessions and bot.log.
telethon.TelegramClient = NoopTelegramClient
logging.handlers.RotatingFileHandler = lambda *_args, **_kwargs: logging.NullHandler()

os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test-api-hash")
os.environ.setdefault("LEADBOT_TOKEN", "12345:test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ["CLEAR_QUEUE_ON_START"] = "0"
os.environ["SEND_NOTIFICATIONS"] = "1"
os.environ["NOTIFY_SEND_ERRORS"] = "0"


@pytest.fixture(autouse=True)
def no_identity_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use fake bot identities and must not depend on developer .env values."""

    monkeypatch.delenv("TARGET_BOT_ID", raising=False)
    monkeypatch.delenv("BOT_ID", raising=False)
    monkeypatch.setenv("SEND_NOTIFICATIONS", "1")
    monkeypatch.setenv("NOTIFY_SEND_ERRORS", "0")
