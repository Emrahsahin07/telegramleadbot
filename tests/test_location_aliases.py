from __future__ import annotations

import pytest

from config import normalize_location
from filters import _all_locations_from_text, extract_transfer_route, infer_region_from_text


SAFE_FORMS = {
    "Анталия": (
        "анталия", "анталии", "анталию", "анталией",
        "анталья", "антальи", "анталью", "антальей", "анталье",
    ),
    "Аланья": (
        "алания", "алании", "аланию", "аланией",
        "аланья", "аланьи", "аланью", "аланьей", "аланье",
    ),
    "Авсаллар": ("авсаллар", "авсаллара", "авсалларе", "авсаллару", "авсалларом"),
    "Кемер": ("кемер", "кемера", "кемере", "кемеру", "кемером"),
    "Стамбул": ("стамбул", "стамбула", "стамбуле", "стамбулу", "стамбулом"),
    "Бельдиби": ("бельдиби", "белдиби"),
    "Белек": ("белек", "белека", "белеке", "белеку", "белеком"),
    "Гёйнюк": ("гёйнюк", "гёйнюка", "гёйнюке", "гёйнюку", "гёйнюком", "гейнюк", "гейнюка", "гейнюке", "гейнюку", "гейнюком"),
    "Манавгат": ("манавгат", "манавгата", "манавгате", "манавгату", "манавгатом"),
    "Чамьюва": ("чамьюва", "чамьювы", "чамьюве", "чамьюву", "чамьювой"),
    "Турция": ("турция", "турции", "турцию", "турцией"),
    "Мерсин": ("мерсин", "мерсина", "мерсине", "мерсину", "мерсином"),
    "Сиде": ("сиде",),
    "Фетхие": ("фетхие",),
}


@pytest.mark.parametrize(
    ("canonical", "form"),
    [(canonical, form) for canonical, forms in SAFE_FORMS.items() for form in forms],
)
def test_safe_location_forms_normalize_to_existing_canonical(
    canonical: str,
    form: str,
) -> None:
    assert normalize_location(form) == canonical
    assert infer_region_from_text("", "", f"нужно в {form}") == canonical


@pytest.mark.parametrize("text", ["сиденье", "кемерово", "анталийский", "мерсиновый"])
def test_location_aliases_do_not_match_inside_other_words(text: str) -> None:
    assert infer_region_from_text("", "", text) is None
    assert _all_locations_from_text(text) == []


def test_multi_location_and_transfer_route_remain_canonical() -> None:
    text = "нужен трансфер из анталии в кемер"

    assert set(_all_locations_from_text(text)) == {"Анталия", "Кемер"}
    assert extract_transfer_route(text, None) == ("Анталия", "Кемер")
