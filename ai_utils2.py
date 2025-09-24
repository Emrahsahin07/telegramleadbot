"""Alternative AI utilities using Grok via OpenRouter.

This module provides a drop-in `classify_text_with_ai` that calls the
`x-ai/grok-4-fast:free` model exposed by OpenRouter instead of the
OpenAI Responses API used in `ai_utils.py`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

from config import logger
from ai_utils import apply_overrides, update_categories  # reuse existing heuristics

load_dotenv()

def get_openai_client():
    """Compatibility shim returning None; kept for legacy imports."""
    return None


OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "x-ai/grok-4-fast:free")

BASE_DIR = Path(__file__).resolve().parent
AI_TOKENS_LOG_PATH = BASE_DIR / "ai_tokens_grok.log"
CACHE_TTL_SECONDS = int(float(os.getenv("GROK_CACHE_TTL", str(6 * 3600))))
_grok_cache: Dict[str, Dict[str, Any]] = {}
_classify_cache = _grok_cache

SYSTEM_PROMPT = """You are a senior classification specialist. You receive end-user
messages from Telegram chats and must determine whether the message is a lead
request for a service. Always respond with a single-line JSON object that matches
this schema exactly:
{"relevant": true/false, "category": "string|null", "subcategory": "string|null",
 "region": "string|null", "explanation": "string", "confidence": 0.0-1.0}

Rules:
1. A relevant lead must contain an explicit or strongly implied request for a
   service listed in the provided category context.
2. If the message promotes, advertises, or offers a service/product, classify it
   as not relevant. That includes salesy language, multiple emojis that clearly
   market accommodation, and calls to action such as "пишите", "забронируй",
   "успей", etc.
3. Past experience reports, reviews, or feedback without a new request are not
   leads.
4. If the user asks for price, availability, or recommendation of a service,
   treat it as relevant even if phrased as a question.
5. Region may be extracted from city/region names (Анталия, Алания, Кемер, etc.).
   Leave null if the text does not specify one.
6. Explanation must be concise (<70 characters) and reference the trigger that
   drove your decision.
7. Use the provided category list exactly. Do not invent new categories.
8. Confidence: 0.9+ for clear leads, 0.6-0.8 for ambiguous leads, below 0.5 if
   the message is almost certainly irrelevant.

Return only JSON. Do not include markdown or additional commentary."""


def _get_headers() -> Dict[str, str]:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set in .env")
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    referer = os.getenv("OPENROUTER_HTTP_REFERER")
    if referer:
        headers["HTTP-Referer"] = referer
    title = os.getenv("OPENROUTER_APP_TITLE")
    if title:
        headers["X-Title"] = title
    return headers


def _log_usage(model_name: str, prompt_tokens: Optional[int], completion_tokens: Optional[int], cached_tokens: Optional[int] = None) -> None:
    ts = time.strftime("%m-%d %H:%M:%S")
    try:
        with AI_TOKENS_LOG_PATH.open("a", encoding="utf-8") as fh:
            total = None
            if prompt_tokens is not None and completion_tokens is not None:
                total = prompt_tokens + completion_tokens
            fh.write(
                f"{ts} | model={model_name} | in={prompt_tokens} cached_in={cached_tokens} "
                f"out={completion_tokens} total={total}\n"
            )
    except Exception:
        pass


def _grok_cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _grok_cache.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > CACHE_TTL_SECONDS:
        _grok_cache.pop(key, None)
        return None
    return entry["payload"].copy()


def _grok_cache_put(key: str, payload: Dict[str, Any]) -> None:
    if len(_grok_cache) > 5000:
        _grok_cache.pop(next(iter(_grok_cache)))
    _grok_cache[key] = {"ts": time.time(), "payload": payload.copy()}


def _build_user_prompt(category_context: str, text: str) -> str:
    return f"Категории для классификации: {category_context}\n\nСообщение: {text.strip()}"


def _call_grok(messages: list[dict[str, Any]], timeout: float = 45.0) -> dict[str, Any]:
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        OPENROUTER_URL,
        headers=_get_headers(),
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text}")
    data = response.json()
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected OpenRouter response: {data}") from exc
    usage = data.get("usage", {})
    _log_usage(
        OPENROUTER_MODEL,
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("cached_tokens"),
    )
    return json.loads(content)


def classify_text_with_ai(
    text: str,
    categories: list[str],
    locations: list[str],
    client_ai: Optional[Any] = None,
) -> Dict[str, Any]:
    """Classify message using Grok via OpenRouter."""
    category_context = ", ".join(sorted(dict.fromkeys(categories))) if categories else ""
    user_prompt = _build_user_prompt(category_context, text)
    cache_key = json.dumps([text, category_context], ensure_ascii=False)
    cached = _grok_cache_get(cache_key)
    if cached:
        return cached

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        result = _call_grok(messages)
    except Exception as exc:
        logger.error("GROK_CALL_FAILED: %s", exc, exc_info=True)
        return {
            "relevant": False,
            "category": None,
            "subcategory": None,
            "region": None,
            "explanation": f"Grok error: {exc}",
            "confidence": 0.0,
            "accepted": False,
        }

    relevant = bool(result.get("relevant"))
    category = result.get("category")
    subcategory = result.get("subcategory")
    region = result.get("region")
    explanation = result.get("explanation") or ""
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    classification = {
        "relevant": relevant,
        "category": category,
        "subcategory": subcategory,
        "region": region,
        "explanation": explanation[:70],
        "confidence": confidence,
        "accepted": relevant and confidence >= 0.79,
    }

    try:
        lower_text = text.lower()
        classification = apply_overrides(classification, lower_text, category)
    except Exception as override_err:
        logger.warning("apply_overrides failed: %s", override_err)

    _grok_cache_put(cache_key, classification)
    return classification


__all__ = ["classify_text_with_ai", "_classify_cache", "apply_overrides", "update_categories"]
