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


class ValidFallbackClient:
    def __init__(self, result: dict) -> None:
        self._result = result
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create_completion)
        )

    def create_completion(self, **_kwargs):
        return SimpleNamespace(
            id="fallback-response",
            usage=None,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(self._result, ensure_ascii=False)
                    )
                )
            ],
        )


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


@pytest.mark.parametrize(
    "technical_error",
    [
        pytest.param(TimeoutError("AI timeout"), id="timeout"),
        pytest.param(RuntimeError("API unavailable"), id="api-error"),
    ],
)
def test_pass_technical_ai_failure_raises_typed_retry_error(
    monkeypatch: pytest.MonkeyPatch,
    technical_error: Exception,
) -> None:
    ai_utils._classify_cache.clear()

    def raise_primary(*_args, **_kwargs):
        raise technical_error

    monkeypatch.setattr(ai_utils, "_responses_create_with_retry", raise_primary)
    monkeypatch.setattr(ai_utils, "_apply_rate_limit", lambda: None)
    with pytest.raises(ai_utils.AIClassificationTechnicalError):
        ai_utils.classify_text_with_ai(
            "Нужен трансфер",
            ["трансфер"],
            ["Анталия"],
            client_ai=FailingClient(technical_error),
        )


def test_pass_invalid_ai_response_raises_typed_retry_error_without_valid_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(id="invalid-response", usage=None, output_text="not JSON")

    with pytest.raises(ai_utils.AIClassificationTechnicalError):
        classify(monkeypatch, response)


def test_pass_invalid_primary_response_uses_valid_existing_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_utils._classify_cache.clear()
    monkeypatch.setattr(
        ai_utils,
        "_responses_create_with_retry",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="invalid-primary",
            usage=None,
            output_text="not JSON",
        ),
    )
    monkeypatch.setattr(ai_utils, "_apply_rate_limit", lambda: None)
    fallback_result = {
        "relevant": False,
        "category": "трансфер",
        "subcategory": None,
        "region": "Анталия",
        "explanation": "предложение услуги",
        "confidence": 0.95,
    }

    result = ai_utils.classify_text_with_ai(
        "Это рекламное предложение трансфера",
        ["трансфер"],
        ["Анталия"],
        client_ai=ValidFallbackClient(fallback_result),
    )

    assert result["relevant"] is False
    assert result["explanation"] == "предложение услуги"


@pytest.mark.correct
def test_pass_business_relevant_is_preserved_as_normal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        id="response-true",
        usage=None,
        output_text=json.dumps(
            {
                "relevant": True,
                "category": "трансфер",
                "subcategory": None,
                "region": "Анталия",
                "explanation": "прямой запрос",
                "confidence": 0.95,
            },
            ensure_ascii=False,
        ),
    )

    result = classify(monkeypatch, response)

    assert result["relevant"] is True
    assert result["category"] == "трансфер"
