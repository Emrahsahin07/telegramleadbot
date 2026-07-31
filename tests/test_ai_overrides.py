from __future__ import annotations

import pytest

from ai_utils import apply_overrides


@pytest.mark.parametrize(
    "media_term",
    ["видео", "ролик", "фильм", "сюжет", "съёмка", "съемка", "фото"],
)
def test_media_context_does_not_force_rental_relevance(media_term: str) -> None:
    classification = {
        "relevant": False,
        "category": None,
        "subcategory": None,
        "confidence": 0.95,
        "explanation": "not a buyer request",
    }

    result = apply_overrides(
        classification,
        f"сниму {media_term} про квартиру в анталии",
        "недвижимость",
    )

    assert result["relevant"] is False
    assert result.get("subcategory") != "аренда"
    assert result["explanation"] != "Запрос аренды недвижимости"


def test_real_rental_request_still_uses_existing_override() -> None:
    classification = {
        "relevant": False,
        "category": None,
        "subcategory": None,
        "confidence": 0.95,
        "explanation": "not relevant",
    }

    result = apply_overrides(
        classification,
        "сниму квартиру в анталии на месяц",
        "недвижимость",
    )

    assert result["relevant"] is True
    assert result["category"] == "недвижимость"
    assert result["subcategory"] == "аренда"
    assert result["explanation"] == "Запрос аренды недвижимости"
