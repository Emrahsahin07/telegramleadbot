import re
import json
import os
from datetime import datetime, timezone, timedelta
import time
import threading
import hashlib
from pathlib import Path
from dotenv import load_dotenv
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from config import logger
from constants import (
    BUYER_TRIGGERS,
    SELLER_TERMS,
    OFFER_TERMS,
    REALTY_HINT_TERMS,
    QUESTION_INDICATORS,
    SALESY_TERMS,
    PROMO_CTA_TERMS,
)
# Load environment variables
load_dotenv()
try:
    from openai import OpenAI, RateLimitError, APIError, Timeout
    from openai._types import NOT_GIVEN
except ImportError:
    import openai
    from openai import OpenAI  # клиент есть
    # shim: если в версии openai нет конкретных классов, подменяем Exception
    RateLimitError = getattr(openai, "RateLimitError", Exception)
    APIError = getattr(openai, "APIError", Exception)
    Timeout = getattr(openai, "Timeout", Exception)
    NOT_GIVEN = object()
DEBUG_PROMPT_TRACE = False


def _coerce_usage_dict(usage_obj):
    if usage_obj in (None, NOT_GIVEN):
        return None
    # If object has a direct usage attribute, inspect that first
    maybe_usage = getattr(usage_obj, 'usage', None) if not isinstance(usage_obj, dict) else None
    if maybe_usage is not None and maybe_usage is not usage_obj:
        converted = _coerce_usage_dict(maybe_usage)
        if converted is not None:
            return converted
    if isinstance(usage_obj, dict):
        return usage_obj
    if hasattr(usage_obj, 'to_dict'):
        try:
            data = usage_obj.to_dict()
            if isinstance(data, dict):
                # Responses API exposes usage nested under 'usage' key
                if 'usage' in data and isinstance(data['usage'], dict):
                    return data['usage']
                return data if 'input_tokens' in data else None
        except Exception:
            pass
    if hasattr(usage_obj, '__dict__'):
        try:
            data = dict(usage_obj.__dict__)
            if 'usage' in data and isinstance(data['usage'], dict):
                return data['usage']
            return data if 'input_tokens' in data else None
        except Exception:
            pass
    return None


def _log_usage(model_name: str, usage_obj, resp_id=None):
    usage_dict = _coerce_usage_dict(usage_obj)
    if usage_dict is None:
        return
    input_tokens = usage_dict.get('input_tokens')
    if input_tokens is None:
        input_tokens = usage_dict.get('prompt_tokens')
    output_tokens = usage_dict.get('output_tokens')
    if output_tokens is None:
        output_tokens = usage_dict.get('completion_tokens')
    total_tokens = usage_dict.get('total_tokens')
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cached_tokens = None
    input_details = usage_dict.get('input_tokens_details') or usage_dict.get('prompt_tokens_details') or {}
    if isinstance(input_details, dict):
        cached_tokens = input_details.get('cached_tokens')
    ts = datetime.now().strftime('%m-%d %H:%M:%S')
    try:
        with AI_TOKENS_LOG_PATH.open('a', encoding='utf-8') as tf:
            tf.write(f"{ts} | model={model_name} | in={input_tokens} cached_in={cached_tokens} out={output_tokens} total={total_tokens} resp_id={resp_id}\n")
    except Exception:
        pass
BASE_DIR = Path(__file__).resolve().parent
AI_TOKENS_LOG_PATH = BASE_DIR / "ai_tokens.log"
import snowballstemmer
_ru_stemmer = snowballstemmer.stemmer('russian')


def _stem(word: str) -> str:
    """Return lowercase snowball stem for Russian word."""
    return _ru_stemmer.stemWord(word.lower())
CATEGORY_ALIASES = {
    "страховка": "страховки",
    "страхование": "страховки",
}
try:
    _prompt_cats_path = BASE_DIR / "categories.json"
    with _prompt_cats_path.open(encoding="utf-8") as cf:
        _PROMPT_CATEGORIES = sorted(json.load(cf).keys())
except Exception:
    _PROMPT_CATEGORIES = ["аренда авто", "аренда яхт", "бьюти", "недвижимость", "страховки", "трансфер", "экскурсии"]
PROMPT_CATEGORY_CONTEXT = "Категории для классификации: " + ", ".join(_PROMPT_CATEGORIES)
CONF_THRESHOLD = 0.79  # confidence threshold for auto-accepting leads (deliver)
# Простой ручной кэш, потому что списки (list) не хешируемы для lru_cache
_classify_cache = {}
_CLASSIFY_CACHE_MAXSIZE = 5000
# TTL for cache entries (seconds), default 12 hours
_CLASSIFY_CACHE_TTL = int(float(os.getenv("AI_CACHE_TTL", str(12 * 3600))))

# --- Rate‑limit settings ---
_RATE_LIMIT_RPS = float(os.getenv("OPENAI_RPS", "3"))  # макс. запросов в секунду
_MIN_INTERVAL = 1.0 / _RATE_LIMIT_RPS
_last_call_ts = 0.0
_rate_lock = threading.Lock()
# Helper for rate-limit


def _apply_rate_limit():
    """Блокирует поток, чтобы выдержать заданный RPS."""
    global _last_call_ts
    with _rate_lock:
        now = time.time()
        wait_sec = _MIN_INTERVAL - (now - _last_call_ts)
        if wait_sec > 0:
            time.sleep(wait_sec)
        _last_call_ts = time.time()

# --- Robust JSON parsing for AI output --------------------------------------
import itertools


def _try_parse_ai_json(ai_text: str):
    """Best-effort parse of a JSON object from model output.
    Returns dict or None.
    """
    if not ai_text:
        return None
    # 1) Fast path
    try:
        obj = json.loads(ai_text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 2) Extract the first {...} block
    import re as _re
    m = _re.search(r"\{[\s\S]*\}", ai_text)
    if m:
        snippet = m.group(0)
        # quick fixes: single quotes -> double, trailing commas
        fixed = snippet.replace("'", '"')
        fixed = _re.sub(r",\s*([}\]])", r"\1", fixed)
        try:
            obj = json.loads(fixed)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
    return None
# Обёртка с retry для Responses API
@retry(
    wait=wait_exponential(min=1, max=30, multiplier=2),
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type(Exception)
)


def _responses_create_with_retry(client: OpenAI, **kwargs):
    """Вызов responses.create с rate‑limit и автоматическим back‑off."""
    _apply_rate_limit()
    return client.responses.create(**kwargs)
# Инициализация клиента OpenAI (лениво, если не передан)


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    return OpenAI(api_key=api_key)
# Вспомогательные детекторы
_contact_regex = re.compile(r"(@[\w_]+|t\.me/[\w_\-]+|https?://t\.me/[\w_\-]+|\+?[\d\-\s\(\)]{7,}|whatsapp)", flags=re.IGNORECASE)
_PRICE_REGEX = re.compile(
    r"\b\d+(?:[\.,]\d+)?\s?(?:€|eur|евро|usd|\$|доллар|₺|try)(?:\b|[/\-]?[а-яa-z]{0,8})",
    flags=re.IGNORECASE
)


def contains_contact(s: str) -> bool:
    return bool(_contact_regex.search(s))


def classify_relevance(text: str, categories: list[str], client_ai=None) -> dict:
    """Сохранена для совместимости: теперь не вызывается (слияние в один ИИ‑чек)."""
    return {"relevant": True, "explanation": "merged-into-main"}


def _sanitize_result(result: dict) -> dict:
    # Ensure keys exist with defaults
    relevant = result.get("relevant", False)
    category = result.get("category")
    subcategory = result.get("subcategory")
    region = result.get("region")
    explanation = result.get("explanation", "")
    confidence = result.get("confidence", 0.0)
    # Coerce confidence to float and clamp between 0 and 1
    try:
        confidence = float(confidence)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    # Truncate explanation to 70 chars with ellipsis if needed
    if len(explanation) > 70:
        explanation = explanation[:67] + "..."
    # Determine accepted and borderline flags
    accepted = relevant is True and confidence >= CONF_THRESHOLD
    borderline = False
    if relevant is True and confidence < CONF_THRESHOLD:
        borderline = True
    # Update result dict
    result["relevant"] = relevant
    result["category"] = category
    result["subcategory"] = subcategory
    result["region"] = region
    result["explanation"] = explanation
    result["confidence"] = confidence
    result["accepted"] = accepted
    if borderline:
        result["borderline"] = True
    elif "borderline" in result:
        del result["borderline"]
    return result


def calibrate_confidence(raw_confidence: float) -> float:
    """Piecewise calibration of confidence based on observed buckets."""
    if raw_confidence >= 0.9:
        calibrated = 0.92
    elif 0.8 <= raw_confidence < 0.9:
        calibrated = 0.85
    elif 0.6 <= raw_confidence < 0.8:
        calibrated = 0.7
    elif 0.5 <= raw_confidence < 0.6:
        calibrated = 0.55
    else:
        calibrated = raw_confidence
    return max(0.0, min(1.0, calibrated))


def classify_text_with_ai(text: str,
                          categories: list,
                          locations: list,
                          client_ai=None) -> dict:
    """
    Возвращает dict с keys: relevant, category, subcategory, region, explanation, confidence.
    Может добавлять override_reason.
    """
    if client_ai is None:
        client_ai = get_openai_client()
    # Ключ для кэша: хэш текста + хэш категорий
    text_hash = hashlib.sha1(text.encode('utf-8')).hexdigest()
    # Стандартизируем категории один раз, чтобы хэш и промпт были детерминированными
    raw_cats = list(categories) if categories else []
    cat_subset_sorted = sorted(dict.fromkeys(raw_cats))
    cats_hash = hashlib.sha1(str(cat_subset_sorted).encode('utf-8')).hexdigest()
    key = f"{text_hash}_{cats_hash}"
    if key in _classify_cache:
        entry = _classify_cache[key]
        cached_ts = entry.get('ts') if isinstance(entry, dict) else None
        cached_payload = entry.get('payload') if isinstance(entry, dict) else None
        if cached_ts is not None and (time.time() - cached_ts) <= _CLASSIFY_CACHE_TTL and isinstance(cached_payload, dict):
            return cached_payload.copy()
        else:
            # Expired entry
            try:
                del _classify_cache[key]
            except KeyError:
                pass
    # Сформировать списки для промпта
    focus_category_list = ', '.join(f'"{cat}"' for cat in cat_subset_sorted) if cat_subset_sorted else ''
    category_list = ', '.join(f'"{cat}"' for cat in _PROMPT_CATEGORIES) if _PROMPT_CATEGORIES else ''
    # УЛУЧШЕННЫЙ ПРОМПТ
    system_prompt = f"""Вы являетесь экспертом по классификации потенциальных клиентов (лидов) в Telegram-группах. Ваша задача - точно определить, является ли сообщение релевантным лидом, и правильно классифицировать его.
Категории для классификации перечислены в пользовательском сообщении ниже. Используй только их и не придумывай новые категории.
Обязательно ответьте ТОЛЬКО в формате JSON в одной строке без переносов:
{{"relevant": true/false, "category": "одна из категорий"|null, "subcategory": "подкатегория"|null, "region": "регион"|null, "explanation": "объяснение", "confidence": 0.0-1.0}}
Критерии релевантного лида:
1. Сообщение должно содержать прямой запрос на услугу из указанных категорий
2. Сообщение может содержать косвенные указания на потребность в услуге
3. Сообщение может быть вопросом или выражением потребности
4. Запрос цены или стоимости услуги также является релевантным лидом
5. Запрос информации о доступности услуги также является релевантным лидом
6. Фразы вроде "сниму", "ищу квартиру" — это запросы, даже если звучат как предложения
7. Категория "Трансфер" — только про перевозку людей, перевозка вещей нерелвантно.
Критерии НЕрелевантных сообщений (relevant: false):
1. Предложения услуг (не запросы) - если сообщение предлагает услугу, а не запрашивает её (например, "ищу попутчиков")
2. Отзывы и комментарии о прошлом опыте
3. Объявления о продаже/аренде (например, недвижимости, авто), если нет явного запроса
4. Спам и реклама
5. Сообщения без явной потребности в услуге
6. Фразы вида "аренда/прокат <услуга> от <цена>" (например, "аренда яхты от 200€") — это предложение услуги, не запрос.
7. Перечисление нескольких вариантов, CTA “напиши/забронируй”, слова “свободна”, “доступен”, “бронь”, “заезд”, отсутствие слов “ищу/нужен/сколько” — это предложение, а не лид!
Уровень уверенности (confidence):
- 0.9-1.0: Очень высокая уверенность - явный запрос
- 0.7-0.9: Высокая уверенность - вероятный запрос
- 0.5-0.7: Средняя уверенность - возможный запрос
- 0.0-0.5: Низкая уверенность - маловероятный запрос
Объяснение должно быть кратким (до 70 символов), указывая на ключевые слова или фразы, на основе которых было принято решение.
Примеры:
1. "Мы вчера ехали из Кемера на трансфере 4 человека, багаж 40$" → {{"relevant": false, "category": "трансфер", "subcategory": null, "region": "Кемер", "explanation": "прошлый опыт", "confidence": 0.95}}
2. "Продаю квартиру в Стамбуле, 2+1, евроремонт" → {{"relevant": false, "category": null, "subcategory": null, "region": null, "explanation": "предложение продажи недвижимости", "confidence": 0.98}}
3. "Кто-нибудь пользовался услугами клининга? Посоветуйте" → {{"relevant": true, "category": "клининг", "subcategory": null, "region": null, "explanation": "запрос рекомендации клининга", "confidence": 0.85}}
4. "Сколько стоит трансфер из аэропорта до отеля в Сиде?" → {{"relevant": true, "category": "трансфер", "subcategory": null, "region": "Сиде", "explanation": "запрос цены на трансфер", "confidence": 0.90}}
5. "Какова стоимость экскурсии по Стамбулу?" → {{"relevant": true, "category": "экскурсии", "subcategory": null, "region": "Стамбул", "explanation": "запрос цены на экскурсию", "confidence": 0.90}}
6. "Где можно арендовать яхту в Кемере?" → {{"relevant": true, "category": "аренда яхт", "subcategory": null, "region": "Кемер", "explanation": "запрос аренды яхты", "confidence": 0.90}}
7. "Нужен массаж в Анталии, сколько это будет стоить?" → {{"relevant": true, "category": "бьюти", "subcategory": "маникюр", "region": "Анталия", "explanation": "запрос цены на массаж", "confidence": 0.90}}
8. "Сниму квартиру 2+1 в Коньяалты" → {{"relevant": true, "category": "недвижимость", "subcategory": "аренда", "region": "Коньяалты", "explanation": "запрос аренды квартиры", "confidence": 0.95}}
9. "Ищу квартиру в Анталии, 2+1" → {{"relevant": true, "category": "недвижимость", "subcategory": "аренда", "region": "Анталия", "explanation": "запрос аренды квартиры", "confidence": 0.95}}
10. "Сдам квартиру в Сиде, евроремонт" → {{"relevant": false, "category": "недвижимость", "subcategory": "аренда", "region": "Сиде", "explanation": "предложение сдачи недвижимости", "confidence": 0.95}}
11. "Нужен электрик в Анталии, срочно" → {{"relevant": true, "category": "услуги_мастеров", "subcategory": "электрика", "region": "Анталия", "explanation": "запрос электрика", "confidence": 0.95}}
12. "Ищу сантехника в Сиде, опыт, гарантия" → {{"relevant": true, "category": "услуги_мастеров", "subcategory": "сантехника", "region": "Сиде", "explanation": "запрос сантехника", "confidence": 0.95}}
13. “🌊 Demre – Myra Tour 🌿 … Стоимость тура: 100€… Это идеальный тур!” → {{“relevant”: false, “category”: “экскурсии”, “subcategory”: null, “region”: “Демре”, “explanation”: “явное предложение тура”, “confidence”: 0.95}}"""
    category_context = PROMPT_CATEGORY_CONTEXT
    focus_context = (
        f"Фокусные категории для этого запроса: {focus_category_list}"
        if focus_category_list else ""
    )
    prompt_id = os.getenv("OPENAI_PROMPT_ID") or os.getenv("OPENAI_PROMPT_TEMPLATE_ID")
    prompt_version = os.getenv("OPENAI_PROMPT_VERSION")
    message_only = text
    user_prompt_parts = [category_context]
    if focus_context:
        user_prompt_parts.append(focus_context)
    user_prompt_parts.append(message_only)
    user_prompt = "\n\n".join(part for part in user_prompt_parts if part)
    prompt_payload = NOT_GIVEN
    instructions_arg = NOT_GIVEN
    prompt_variables = None
    prompt_cache_source = None
    raw_prompt_system = None
    if prompt_id:
        prompt_payload = {"id": prompt_id}
        if prompt_version:
            prompt_payload["version"] = prompt_version
        prompt_variables = {
            "text": message_only,
            "message": message_only,
            "user_prompt": user_prompt,
            "category_context": category_context,
            "category_list": category_list,
            "focus_category_list": focus_category_list,
            "focus_context": focus_context,
        }
        prompt_variables = {k: v for k, v in prompt_variables.items() if v}
        if prompt_variables:
            prompt_payload["variables"] = prompt_variables
        raw_prompt_system = f"prompt_id:{prompt_id}{'@'+prompt_version if prompt_version else ''}"
    else:
        instructions_arg = system_prompt
        raw_prompt_system = system_prompt
    input_messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": user_prompt}
            ],
        }
    ]
    if prompt_id:
        cache_components = [f"prompt:{prompt_id}:{prompt_version or 'latest'}"]
        if prompt_variables:
            stable_cache_vars = {
                key: prompt_variables.get(key)
                for key in ("category_context", "category_list")
                if prompt_variables.get(key)
            }
            if stable_cache_vars:
                cache_components.append(json.dumps(stable_cache_vars, ensure_ascii=False, sort_keys=True))
        prompt_cache_source = "|".join(cache_components)
    else:
        prompt_cache_source = system_prompt
    if not prompt_cache_source:
        prompt_cache_source = user_prompt
    # Store raw prompt for debug/tracing
    raw_prompt = {
        "system": raw_prompt_system,
        "user": user_prompt,
    }
    if prompt_variables:
        raw_prompt["prompt_variables"] = prompt_variables
    # Responses API: strict JSON schema via text.format
    model_name = os.getenv("OPENAI_MODEL", "gpt-5-nano")
    reasoning_effort = os.getenv("REASONING_EFFORT", "minimal")
    text_block = {
        "format": {
            "type": "json_schema",
            "name": "lead_classification",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "relevant": {"type": "boolean"},
                    "category": {"type": ["string", "null"]},
                    "subcategory": {"type": ["string", "null"]},
                    "region": {"type": ["string", "null"]},
                    "explanation": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
                },
                "required": ["relevant", "category", "subcategory", "region", "explanation", "confidence"],
                "additionalProperties": False
            }
        }
    }
    verbosity = os.getenv("AI_VERBOSITY")
    if verbosity in ("low", "medium", "high"):
        text_block["verbosity"] = verbosity
    # Prompt caching key (stable for identical instructions setup)
    prompt_cache_key = hashlib.sha1(str(prompt_cache_source).encode("utf-8")).hexdigest()
    cache_enabled = os.getenv("ENABLE_PROMPT_CACHE", "1") == "1"
    cache_kwargs = {}
    if cache_enabled:
        cache_kwargs = {
            "prompt_cache_key": f"leadbot:{prompt_cache_key}",
        }
    response_kwargs = {
        "model": model_name,
        "input": input_messages,
        "reasoning": {"effort": reasoning_effort},
        "text": text_block,
        "store": True,
        "metadata": {"app": "leadbot", "component": "classifier"},
        **cache_kwargs,
    }
    if prompt_payload is not NOT_GIVEN:
        response_kwargs["prompt"] = prompt_payload
    if instructions_arg is not NOT_GIVEN:
        response_kwargs["instructions"] = instructions_arg
    try:
        resp = _responses_create_with_retry(
            client_ai,
            **response_kwargs,
        )
    except Exception as e:
        logger.warning("RESPONSES_CALL_FAILED: %s", e, exc_info=True)
        # Fallback 1: Chat Completions with response_format json_object
        try:
            _apply_rate_limit()
            fallback_user = user_prompt
            cc = client_ai.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": fallback_user},
                ],
                response_format={"type": "json_object"},
            )
            content = None
            try:
                content = cc.choices[0].message.content if getattr(cc.choices[0].message, 'content', None) else cc.choices[0].message["content"]
            except Exception:
                # try alternative attribute structure
                content = str(cc)
            parsed = _try_parse_ai_json(content or "")
            if not parsed:
                raise RuntimeError("ChatCompletions returned non-JSON content")
            _log_usage(model_name, cc, getattr(cc, 'id', None))
            result = _sanitize_result(parsed)
            raw_conf = result.get("confidence", 0.0)
            result["confidence"] = calibrate_confidence(raw_conf)
            result = _sanitize_result(result)
            # Cache and return
            if len(_classify_cache) >= _CLASSIFY_CACHE_MAXSIZE:
                _classify_cache.pop(next(iter(_classify_cache)))
            _classify_cache[key] = {"ts": time.time(), "payload": result.copy()}
            return result
        except Exception as e2:
            # Fallback 2: plain parsing failure
            return {
                "relevant": False,
                "category": None,
                "subcategory": None,
                "region": None,
                "explanation": f"OpenAI error: {e} | fallback: {e2}",
                "confidence": 0.0,
                "accepted": False,
            }
    # Token usage dedicated log
    _log_usage(model_name, resp, getattr(resp, 'id', None))
    # Prefer consolidated output_text helper, fallback to joining output items
    content = getattr(resp, 'output_text', None) or ""
    if not content:
        parts = []
        out = getattr(resp, 'output', None) or []
        try:
            for item in out:
                cont = getattr(item, 'content', None)
                if isinstance(cont, list):
                    for c in cont:
                        txt = getattr(c, 'text', None) if hasattr(c, 'text') else (c.get('text') if isinstance(c, dict) else None)
                        if txt:
                            parts.append(str(txt))
        except Exception:
            pass
        content = "\n".join(parts).strip()
    raw_model_output = content
    result = _try_parse_ai_json(content)
    if result is None:
        # Fallback: return safe default with raw included for trace
        return {
            "relevant": False,
            "category": None,
            "subcategory": None,
            "region": None,
            "explanation": "Ошибка парсинга ответа ИИ",
            "confidence": 0.0,
            "raw": content,
            "accepted": False,
            "raw_prompt": raw_prompt,
            "raw_model_output": raw_model_output,
        }
    # Sanitize, calibrate, and sanitize again (single clear flow)
    result = _sanitize_result(result)
    raw_conf = result.get("confidence", 0.0)
    result["confidence"] = calibrate_confidence(raw_conf)
    result = _sanitize_result(result)
    cat_name = result.get("category")
    if cat_name:
        normalized = CATEGORY_ALIASES.get(str(cat_name).lower())
        if normalized:
            result["category"] = normalized
    # Attach prompt and model output for trace/debug only if DEBUG_PROMPT_TRACE
    if DEBUG_PROMPT_TRACE:
        result["raw_prompt"] = raw_prompt
        result["raw_model_output"] = raw_model_output
    # Кешируем результат (копию) и ограничиваем размер
    if len(_classify_cache) >= _CLASSIFY_CACHE_MAXSIZE:
        _classify_cache.pop(next(iter(_classify_cache)))
    _classify_cache[key] = {"ts": time.time(), "payload": result.copy()}
    return result

# --- apply_overrides and helpers moved from Botparsing.py ---


def apply_overrides(cla, lower_text, category_heuristic):
    """
    Post-hoc override logic for classification adjustment (heuristics only).
    """
    lower = lower_text
    # Явное разрешение для запросов аренды
    rental_signals = [
        "сниму", "снять",
        "ищу квартиру", "ищем квартиру", "ищет квартиру",
        "короткий срок", "на месяц", "на 1 месяц", "на один месяц"
    ]
    if any(w in lower for w in rental_signals) or ("квартир" in lower and any(w in lower for w in ["ищу", "ищем", "ищет"])):
        cla["relevant"] = True
        cla["category"] = cla.get("category") or "недвижимость"
        cla["explanation"] = "Запрос аренды недвижимости"
        return cla
    # Доменное правило: массаж → бьюти
    if any(w in lower for w in ("массаж", "массажист", "массажистка")):
        cla["category"] = "бьюти"
    # Реклама страховки: salesy без вопроса
    if "страховка" in lower:
        salesy = any(w in lower for w in SALESY_TERMS)
        if salesy and not any(q in lower for q in QUESTION_INDICATORS):
            cla["relevant"] = False
            cla["explanation"] = "Реклама страховки"
            return cla  # Прекращаем обработку, если точно реклама
    # 2.1) Специальная отсечка листингов недвижимости после ИИ
    # Если ИИ (или эвристика) выбрал «недвижимость», но текст похож на объявление продавца — помечаем как нерелевантное
    if (cla.get("category") == "недвижимость" or category_heuristic == "недвижимость"):
        has_seller = any(t in lower for t in SELLER_TERMS)
        price_matches = _PRICE_REGEX.findall(lower)
        has_price = bool(price_matches)
        has_layout = bool(re.search(r"\b[1-5]\s*([+xх])\s*[0-5]\b", lower))
        has_area = bool(re.search(r"\b(кв\.?\s?м|м2|м\^2|м²|sqm|sq\s?m)\b", lower))
        has_realty_hint = any(tok in lower for tok in REALTY_HINT_TERMS)
        has_buyer = any(bt in lower for bt in BUYER_TRIGGERS)
        sellerish = has_seller or has_price or has_layout or has_area or has_realty_hint
        # если продавец и нет явного покупательского запроса — не лид
        emoji_sections = lower.count("🌟") + lower.count("🌴") + lower.count("✨")
        promo_cta_hits = sum(lower.count(term) for term in PROMO_CTA_TERMS)
        if sellerish and not has_buyer:
            cla["relevant"] = False
            cla["explanation"] = "Риэлторский листинг/продажа"
            return cla  # Прекращаем обработку, если точно реклама
        if (len(price_matches) >= 2 or emoji_sections >= 2 or promo_cta_hits >= 2) and not has_buyer:
            cla["relevant"] = False
            cla["explanation"] = "Риэлторский листинг/продажа"
            return cla
    # Фильтрация предложений услуг (не запросов)
    has_offer = any(term in lower for term in OFFER_TERMS)
    has_buyer_request = any(trigger in lower for trigger in BUYER_TRIGGERS)
    if has_offer and not has_buyer_request:
        cla["relevant"] = False
        cla["explanation"] = "Предложение услуг, а не запрос"
        return cla  # Прекращаем обработку, если точно предложение
    # Фильтрация отзывов
    review_terms = [
        "отлично", "хорошо", "плохо", "ужасно", "не рекомендую", "рекомендую", "советую", 
        "не советую", "опыт", "работал", "работала", "пользовался", "пользовалась"
    ]
    has_review = any(term in lower for term in review_terms)
    
    # Дополнительная проверка для запросов рекомендаций
    recommendation_request_terms = ["посоветуй", "посоветуйте", "кто знает", "кто может", "кто занимается"]
    is_recommendation_request = any(term in lower for term in recommendation_request_terms)
    
    if has_review and not has_buyer_request and not is_recommendation_request:
        cla["relevant"] = False
        cla["explanation"] = "Отзыв или комментарий, а не запрос"
        return cla  # Прекращаем обработку, если точно отзыв
    # Эвристика категории НЕ должна жёстко перебивать ИИ:
    # перезаписываем только если у ИИ нет категории или уверенность низкая
    if category_heuristic:
        ai_cat = cla.get("category")
        try:
            ai_conf = float(cla.get("confidence", 0.0) or 0.0)
        except Exception:
            ai_conf = 0.0
        if not ai_cat or ai_conf < 0.6:
            cla["category"] = category_heuristic
    # Мягкая калибровка confidence при совпадении с эвристикой
    conf = cla.get("confidence", 0.0)
    if category_heuristic and cla.get("category") == category_heuristic and conf < 0.8:
        cla["confidence"] = min(0.85, conf + 0.1)
    return cla


def _stem_in_text(needle: str, stems_set: set[str]) -> bool:
    """True if stem(needle) присутствует в тексте (по стемам)."""
    return _stem(needle) in stems_set

# --- Exports ---------------------------------------------------------------
__all__ = [
    "classify_text_with_ai",
    "apply_overrides",
    "_classify_cache",
    "classify_relevance",
]# ai_utils.py (в конец файла)


def update_categories():
    """Обновляет категории в памяти из файла categories.json"""
    global categories
    try:
        with open("categories.json", "r", encoding="utf-8") as f:
            categories = json.load(f)
        print("✅ Категории обновлены из categories.json")
    except Exception as e:
        print(f"❌ Ошибка обновления категорий: {e}")

# --- Backward-compat cache helpers for tests/tools ---


def _norm_text(s: str) -> str:
    """Normalize text for cache key creation (lowercase + strip)."""
    return (s or "").strip().lower()


def _cache_put(key: str, value: dict):
    """Put value into classification cache with current timestamp."""
    if len(_classify_cache) >= _CLASSIFY_CACHE_MAXSIZE:
        _classify_cache.pop(next(iter(_classify_cache)))
    _classify_cache[key] = {"ts": time.time(), "payload": value.copy() if isinstance(value, dict) else value}


def _cache_get(key: str):
    """Get cached value if present and not expired; else None."""
    entry = _classify_cache.get(key)
    if not isinstance(entry, dict):
        return None
    ts = entry.get("ts")
    payload = entry.get("payload")
    if ts is None or (time.time() - ts) > _CLASSIFY_CACHE_TTL:
        try:
            del _classify_cache[key]
        except KeyError:
            pass
        return None
    return payload
