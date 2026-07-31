from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import Botparsing
import message_queue
import review_handler
from decision_policy import AUTO_SEND, REJECT, SHADOW_BORDERLINE, decide_lead
from delivery import DeliveryResult
from tests.helpers import configure_temp_queue


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        pytest.param(0.6999, REJECT, id="below-shadow"),
        pytest.param(0.70, SHADOW_BORDERLINE, id="shadow-lower-bound"),
        pytest.param(0.7899, SHADOW_BORDERLINE, id="shadow-upper-bound"),
        pytest.param(0.79, AUTO_SEND, id="auto-send-boundary"),
    ],
)
def test_pass_automatic_decision_boundaries(confidence: float, expected: str) -> None:
    assert decide_lead(True, confidence).decision == expected


def test_pass_not_relevant_is_rejected_even_with_high_confidence() -> None:
    assert decide_lead(False, 0.99).decision == REJECT


class CandidateEvent:
    id = 10
    chat_id = -100123
    sender_id = 777
    raw_text = "Нужен трансфер в Анталии"
    is_group = True
    is_channel = False

    async def get_chat(self):
        return SimpleNamespace(title="Test group", username="test_group")

    async def get_sender(self):
        return SimpleNamespace(
            id=self.sender_id,
            first_name="Sender",
            username="sender",
            bot=False,
        )


def configure_candidate_path(monkeypatch: pytest.MonkeyPatch, confidence: float) -> None:
    monkeypatch.setattr(Botparsing, "metrics", Counter())
    monkeypatch.setattr(Botparsing, "SELF_ID", None)
    monkeypatch.setattr(Botparsing, "SELF_USERNAME", None)
    monkeypatch.setattr(Botparsing, "REGION_CACHE", {})
    monkeypatch.setattr(Botparsing, "_should_drop_duplicate", lambda *_args: False)
    monkeypatch.setattr(Botparsing, "contains_negative", lambda *_args: False)
    monkeypatch.setattr(Botparsing, "is_advertisement", lambda *_args: False)
    monkeypatch.setattr(
        Botparsing,
        "infer_region_from_text",
        lambda *_args: "Анталия",
    )
    monkeypatch.setattr(
        Botparsing,
        "_all_locations_from_text",
        lambda *_args: ["Анталия"],
    )
    categories = {
        "трансфер": {"keywords": ["трансфер"], "subcategories": {}}
    }
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
    monkeypatch.setattr(Botparsing, "apply_overrides", lambda cla, *_args: cla)
    monkeypatch.setattr(
        Botparsing,
        "_classify_message_with_ai",
        AsyncMock(
            return_value={
                "relevant": True,
                "category": "трансфер",
                "subcategory": None,
                "region": "Анталия",
                "explanation": "запрос трансфера",
                "raw_confidence": 0.73,
                "confidence": confidence,
            }
        ),
    )


@pytest.mark.correct
@pytest.mark.parametrize("review_flag", ["0", "1"])
async def test_pass_shadow_is_durable_without_outbox_or_admin_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    review_flag: str,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    configure_candidate_path(monkeypatch, 0.75)
    monkeypatch.setenv("ENABLE_ADMIN_REVIEW", review_flag)
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_PROMPT_ID", "test-prompt")
    monkeypatch.setenv("OPENAI_PROMPT_VERSION", "7")
    admin_review = AsyncMock(return_value=None)
    monkeypatch.setattr(review_handler, "send_review_to_admin", admin_review)

    await Botparsing.process_message(CandidateEvent())

    event_id = message_queue.build_delivery_event_id(-100123, 10)
    telemetry = await message_queue.get_shadow_decision(event_id)
    assert telemetry["decision"] == SHADOW_BORDERLINE
    assert telemetry["category"] == "трансфер"
    assert telemetry["location"] == "Анталия"
    assert telemetry["raw_confidence"] == pytest.approx(0.73)
    assert telemetry["calibrated_confidence"] == pytest.approx(0.75)
    assert telemetry["model"] == "test-model"
    assert telemetry["prompt_id"] == "test-prompt"
    assert telemetry["prompt_version"] == "7"
    assert telemetry["config_version"]
    assert telemetry["policy_version"] == "automatic-v1"
    assert await message_queue.get_delivery_outbox_rows(event_id) == []
    admin_review.assert_not_awaited()


@pytest.mark.correct
async def test_pass_current_high_confidence_path_still_calls_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    configure_candidate_path(monkeypatch, 0.79)
    delivery_call = AsyncMock(return_value=DeliveryResult(mode="outbox"))
    monkeypatch.setattr(Botparsing, "send_lead_to_users", delivery_call)

    await Botparsing.process_message(CandidateEvent())

    delivery_call.assert_awaited_once()
    assert delivery_call.await_args.kwargs["confidence"] == pytest.approx(0.79)
    assert await message_queue.get_shadow_decision("-100123:10") is None
