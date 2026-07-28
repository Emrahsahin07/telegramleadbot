from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import ai_utils


class FailingChatCompletions:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def create(self, **_kwargs):
        raise self._error


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.chat = SimpleNamespace(completions=FailingChatCompletions(error))


def classify(monkeypatch: pytest.MonkeyPatch, response) -> dict:
    ai_utils._classify_cache.clear()
    monkeypatch.setattr(
        ai_utils,
        "_responses_create_with_retry",
        lambda *_args, **_kwargs: response,
    )
    return ai_utils.classify_text_with_ai(
        "Это рекламное предложение трансфера",
        ["трансфер"],
        ["Анталия"],
        client_ai=object(),
    )


@pytest.mark.correct
def test_pass_business_not_relevant_is_preserved_as_business_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PASS: a valid model response can explicitly classify a business non-lead."""

    response = SimpleNamespace(
        id="response-1",
        usage=None,
        output_text=json.dumps(
            {
                "relevant": False,
                "category": "трансфер",
                "subcategory": None,
                "region": "Анталия",
                "explanation": "предложение услуги",
                "confidence": 0.95,
            },
            ensure_ascii=False,
        ),
    )

    result = classify(monkeypatch, response)

    assert result["relevant"] is False
    assert result["explanation"] == "предложение услуги"
    assert "OpenAI error" not in result["explanation"]


@pytest.mark.known_bug
@pytest.mark.parametrize(
    "technical_error",
    [
        pytest.param(TimeoutError("AI timeout"), id="timeout"),
        pytest.param(RuntimeError("API unavailable"), id="api-error"),
    ],
)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Known bug: exhausted AI failures return relevant=False without a typed "
        "retry outcome, so the worker can treat a technical failure as a business drop"
    ),
)
def test_xfail_technical_ai_failure_has_retry_outcome(
    monkeypatch: pytest.MonkeyPatch,
    technical_error: Exception,
) -> None:
    """XFAIL: technical failures must be typed differently from not-relevant."""

    ai_utils._classify_cache.clear()

    def raise_primary(*_args, **_kwargs):
        raise technical_error

    monkeypatch.setattr(ai_utils, "_responses_create_with_retry", raise_primary)
    monkeypatch.setattr(ai_utils, "_apply_rate_limit", lambda: None)
    result = ai_utils.classify_text_with_ai(
        "Нужен трансфер",
        ["трансфер"],
        ["Анталия"],
        client_ai=FailingClient(technical_error),
    )

    assert result["relevant"] is False
    assert result.get("outcome") == "retry"
    assert result.get("technical_error") is True
