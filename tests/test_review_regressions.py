from __future__ import annotations

from collections import Counter
from unittest.mock import AsyncMock

import pytest

import delivery
import review_handler
from tests.helpers import RecordingBot, ReviewEvent, active_prefs
from tests.test_delivery_regressions import configure_delivery


@pytest.mark.known_bug
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Known bug: admin-approved review keeps its original confidence below "
        "0.79, so the normal delivery threshold silently drops it"
    ),
)
async def test_xfail_admin_approved_review_reaches_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XFAIL: explicit admin approval must bypass the auto-delivery threshold."""

    bot = RecordingBot()
    configure_delivery(monkeypatch, bot, {"202": active_prefs()})
    monkeypatch.setattr(review_handler, "metrics", Counter())
    monkeypatch.setattr(
        review_handler,
        "_store_admin_decision",
        AsyncMock(return_value=None),
    )

    lead_id = "review-lead"
    review_handler.pending_leads.clear()
    review_handler.pending_leads[lead_id] = {
        "timestamp": "01-01 12:00",
        "chat_info": "-100123 (Test group)",
        "text": "Нужен трансфер в Анталии",
        "link": "https://t.me/c/123/10",
        "sender_username": "sender",
        "sender_id": 777,
        "category": "трансфер",
        "subcategory": None,
        "region": "Анталия",
        "regions": ["Анталия"],
        "confidence": 0.75,
    }
    event = ReviewEvent(lead_id)

    await review_handler.handle_review_callback(event)

    assert 202 in bot.calls
