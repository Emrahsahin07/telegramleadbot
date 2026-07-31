from __future__ import annotations

import pytest

import Botparsing
from filters import infer_region_from_text


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("#Анталия", "Анталия", id="location"),
        pytest.param("#трансфер", "трансфер", id="category"),
        pytest.param(
            "#нужен #трансфер #Анталия",
            "нужен трансфер Анталия",
            id="multiple",
        ),
        pytest.param(
            "Нужен #трансфер сегодня",
            "Нужен трансфер сегодня",
            id="ordinary-text",
        ),
    ],
)
def test_hashtag_marker_is_removed_but_word_is_preserved(
    source: str,
    expected: str,
) -> None:
    assert Botparsing._normalize_hashtags(source) == expected


def test_location_hashtag_remains_detectable() -> None:
    normalized = Botparsing._normalize_hashtags("нужно в #Анталии").lower()

    assert infer_region_from_text("", "", normalized) == "Анталия"


def test_category_hashtag_remains_a_normal_keyword() -> None:
    normalized = Botparsing._normalize_hashtags("#трансфер нужен").lower()
    stems = {Botparsing._stem(token) for token in Botparsing.WORD_RE.findall(normalized)}

    assert Botparsing._stem("трансфер") in stems


def test_hashtag_does_not_create_location_substring_match() -> None:
    normalized = Botparsing._normalize_hashtags("новый #анталийский чат").lower()

    assert infer_region_from_text("", "", normalized) is None
