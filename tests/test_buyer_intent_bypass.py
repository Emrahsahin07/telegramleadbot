from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import Botparsing
from delivery import DeliveryResult


class BuyerIntentEvent:
    id = 501
    chat_id = -100501
    sender_id = 777
    is_group = True
    is_channel = False

    def __init__(self, text: str, title: str = "Общий чат") -> None:
        self.raw_text = text
        self.title = title

    async def get_chat(self):
        return SimpleNamespace(title=self.title, username="general_chat")

    async def get_sender(self):
        return SimpleNamespace(
            id=self.sender_id,
            first_name="Sender",
            username="sender",
            bot=False,
        )


def configure_bypass_path(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(Botparsing, "metrics", Counter())
    monkeypatch.setattr(Botparsing, "SELF_ID", None)
    monkeypatch.setattr(Botparsing, "SELF_USERNAME", None)
    monkeypatch.setattr(Botparsing, "REGION_CACHE", {})
    monkeypatch.setattr(Botparsing, "_should_drop_duplicate", lambda *_args: False)
    categories = {"трансфер": {"keywords": ["трансфер"], "subcategories": {}}}
    subscriptions = {
        "101": {
            "categories": ["трансфер"],
            "locations": ["Анталия"],
            "subcats": {},
        }
    }
    monkeypatch.setattr(Botparsing, "categories", categories)
    monkeypatch.setattr(Botparsing, "subscriptions", subscriptions)
    monkeypatch.setattr(
        Botparsing,
        "TOP_KEYWORD_STEMS",
        {Botparsing._stem("трансфер")},
    )
    ai_call = AsyncMock(
        return_value={
            "relevant": True,
            "category": "трансфер",
            "subcategory": None,
            "region": "Анталия",
            "explanation": "buyer request",
            "raw_confidence": 0.90,
            "confidence": 0.90,
        }
    )
    monkeypatch.setattr(Botparsing, "_classify_message_with_ai", ai_call)
    monkeypatch.setattr(Botparsing, "apply_overrides", lambda cla, *_args: cla)
    monkeypatch.setattr(
        Botparsing,
        "send_lead_to_users",
        AsyncMock(return_value=DeliveryResult(mode="outbox")),
    )
    return ai_call


@pytest.mark.correct
async def test_strong_buyer_intent_with_known_location_reaches_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_call = configure_bypass_path(monkeypatch)

    await Botparsing.process_message(BuyerIntentEvent("Ищу водителя в Анталии"))

    ai_call.assert_awaited_once()
    assert Botparsing.metrics["regional_keyword_bypass"] == 1
    delivery_call = Botparsing.send_lead_to_users
    delivery_call.assert_awaited_once()
    assert delivery_call.await_args.kwargs["allow_keyword_bypass"] is True


@pytest.mark.correct
async def test_bypass_does_not_auto_send_when_ai_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_call = configure_bypass_path(monkeypatch)
    ai_call.return_value = {
        **ai_call.return_value,
        "relevant": False,
        "confidence": 0.99,
    }

    await Botparsing.process_message(BuyerIntentEvent("Ищу водителя в Анталии"))

    ai_call.assert_awaited_once()
    Botparsing.send_lead_to_users.assert_not_awaited()


@pytest.mark.correct
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Предлагаю услуги водителя в Анталии @driver", id="seller-ad"),
        pytest.param("Пользовался услугами водителя в Анталии, кто ещё?", id="review-discussion"),
        pytest.param("Можно водителя в Анталии?", id="weak-intent"),
    ],
)
async def test_unsafe_or_weak_messages_do_not_bypass_to_ai(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    ai_call = configure_bypass_path(monkeypatch)

    await Botparsing.process_message(BuyerIntentEvent(text))

    ai_call.assert_not_awaited()
    assert Botparsing.metrics["regional_keyword_bypass"] == 0


@pytest.mark.correct
async def test_strong_buyer_intent_without_location_keeps_existing_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_call = configure_bypass_path(monkeypatch)

    await Botparsing.process_message(BuyerIntentEvent("Ищу водителя"))

    ai_call.assert_not_awaited()
    assert Botparsing.metrics["no_region"] == 1
