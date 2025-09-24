from __future__ import annotations
r"""
filters.py
Содержит функции и константы для быстрой фильтрации лид‑сообщений.

Функции:
• extract_stems(entry)        – рекурсивно собирает все keywords из категорий/подкатегорий.
• is_similar(a, b, threshold) – проверяет частичное совпадение строк через RapidFuzz.
• contains_negative(text)     – детектирует слова‑триггеры «осторожно, спам, мошенник…»,
                                игнорируя «не спам» благодаря отрицанию (?<!не\s).
"""

from typing import Any, List, Union
import re
from config import LOCATION_ALIAS
from constants import BUYER_TRIGGERS, SELLER_TERMS, OFFER_TERMS, REALTY_HINT_TERMS, PROMO_CTA_TERMS
from rapidfuzz import fuzz
import logging

# ---------- Contact detection ---------------------------------------------
_contact_regex = re.compile(
    r"(@[\w_]+|t\.me/[\w_\-]+|https?://t\.me/[\w_\-]+|\+?[\d\-\s\(\)]{7,}|whatsapp)",
    flags=re.IGNORECASE
)

def contains_contact(text: str) -> bool:
    """
    True if text contains contact info: @username, t.me links, phone numbers, or whatsapp.
    """
    return bool(_contact_regex.search(text))


# ---------- Negative context -----------------------------------------------
NEGATIVE_STEMS: List[str] = [
    "осторожн",
    "мошенник",
    "спам",
    "реклама",
    "мошенников",
    "предоплат",
    "обман",
    "кидали",
    "кидают",
    "не советую",
    "фейк",
    "дешево откровенно",
]

NEGATIVE_PHRASES: List[str] = [
    "не рекомендую",
    "остерегайтесь",
    "берегитесь",
    "не стоит",
    "развод",
    "отстой",
    "плохой сервис",
    "никому не советую"
]

_NEGATIVE_REGEX = re.compile(
    rf"(?<!\bне\s)({'|'.join(NEGATIVE_STEMS)})",
    flags=re.IGNORECASE
)

def contains_negative(text: str) -> bool:
    """True, если сообщение содержит нежелательный контекст."""
    text_lower = text.lower()
    # Проверка на отдельные слова
    if _NEGATIVE_REGEX.search(text_lower):
        return True
    # Проверка на фразы
    if any(phrase in text_lower for phrase in NEGATIVE_PHRASES):
        return True
    return False


# ---------- Fuzzy similarity ------------------------------------------------
def is_similar(a: str, b: str, threshold: int = 70) -> bool:
    """
    Быстрая проверка частичного совпадения строк с помощью RapidFuzz.
    threshold – минимальный процент совпадения (0–100).
    """
    return fuzz.partial_ratio(a.lower(), b.lower()) >= threshold


# ---------- Keyword stems extraction ---------------------------------------
def extract_stems(entry: Any) -> List[str]:
    """
    Рекурсивно собирает все строки‑ключевые слова из
    словаря категорий/подкатегорий:

    {
        "keywords": [...],
        "subcategories": {
            "подкат": { "keywords": [...] }
        }
    }
    """
    stems: List[str] = []
    if isinstance(entry, dict):
        if "keywords" in entry and isinstance(entry["keywords"], list):
            stems.extend(entry["keywords"])
        # рекурсивно вглубь всех ключей, кроме keywords
        for key, val in entry.items():
            if key != "keywords":
                stems.extend(extract_stems(val))
    elif isinstance(entry, list):
        for item in entry:
            stems.extend(extract_stems(item))
    elif isinstance(entry, str):
        stems.append(entry)
    return stems


# --- Word boundary matching -----------------------------------------------
def _contains_word(haystack: str, needle: str) -> bool:
    """
    True if `needle` appears in `haystack` as a separate word/morpheme,
    not as part of another word.
    """
    pattern = rf"(?<![а-яa-zё]){re.escape(needle)}(?![а-яa-zё])"
    return bool(re.search(pattern, haystack, flags=re.IGNORECASE))

# ---------- Advertisement detection ---------------------------------------
_PRICE_REGEX = re.compile(
    r"\b\d+(?:[\.,]\d+)?\s?(?:€|eur|евро|usd|\$|доллар|₺|try)(?:\b|[/\-]?[а-яa-z]{0,8})",
    flags=re.IGNORECASE
)

_AD_PATTERNS = [
    r"(\+?\d[\d\-\s\(\)]{6,}\d)",       # телефон
    _PRICE_REGEX,                                # цена + валюта (включая €/сутки и т.п.)
]

_SELLER_TERMS = SELLER_TERMS

# Реалестейт-хинты и паттерны планировок/площади
_REALTY_HINTS = REALTY_HINT_TERMS

_LAYOUT_REGEX = re.compile(r"\b[1-5]\s*([+xх])\s*[0-5]\b")
_AREA_REGEX = re.compile(r"\b(кв\.?\s?м|м2|м\^2|м²|sqm|sq\s?m)\b", flags=re.IGNORECASE)

# Дополнительные паттерны для фильтрации
_OFFER_TERMS = OFFER_TERMS

_REVIEW_TERMS = [
    "отлично", "хорошо", "плохо", "ужасно", "не рекомендую", "рекомендую", "советую", 
    "не советую", "опыт", "работал", "работала", "пользовался", "пользовалась"
]

def is_advertisement(text: str) -> bool:
    """
    True если похоже на объявление о продаже/аренде недвижимости:
    контакт + (термины продавца | цена/валюта | много хэштегов), либо явные ценники/термины.
    Не помечает как рекламу просто наличие контакта без продажи.
    """
    text_low = text.lower()
    has_contact = contains_contact(text_low)
    has_price_or_currency = any(
        (pat.search(text_low) if hasattr(pat, "search") else re.search(pat, text_low))
        for pat in _AD_PATTERNS
    )
    has_seller_terms = any(term in text_low for term in _SELLER_TERMS)
    has_layout = bool(_LAYOUT_REGEX.search(text_low))
    has_area = bool(_AREA_REGEX.search(text_low))
    has_realty_hint = any(tok in text_low for tok in _REALTY_HINTS)
    many_hashtags = len(re.findall(r"#\w+", text_low)) > 3
    
    # Дополнительные фильтры
    has_offer_terms = any(term in text_low for term in _OFFER_TERMS)
    has_review_terms = any(term in text_low for term in _REVIEW_TERMS)

    # Покупательские триггеры — не считаем рекламой. Разрешаем цену, если нет явных seller-терминов
    # Дополнительно учитываем маркеры бюджета (бюджет/до + цена) как спрос
    has_buyer = any(bt in text_low for bt in BUYER_TRIGGERS)
    has_budget_marker = bool(re.search(r"\b(бюджет|до)\b", text_low)) and has_price_or_currency
    if (has_buyer or has_budget_marker) and not has_seller_terms:
        return False

    sellerish = has_seller_terms or has_price_or_currency or has_layout or has_area or has_realty_hint

    price_hits = len(_PRICE_REGEX.findall(text_low))
    emoji_sections = text_low.count("🌟") + text_low.count("🌴") + text_low.count("✨")
    promo_cta_hits = sum(text_low.count(term) for term in PROMO_CTA_TERMS)

    # Базовые правила отсечения объявлений
    if (has_contact and (sellerish or many_hashtags)):
        return True
    if has_price_or_currency and (sellerish or has_contact or many_hashtags):
        return True
    if has_seller_terms and (has_contact or has_price_or_currency or many_hashtags or has_realty_hint):
        return True

    # Дополнительные маркеры многообъектных листингов
    if (price_hits >= 2 or emoji_sections >= 2 or promo_cta_hits >= 2) and not has_buyer:
        return True

    # Длинные описания-листинги без вопросительных слов — почти наверняка реклама
    question_triggers = ["?", "подскажите", "сколько", "где", "кто может", "нужен", "ищу", "нужна", "требуется"]
    if len(text_low) > 220 and sellerish and not any(q in text_low for q in question_triggers):
        return True
        
    # Фильтрация предложений услуг (не запросов)
    if has_offer_terms and not any(bt in text_low for bt in BUYER_TRIGGERS):
        return True
        
    # Фильтрация отзывов
    if has_review_terms and not any(bt in text_low for bt in BUYER_TRIGGERS):
        return True

    return False


# ---------------- Region & route utilities (moved from Botparsing.py) ----------------
# City keywords to canonical names (english/ru variants)
CITY_KEYWORDS = {
    "antalya": "Анталия", "анталия": "Анталия",
    "alanya": "Аланья", "алания": "Аланья",
    "kemer": "Кемер", "кемер": "Кемер",
    "belek": "Белек", "белек": "Белек",
    "side": "Сиде", "сиде": "Сиде",
    "istanbul": "Стамбул", "истамбул": "Стамбул", "стамбул": "Стамбул",
    "kundu": "Кунду", "кунду": "Кунду",
    "fethiye": "Фетхие", "фетхие": "Фетхие",
    "mersin": "Мерсин", "мерсин": "Мерсин",
    "beldibi": "Бельдиби", "бельдиби": "Бельдиби",
    "goynuk": "Гёйнюк","гейнюк": "Гёйнюк", "göynük": "Гёйнюк"
}

# Merge LOCATION_ALIAS with CITY_KEYWORDS to widen coverage (e.g., Фетхие/Fethiye)
MERGED_ALIASES = dict(LOCATION_ALIAS)
for k, v in CITY_KEYWORDS.items():
    MERGED_ALIASES[k] = v


def _alias_regex(alias: str) -> str:
    """Regex for alias tolerant to Russian endings. Latin remains strict."""
    alias = alias.lower()
    base = re.escape(alias)
    if re.search(r"[a-z]", alias):
        return base
    return base + r"[а-яё]*"

# Airports
AIRPORT_CODES = {
    "ayt": "Анталия",  # Antalya airport
    "ist": "Стамбул",  # Istanbul new airport
    "saw": "Стамбул",  # Sabiha Gökçen
}


def _all_locations_from_text(text_lower: str):
    locs = set()
    for alias, canon in MERGED_ALIASES.items():
        pat = _alias_regex(alias)
        if re.search(rf"(?<![а-яa-zё]){pat}(?![а-яa-zё])", text_lower):
            locs.add(canon)
    for code, canon in AIRPORT_CODES.items():
        if re.search(rf"\b{code}\b", text_lower):
            locs.add(canon)
    return list(locs)


def infer_region_from_text(title: str, username: str, text_lower: str):
    title_l = (title or "").lower()
    uname_l = (username or "").lower()

    # 1) Try to match from chat title first (strict word boundaries)
    for alias, canon in LOCATION_ALIAS.items():
        pat = _alias_regex(alias)
        if re.search(rf"(?<![а-яa-zё]){pat}(?![а-яa-zё])", title_l):
            return canon

    # 2) Then from message text (strict)
    for alias, canon in LOCATION_ALIAS.items():
        pat = _alias_regex(alias)
        if re.search(rf"(?<![а-яa-zё]){pat}(?![а-яa-zё])", text_lower):
            return canon

    # 3) Fall back to city keywords (title/username only, strict)
    for key, canon in CITY_KEYWORDS.items():
        pat = _alias_regex(key)
        if re.search(rf"(?<![а-яa-zё]){pat}(?![а-яa-zё])", title_l) or \
           re.search(rf"(?<![а-яa-zё]){pat}(?![а-яa-zё])", uname_l):
            return canon
    return None


def extract_transfer_route(text_lower: str, region_chat: Union[str, None]):
    """Return (pickup, destination) for transfer-like requests.
    Heuristics:
      - pickup: after 'из|с|от', 'из аэропорта', airport codes
      - destination: after 'в|до|к'
      - if only destination given → pickup = region_chat
      - if only pickup given → destination = None
      - if nothing found and region_chat is None → pickup=None
    """
    pickup = None
    destination = None

    # prepositions
    for alias, canon in MERGED_ALIASES.items():
        pat = _alias_regex(alias)
        if re.search(rf"\b(из|с|от)\s+{pat}\b", text_lower):
            pickup = canon
            break
    for alias, canon in MERGED_ALIASES.items():
        pat = _alias_regex(alias)
        if re.search(rf"\b(в|до|к)\s+{pat}\b", text_lower):
            destination = canon
            break

    # airports (prefer region_chat unless explicit code present)
    if re.search(r"\bв\s+аэропорт[а-яё]*\b", text_lower):
        if destination is None:
            for code, canon in AIRPORT_CODES.items():
                if re.search(rf"\b{code}\b", text_lower):
                    destination = canon
                    break
            if destination is None:
                destination = region_chat
    if re.search(r"\bиз\s+аэропорт[а-яё]*\b", text_lower):
        if pickup is None:
            for code, canon in AIRPORT_CODES.items():
                if re.search(rf"\b{code}\b", text_lower):
                    pickup = canon
                    break
            if pickup is None:
                pickup = region_chat

    # fallback destination from any loose location other than pickup
    if destination is None:
        for loc in _all_locations_from_text(text_lower):
            if loc != pickup:
                destination = loc
                break

    if pickup is None and region_chat:
        pickup = region_chat
    return pickup, destination
