"""Alternative AI utilities using Grok via OpenRouter.

This module provides a drop-in `classify_text_with_ai` that calls the
`x-ai/grok-4-fast:free` model exposed by OpenRouter instead of the
OpenAI Responses API used in `ai_utils.py`.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

from config import logger
from ai_utils import (
    apply_overrides,
    update_categories,
    classify_text_with_ai as _classify_with_openai,
    get_openai_client as _get_openai_client,
)  # reuse existing heuristics

load_dotenv()

def get_openai_client():
    """Return an OpenAI client for fallback flows; keep legacy signature."""
    try:
        return _get_openai_client()
    except Exception as exc:
        logger.warning("OPENAI_CLIENT_INIT_FAILED: %s", exc)
        return None


OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "x-ai/grok-4-fast:free")
OPENROUTER_DEEPSEEK_URL = os.getenv("OPENROUTER_DEEPSEEK_URL", OPENROUTER_URL)
OPENROUTER_DEEPSEEK_MODEL = os.getenv("OPENROUTER_DEEPSEEK_MODEL", "deepseek/deepseek-chat-v3.1:free")
OPENROUTER_OSS_URL = os.getenv("OPENROUTER_OSS_URL", OPENROUTER_URL)
OPENROUTER_OSS_MODEL = os.getenv("OPENROUTER_OSS_MODEL", "openai/gpt-oss-20b:free")
OPENROUTER_GLM_URL = os.getenv("OPENROUTER_GLM_URL", OPENROUTER_URL)
OPENROUTER_GLM_MODEL = os.getenv("OPENROUTER_GLM_MODEL", "z-ai/glm-4.5-air:free")
ENABLE_DEEPSEEK_FALLBACK = os.getenv("ENABLE_DEEPSEEK_FALLBACK", "1") == "1"
ENABLE_OSS_FALLBACK = os.getenv("ENABLE_OSS_FALLBACK", "1") == "1"
ENABLE_GLM_FALLBACK = os.getenv("ENABLE_GLM_FALLBACK", "1") == "1"

BASE_DIR = Path(__file__).resolve().parent
AI_TOKENS_LOG_PATH = BASE_DIR / "ai_tokens_grok.log"
AI_TOKENS_DEEPSEEK_LOG_PATH = BASE_DIR / "ai_tokens_deepseek.log"
AI_TOKENS_OSS_LOG_PATH = BASE_DIR / "ai_tokens_oss.log"
AI_TOKENS_GLM_LOG_PATH = BASE_DIR / "ai_tokens_glm.log"
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
4. Announcements about chat rules, welcome messages, or instructions on how to
   post ads are not leads; treat them as irrelevant.
5. If the user asks for price, availability, or recommendation of a service,
   treat it as relevant even if phrased as a question.
6. Region may be extracted from city/region names (Анталия, Алания, Кемер, etc.).
   Leave null if the text does not specify one.
7. Explanation must be concise (<70 characters) and reference the trigger that
   drove your decision.
8. Use the provided category list exactly. Do not invent new categories.
9. Confidence: 0.9+ for clear leads, 0.6-0.8 for ambiguous leads, below 0.5 if
   the message is almost certainly irrelevant.

Example non-lead: "Приветствуем … здесь вы можете размещать объявления" →
{"relevant": false, ...}.

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


def _log_usage(
    model_name: str,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    cached_tokens: Optional[int] = None,
    *,
    log_path: Path = AI_TOKENS_LOG_PATH,
) -> None:
    ts = time.strftime("%m-%d %H:%M:%S")
    try:
        with log_path.open("a", encoding="utf-8") as fh:
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


def _call_openrouter_model(
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    timeout: float,
    url: str,
    log_path: Path,
) -> dict[str, Any]:
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        url,
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
        model_name,
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("cached_tokens"),
        log_path=log_path,
    )
    return json.loads(content)


def _call_grok(messages: list[dict[str, Any]], timeout: float = 45.0) -> dict[str, Any]:
    return _call_openrouter_model(
        OPENROUTER_MODEL,
        messages,
        timeout=timeout,
        url=OPENROUTER_URL,
        log_path=AI_TOKENS_LOG_PATH,
    )


def _call_deepseek(messages: list[dict[str, Any]], timeout: float = 45.0) -> dict[str, Any]:
    return _call_openrouter_model(
        OPENROUTER_DEEPSEEK_MODEL,
        messages,
        timeout=timeout,
        url=OPENROUTER_DEEPSEEK_URL,
        log_path=AI_TOKENS_DEEPSEEK_LOG_PATH,
    )


def _call_oss(messages: list[dict[str, Any]], timeout: float = 45.0) -> dict[str, Any]:
    return _call_openrouter_model(
        OPENROUTER_OSS_MODEL,
        messages,
        timeout=timeout,
        url=OPENROUTER_OSS_URL,
        log_path=AI_TOKENS_OSS_LOG_PATH,
    )


def _call_glm(messages: list[dict[str, Any]], timeout: float = 45.0) -> dict[str, Any]:
    return _call_openrouter_model(
        OPENROUTER_GLM_MODEL,
        messages,
        timeout=timeout,
        url=OPENROUTER_GLM_URL,
        log_path=AI_TOKENS_GLM_LOG_PATH,
    )


def _run_deepseek_fallback(
    messages: list[dict[str, Any]],
    text: str,
    cache_key: str,
    deadline: float,
    grok_error: str,
) -> Optional[Dict[str, Any]]:
    if not ENABLE_DEEPSEEK_FALLBACK:
        return None

    logger.info("DEEPSEEK_FALLBACK_ACTIVATED due to OpenRouter error: %s", grok_error)
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        logger.warning("DEEPSEEK_FALLBACK_SKIPPED: timeout budget exhausted")
        return None
    secondary_cap = max(2.0, min(remaining * 0.6, 6.0))
    deepseek_timeout = _resolve_timeout(deadline, "DEEPSEEK_TIMEOUT", secondary_cap)

    try:
        result = _call_deepseek(messages, timeout=deepseek_timeout)
    except Exception as exc:
        logger.error("DEEPSEEK_CALL_FAILED: %s", exc, exc_info=True)
        return None

    return _finalize_classification_result(result, text, cache_key)


def _run_glm_fallback(
    messages: list[dict[str, Any]],
    text: str,
    cache_key: str,
    deadline: float,
    previous_error: str,
) -> Optional[Dict[str, Any]]:
    if not ENABLE_GLM_FALLBACK:
        return None

    logger.info("GLM_FALLBACK_ACTIVATED due to previous error: %s", previous_error)
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        logger.warning("GLM_FALLBACK_SKIPPED: timeout budget exhausted")
        return None
    glm_timeout = _resolve_timeout(deadline, "GLM_TIMEOUT", max(2.0, min(remaining * 0.6, 8.0)))

    try:
        result = _call_glm(messages, timeout=glm_timeout)
    except Exception as exc:
        logger.error("GLM_CALL_FAILED: %s", exc, exc_info=True)
        return None

    return _finalize_classification_result(result, text, cache_key)


def _run_oss_fallback(
    messages: list[dict[str, Any]],
    text: str,
    cache_key: str,
    deadline: float,
    previous_error: str,
) -> Optional[Dict[str, Any]]:
    if not ENABLE_OSS_FALLBACK:
        return None

    logger.info("OSS_FALLBACK_ACTIVATED due to previous error: %s", previous_error)
    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        logger.warning("OSS_FALLBACK_SKIPPED: timeout budget exhausted")
        return None
    oss_timeout = _resolve_timeout(deadline, "OSS_TIMEOUT", max(2.0, min(remaining * 0.6, 8.0)))

    try:
        result = _call_oss(messages, timeout=oss_timeout)
    except Exception as exc:
        logger.error("OSS_CALL_FAILED: %s", exc, exc_info=True)
        return None

    return _finalize_classification_result(result, text, cache_key)


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

    total_budget = _get_env_float("AI_TIMEOUT", 60.0)
    if total_budget <= 0:
        total_budget = 60.0
    deadline = time.monotonic() + total_budget
    primary_cap = max(3.0, min(total_budget * 0.5, 12.0))
    openrouter_timeout = _resolve_timeout(deadline, "OPENROUTER_TIMEOUT", primary_cap)

    try:
        result = _call_grok(messages, timeout=openrouter_timeout)
    except Exception as exc:
        logger.error("GROK_CALL_FAILED: %s", exc, exc_info=True)
        deepseek = _run_deepseek_fallback(
            messages,
            text,
            cache_key,
            deadline,
            str(exc),
        )
        if deepseek is not None:
            return deepseek

        glm = _run_glm_fallback(
            messages,
            text,
            cache_key,
            deadline,
            str(exc),
        )
        if glm is not None:
            return glm

        oss = _run_oss_fallback(
            messages,
            text,
            cache_key,
            deadline,
            str(exc),
        )
        if oss is not None:
            return oss

        fallback = _run_openai_fallback(
            text,
            categories,
            locations,
            client_ai,
            cache_key,
            str(exc),
            deadline,
        )
        if fallback is not None:
            return fallback
        return {
            "relevant": False,
            "category": None,
            "subcategory": None,
            "region": None,
            "explanation": f"Grok error: {exc}",
            "confidence": 0.0,
            "accepted": False,
        }

    return _finalize_classification_result(result, text, cache_key)


def _run_openai_fallback(
    text: str,
    categories: list[str],
    locations: list[str],
    client_ai: Optional[Any],
    cache_key: str,
    grok_error: str,
    deadline: float,
) -> Optional[Dict[str, Any]]:
    logger.info("OPENAI_FALLBACK_ACTIVATED due to OpenRouter error: %s", grok_error)

    remaining = deadline - time.monotonic()
    if remaining <= 1.0:
        logger.warning("OPENAI_FALLBACK_SKIPPED: timeout budget exhausted")
        return None

    fallback_client = client_ai or get_openai_client()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                _classify_with_openai,
                text,
                categories,
                locations,
                fallback_client,
            )
            timeout = max(1.0, remaining - 0.5)
            classification = future.result(timeout=timeout)
    except FutureTimeout:
        logger.error(
            "OPENAI_FALLBACK_TIMEOUT: exceeded %.1fs budget after Grok/DeepSeek failure",
            remaining,
        )
        return None
    except Exception as fallback_exc:
        logger.error("OPENAI_FALLBACK_FAILED: %s", fallback_exc, exc_info=True)
        return None

    try:
        lower_text = text.lower()
        category = classification.get("category") if isinstance(classification, dict) else None
        classification = apply_overrides(classification, lower_text, category)
    except Exception as override_err:
        logger.warning("apply_overrides fallback failed: %s", override_err)

    _grok_cache_put(cache_key, classification)
    return classification


def _finalize_classification_result(
    result: Dict[str, Any],
    text: str,
    cache_key: str,
) -> Dict[str, Any]:
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


def _get_env_float(env_key: str, default: float) -> float:
    try:
        return float(os.getenv(env_key, str(default)))
    except (TypeError, ValueError):
        return default


def _resolve_timeout(deadline: float, env_key: str, default: float) -> float:
    configured = _get_env_float(env_key, default)
    remaining = max(0.0, deadline - time.monotonic())
    if remaining <= 0.5:
        return 0.5
    timeout = configured if configured > 0 else default
    timeout = min(timeout, default)  # don't exceed caller-specified ceiling
    timeout = min(timeout, remaining)
    return max(0.5, timeout)


__all__ = ["classify_text_with_ai", "_classify_cache", "apply_overrides", "update_categories"]
