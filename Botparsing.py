import os
import socket
try:
    import requests  # optional; network failures are non-fatal
except Exception:
    requests = None

def get_ip(timeout: int = 3) -> str:
    env_ip = os.getenv("BOT_IP") or os.getenv("MY_IP")
    if env_ip:
        return env_ip.strip()
    if requests is not None:
        try:
            r = requests.get("https://api.ipify.org", timeout=timeout)
            if r.ok and r.text:
                return r.text.strip()
        except Exception:
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no traffic actually sent
            return s.getsockname()[0]
    except Exception:
        pass
    try:
        host = socket.gethostname()
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
        for _, _, _, _, sockaddr in infos:
            tip = sockaddr[0]
            if tip and not tip.startswith("127."):
                return tip
    except Exception:
        pass
    return "127.0.0.1"

ip = get_ip()
print(f"[BOOT] Running on host IP: {ip}")
VERBOSE_DEBUG = os.getenv("BOT_DEBUG") == "1"

from datetime import datetime, timedelta, timezone
import time
# helper for Istanbul time
def now_istanbul():
    return datetime.now(timezone.utc) + timedelta(hours=3)

import keep_alive  # стартует Flask-сервер для keep-alive
from ai_utils2 import classify_text_with_ai, _classify_cache, apply_overrides, update_categories
from connection_manager import add_telegram_client, connect_all_clients, start_connection_monitoring, disconnect_all_clients

import openai
from openai import OpenAI
import re
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from collections import deque
from filters import (
    extract_stems, is_similar, contains_negative, _contains_word, is_advertisement,
    infer_region_from_text, extract_transfer_route, _all_locations_from_text,
    contains_contact,
)
from delivery import send_lead_to_users, WORD_RE, _stem

from constants import BUYER_TRIGGERS, OFFER_TERMS
from config import ADMIN_ID, categories, metrics, logger, bot_client, subscriptions, save_subscriptions
import message_queue

# Initialize global variables at module level to prevent NameError
SELF_ID = None
SELF_USERNAME = None

try:
    _dedup_env = float(os.getenv("DEDUP_WINDOW_SECONDS", "600"))
except (TypeError, ValueError):
    _dedup_env = 600.0
DEDUP_WINDOW_SECONDS = int(_dedup_env) if _dedup_env > 0 else 0
try:
    MAX_DEDUP_CACHE = int(os.getenv("DEDUP_CACHE_LIMIT", "20000"))
except (TypeError, ValueError):
    MAX_DEDUP_CACHE = 20000
_recent_text_cache = {}
_recent_text_queue = deque()

# Очередь сообщений
import asyncio
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "2"))

import json
import logging
import atexit

# --- Unified logging helper ---
def log_evt(code, *, chat_id=None, group_name=None, region=None, cat=None, conf=None, kw=None, msg=None, extra=None):
    if code == 'DROP_AD':
        return

    parts = [code]
    if chat_id is not None:
        if group_name:
            parts.append(f"chat={chat_id} ({group_name})")
        else:
            parts.append(f"chat={chat_id}")
    if region:
        parts.append(f"region={region}")
    if cat:
        parts.append(f"cat={cat}")
    if conf is not None:
        try:
            parts.append(f"conf={float(conf):.2f}")
        except Exception:
            parts.append(f"conf={conf}")
    if kw:
        parts.append(f"kw={kw}")
    if msg:
        parts.append(f"msg='{msg}'")
    if extra:
        parts.append(str(extra))
    return " | ".join(parts)


def log_info_event(code, **kwargs):
    line = log_evt(code, **kwargs)
    if line:
        logger.info(line)


def _normalize_for_dedup(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _should_drop_duplicate(normalized_text: str) -> bool:
    if DEDUP_WINDOW_SECONDS <= 0 or not normalized_text:
        return False

    now = time.time()
    cutoff = now - DEDUP_WINDOW_SECONDS

    # purge expired entries
    while _recent_text_queue and _recent_text_queue[0][0] <= cutoff:
        ts, text_key = _recent_text_queue.popleft()
        current = _recent_text_cache.get(text_key)
        if current is not None and current <= cutoff and current == ts:
            _recent_text_cache.pop(text_key, None)

    last_seen = _recent_text_cache.get(normalized_text)
    is_duplicate = last_seen is not None and last_seen >= cutoff

    _recent_text_cache[normalized_text] = now
    _recent_text_queue.append((now, normalized_text))

    if MAX_DEDUP_CACHE > 0 and len(_recent_text_queue) > MAX_DEDUP_CACHE:
        # trim oldest entries beyond cache limit
        overflow = len(_recent_text_queue) - MAX_DEDUP_CACHE
        for _ in range(overflow):
            ts, text_key = _recent_text_queue.popleft()
            current = _recent_text_cache.get(text_key)
            if current is not None and current == ts:
                _recent_text_cache.pop(text_key, None)

    return is_duplicate

# Persist metrics to JSON on shutdown
def dump_metrics():
    with open("metrics.json", "w", encoding="utf-8") as mf:
        json.dump(metrics, mf, ensure_ascii=False, indent=2)

atexit.register(dump_metrics)


async def metrics_dump_task():
    while True:
        await asyncio.sleep(3600)  # раз в час
        dump_metrics()


# Load environment variables
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
if not openai.api_key:
    raise RuntimeError("OPENAI_API_KEY is not set in .env")

api_id_str = os.getenv("API_ID")
if not api_id_str or not api_id_str.isdigit():
    raise RuntimeError("API_ID is not set or not an integer in .env")
api_id = int(api_id_str)

api_hash = os.getenv("API_HASH")
if not api_hash:
    raise RuntimeError("API_HASH is not set in .env")

bot_token = os.getenv("LEADBOT_TOKEN", os.getenv("BOT_TOKEN"))
if not bot_token:
    raise RuntimeError("LEADBOT_TOKEN or BOT_TOKEN is not set in .env")

# Telegram user ID to receive manual payment proofs (replace with your ID)
# ADMIN_ID moved to config.py

# Initialize OpenAI client after API key is loaded (bounded timeouts)
from ai_utils2 import get_openai_client
client_ai = get_openai_client()

from config import LOCATION_ALIAS
# List of canonical display names

CANONICAL_LOCATIONS = sorted(set(LOCATION_ALIAS.values()))

# Simple bot command to verify liveness in dev
@bot_client.on(events.NewMessage(pattern=r"/ping"))
async def _ping(event):
    try:
        await event.reply("pong")
    except Exception:
        pass

# --- Region inference utilities & cache ---
REGION_CACHE = {}  # chat_id -> canonical region name




# Импортируем функцию отправки лидов из delivery.py
# Confidence thresholds (unified)
# - conf >= 0.79 → deliver to users
# - 0.70 <= conf < 0.79 → admin review
# - conf < 0.70 → discard
CONF_THRESHOLD = 0.79
DISCARD_THRESHOLD = 0.70

# Session file name can be overridden:  TG_SESSION=custom python3 Botparsing.py
session_name = os.getenv("TG_SESSION", "bot_parser")
client = TelegramClient(
    session_name, 
    api_id, 
    api_hash, 
    connection_retries=10,  # Increased retries
    retry_delay=2,  # Delay between retries
    timeout=30,  # Connection timeout
    request_retries=5,  # Request retries
    flood_sleep_threshold=60  # Auto-sleep on flood wait
)

# Bot client for UI and commands
# bot_client moved to config.py
import ui
import admin_feedback  # Load admin feedback commands

async def initialize_feedback_system():
    """Инициализация системы обратной связи при запуске"""
    try:
        from feedback_manager import feedback_manager
        from review_handler import migrate_feedback_log_to_db
        
        # Инициализируем базу данных
        await feedback_manager.init_db()
        logger.info("✅ Feedback database initialized")
        
        # Автоматически мигрируем существующие данные из feedback.log
        migrated_count = await migrate_feedback_log_to_db()
        if migrated_count > 0:
            logger.info(f"📚 Auto-migrated {migrated_count} admin decisions from feedback.log")
        
    except Exception as e:
        logger.error(f"❌ Error initializing feedback system: {e}")

# Precompute top-level keyword stems once
TOP_KEYWORD_STEMS = set()
for cat_entry in categories.values():
    for kw in cat_entry.get("keywords", []):
        for tok in WORD_RE.findall(str(kw).lower()):
            TOP_KEYWORD_STEMS.add(_stem(tok))

ALLOWED_CHATS = None
try:
    _allowed = os.getenv("ALLOWED_CHAT_IDS")
    if _allowed:
        ALLOWED_CHATS = {int(x.strip()) for x in _allowed.split(',') if x.strip()}
except Exception:
    ALLOWED_CHATS = None

@client.on(events.NewMessage())
async def handler(event):
    # Only enqueue messages from groups/channels; ignore private/bot chats
    if not (event.is_group or event.is_channel):
        return
    if ALLOWED_CHATS is not None and event.chat_id not in ALLOWED_CHATS:
        logger.debug(f"SKIP(parser): chat_id={event.chat_id} not in ALLOWED_CHATS")
        return
    # Сохраняем событие в SQLite
    chat = await event.get_chat()
    sender = await event.get_sender()

    fwd = getattr(event, 'fwd_from', None)
    fwd_from_name = getattr(fwd, 'from_name', None) if fwd else None
    fwd_from_id = None
    try:
        from_id_obj = getattr(fwd, 'from_id', None)
        fwd_from_id = getattr(from_id_obj, 'user_id', None) or getattr(from_id_obj, 'channel_id', None)
    except Exception:
        fwd_from_id = None

    event_dict = {
        "id": event.id,
        "chat_id": event.chat_id,
        "is_group": bool(getattr(event, 'is_group', False)),
        "is_channel": bool(getattr(event, 'is_channel', False)),
        "chat_title": getattr(chat, 'title', None),
        "chat_username": getattr(chat, 'username', None),
        "sender_id": getattr(event, 'sender_id', None),
        "sender_name": getattr(sender, 'first_name', None) if sender else None,
        "sender_username": getattr(sender, 'username', None) if sender else None,
        "text": event.raw_text,
        "date": event.date.isoformat() if event.date else None,
        "is_forwarded": bool(fwd),
        "fwd_from_name": fwd_from_name,
        "fwd_from_id": fwd_from_id,
    }
    enq_ok = await message_queue.enqueue(event_dict)
    logger.debug(f"ENQ(parser): chat_id={event.chat_id} id={event.id} ok={enq_ok}")

# Also listen on the bot client in dev or where bot privacy allows
@bot_client.on(events.NewMessage())
async def handler_bot(event):
    # Disabled by default to avoid duplicate enqueues; enable via ENABLE_BOT_LISTENER=1
    if os.getenv("ENABLE_BOT_LISTENER", "0") != "1":
        return
    try:
        if ALLOWED_CHATS is not None and event.chat_id not in ALLOWED_CHATS:
            logger.debug(f"SKIP(bot): chat_id={event.chat_id} not in ALLOWED_CHATS")
            return
        chat = await event.get_chat()
        sender = await event.get_sender()
        event_dict = {
            "id": getattr(event, 'id', None) or int(datetime.now().timestamp()*1000),
            "chat_id": event.chat_id,
            "chat_title": getattr(chat, 'title', None),
            "chat_username": getattr(chat, 'username', None),
            "sender_id": getattr(event, 'sender_id', None),
            "sender_name": getattr(sender, 'first_name', None) if sender else None,
            "sender_username": getattr(sender, 'username', None) if sender else None,
            "text": event.raw_text,
            "date": event.date.isoformat() if getattr(event, 'date', None) else datetime.now().isoformat(),
            "is_forwarded": bool(getattr(event, 'fwd_from', None)),
            "fwd_from_name": getattr(getattr(event, 'fwd_from', None), 'from_name', None),
            "fwd_from_id": None,
        }
        enq_ok = await message_queue.enqueue(event_dict)
        logger.debug(f"ENQ(bot): chat_id={event.chat_id} id={event_dict['id']} ok={enq_ok}")
    except Exception as e:
        logger.error(f"Bot handler enqueue error: {e}")

async def process_message(event):
    metrics['received'] += 1
    # Only groups and channels
    if not (event.is_group or event.is_channel):
        return
    chat_id = event.chat_id
    # Ignore own messages
    global SELF_ID
    if event.sender_id and event.sender_id == SELF_ID:
        return
    # Prevent forward-loop: ignore messages forwarded from our own bot/user
    is_forwarded = False
    fwd_from_name = None
    fwd_from_id = None
    if hasattr(event, '_data') and isinstance(event._data, dict):
        is_forwarded = bool(event._data.get("is_forwarded"))
        fwd_from_name = event._data.get("fwd_from_name")
        fwd_from_id = event._data.get("fwd_from_id")
    else:
        fwd = getattr(event, 'fwd_from', None)
        is_forwarded = bool(fwd)
        fwd_from_name = getattr(fwd, 'from_name', None) if fwd else None
        try:
            from_id_obj = getattr(fwd, 'from_id', None)
            fwd_from_id = getattr(from_id_obj, 'user_id', None) or getattr(from_id_obj, 'channel_id', None)
        except Exception:
            fwd_from_id = None

    global SELF_USERNAME
    if is_forwarded and (
        (SELF_ID is not None and fwd_from_id == SELF_ID) or
        (SELF_USERNAME and fwd_from_name and SELF_USERNAME.lower() in str(fwd_from_name).lower())
    ):
        metrics['forward_loop_blocked'] += 1
        return
    text = event.raw_text or ""
    # Remove hashtags to avoid false matches
    clean_text = re.sub(r'#\w+', '', text)
    lower_text = clean_text.lower()

    # Guard against processing our own outbound notifications (avoid loops/duplicates)
    if clean_text.lstrip().startswith("📩") or clean_text.lstrip().startswith("⚠️ Ошибка"):
        metrics['forward_loop_blocked'] += 1
        logger.debug(log_evt("DROP_SELFMSG", chat_id=chat_id, msg=clean_text[:120]))
        return
    if contains_negative(lower_text):
        metrics['negative_ctx_filtered'] += 1
        logger.debug(log_evt("DROP_NEGCTX", chat_id=chat_id, msg=lower_text[:180]))
        return

    # Deduplicate identical messages within configured window
    normalized_for_dedup = _normalize_for_dedup(clean_text)
    if _should_drop_duplicate(normalized_for_dedup):
        metrics['dedup_text'] += 1
        log_info_event("DROP_DUP", chat_id=chat_id, msg=clean_text[:120])
        return
    # Pre-filter real estate ads to save AI calls
    if is_advertisement(text):
        metrics['pre_ad_filtered'] += 1
        log_info_event("DROP_AD", chat_id=chat_id, msg=(text or "")[:180])
        return
    # Lowercase text for matching
    # Pre‑compute set of stems in the message for fast membership test
    stems_in_text = {_stem(tok) for tok in WORD_RE.findall(lower_text)}

    # Global pre-filter via stems (soft)
    top_keyword_stems = TOP_KEYWORD_STEMS
    has_top_kw = bool(stems_in_text.intersection(top_keyword_stems))


    dialog = await event.get_chat()
    group_name = (
        getattr(dialog, 'title', None)
        or getattr(dialog, 'username', None)
        or f"chat_{chat_id}"
    )

    sender_entity = await event.get_sender()
    if sender_entity:
        sender_id = sender_entity.id
        sender_name = getattr(sender_entity, 'first_name', None) or getattr(sender_entity, 'username', 'Неизвестный отправитель')
        sender_username = getattr(sender_entity, 'username', None)
    else:
        # Fallback for channels without a user sender
        sender_id = getattr(event, 'sender_id', None) or event.chat_id
        sender_name = group_name
        sender_username = None

    # Extra guard: drop if the sender is our own bot/user
    if SELF_USERNAME and sender_username and sender_username.lower() == SELF_USERNAME.lower():
        metrics['forward_loop_blocked'] += 1
        logger.debug(log_evt("DROP_SELFUSR", chat_id=chat_id, msg=clean_text[:120]))
        return

    # Optionally ignore messages posted by bot accounts in groups (to avoid bot spam)
    if os.getenv("IGNORE_BOT_SENDERS", "1") == "1":
        try:
            if getattr(sender_entity, 'bot', False):
                logger.debug(log_evt("DROP_BOT", chat_id=chat_id, msg=clean_text[:120]))
                return
        except Exception:
            pass



    # Region detection with cache and heuristics (do not drop yet)
    group_username = getattr(dialog, 'username', None)

    # --- Region detection (cache + strict title alias match, no fuzzy) ---
    region = REGION_CACHE.get(chat_id)
    title_lower = (group_name or "").lower()
    group_username = getattr(dialog, 'username', None)

    # Try cache; if absent, infer strictly (no fuzzy)
    if not region:
        region = infer_region_from_text(
            group_name if group_name is not None else "",
            group_username if group_username is not None else "",
            lower_text
        )
        if region:
            REGION_CACHE[chat_id] = region

    # If cached region conflicts with strict title match, correct the cache
    # We only trust exact/boundary alias matches in the chat title
    strict_title_region = None
    for alias, canon in LOCATION_ALIAS.items():
        if _contains_word(title_lower, alias):
            strict_title_region = canon
            break
    if strict_title_region and region != strict_title_region:
        region = strict_title_region
        REGION_CACHE[chat_id] = region

    if not region:
        metrics['no_region'] += 1
        logger.debug(log_evt("DROP_NOREG", chat_id=chat_id, group_name=group_name, msg=lower_text[:180]))
        return
    metrics['region_detected'] += 1

    # Heuristic category detection: match any category stem in text (support nested)
    category_heuristic = None
    for cat, stems in categories.items():
        for stem in extract_stems(stems):
            if _stem(stem.lower()) in stems_in_text:
                category_heuristic = cat
                break
        if category_heuristic:
            break
    if category_heuristic:
        metrics['category_heuristic_detected'] += 1
    else:
        metrics['category_not_detected'] += 1
        logger.debug("Drop: no category heuristic detected")
    if region and category_heuristic:
        metrics['coverage_ok'] += 1

    # User-specific pre-filter: consider candidate regions from chat and explicit locations in text
    candidate_regions = set()
    if region:
        candidate_regions.add(region)
    for loc in _all_locations_from_text(lower_text):
        candidate_regions.add(loc)

    subscribers_for_region = [
        prefs for prefs in subscriptions.values()
        if any(r in prefs.get("locations", []) for r in candidate_regions)
    ]

    # Tighten only if we actually have subscribers for the candidate regions
    if subscribers_for_region:
        regional_keyword_stems = {
            _stem(kw.lower())
            for prefs in subscribers_for_region
            for cat in prefs.get("categories", [])
            for kw in categories.get(cat, {}).get("keywords", [])
        }
        if not stems_in_text.intersection(regional_keyword_stems):
            metrics['no_regional_keyword_match'] += 1
            logger.debug(log_evt(
                "DROP_NOREGK",
                chat_id=chat_id,
                group_name=group_name,
                region=",".join(candidate_regions) or None,
                msg=lower_text[:180]
            ))
            return
    # else: no subscribers for candidate regions yet — don't drop here; route/AI may find deliverable region later
    # Build stems set from all these subscribers' categories и их подкатегорий
    stems_for_users = set()
    parent_category_stems = set()  # only stems from top‑level categories
    stem_to_category = {}  # new: map stem → category we added it from
    for prefs in subscribers_for_region:
        # категории
        for cat in prefs.get("categories", []):
            cat_entry = categories.get(cat, {})
            # --- add ONLY top‑level keywords to parent_category_stems ---
            for kw in cat_entry.get("keywords", []):
                stem_key = _stem(kw)
                parent_category_stems.add(stem_key)
                stems_for_users.add(stem_key)
                stem_to_category.setdefault(stem_key, cat)
            # --- add all nested stems (parent + subcats) for general matching ---
            for stem in extract_stems(cat_entry):
                stem_key = _stem(stem)
                stems_for_users.add(stem_key)
                stem_to_category.setdefault(stem_key, cat)
        # подкатегории
        for cat, sub_list in prefs.get("subcats", {}).items():
            for sub in sub_list:
                sub_entry = categories.get(cat, {}).get("subcategories", {}).get(sub, {})
                for stem in extract_stems(sub_entry):
                    stem_key = _stem(stem)
                    stems_for_users.add(stem_key)
                    stem_to_category.setdefault(stem_key, f"{cat}/{sub}")

    user_stems = {s.lower() for s in stems_for_users}
    # Detect and log first matched stem
    matched_stem = next((s for s in user_stems if s in stems_in_text), None)
    if not matched_stem:
        metrics['no_category_match'] += 1
        logger.debug("Drop: no category match for users")
        return
    matched_cat = stem_to_category.get(matched_stem, "?")
    kw_log = log_evt("KW", chat_id=chat_id, group_name=group_name, kw=matched_stem, cat=matched_cat, msg=text[:180])
    if kw_log:
        logger.debug(kw_log)

    # ВРЕМЕННО ОТКЛЮЧЕНО: проверка TOP-level keywords
    # if not any(s in stems_in_text for s in parent_category_stems):
    #     metrics['no_parent_category_match'] += 1
    #     logger.debug(f"Drop: no TOP-level keywords. Message stems: {list(stems_in_text)[:10]}, Required: {list(parent_category_stems)[:10]}")
    #     return

    # Построить компактный текст для AI (снижаем токены):
    #  - сохраняем первые 300 символов
    #  - добавляем предложения с покупательскими триггерами
    buyer_triggers = BUYER_TRIGGERS
    
    # Дополнительная предварительная фильтрация перед AI
    # Отсекаем сообщения, которые явно не являются запросами
    offer_terms = OFFER_TERMS
    review_terms = [
        "отлично", "хорошо", "плохо", "ужасно", "не рекомендую", "рекомендую", "советую", 
        "не советую", "опыт", "работал", "работала", "пользовался", "пользовалась"
    ]
    
    lower_clean_text = clean_text.lower()
    buyer_trigger_hit = any(trigger in lower_clean_text for trigger in buyer_triggers)
    question_mark = lower_clean_text.count('?') >= 2
    has_buyer_request = buyer_trigger_hit or question_mark
    has_offer = any(term in lower_clean_text for term in offer_terms)
    has_review = any(term in lower_clean_text for term in review_terms)

    # Targeted filter: excursion/tickets promos with contact but no buyer request
    # Example: "ТУРЕЦКИЙ ДИСНЕЙЛЕНД... Узнать стоимость и забронировать билеты... @user"
    excursion_markers = any(k in lower_clean_text for k in (
        "экскурс", "билет", "land of legends", "легенд"
    ))
    if excursion_markers and contains_contact(lower_clean_text) and not has_buyer_request:
        metrics['pre_offer_filtered'] += 1
        log_info_event("DROP_OFFER", chat_id=chat_id, group_name=group_name, msg=text[:180])
        return

    # Targeted filter: transfer service promos with contact but no buyer request
    # Example: "Надёжный трансфер... Для бронирования пишите @user"
    transfer_markers = any(k in lower_clean_text for k in (
        "трансфер", "transfer"
    ))
    if transfer_markers and contains_contact(lower_clean_text) and not has_buyer_request:
        metrics['pre_offer_filtered'] += 1
        log_info_event("DROP_OFFER", chat_id=chat_id, group_name=group_name, msg=text[:180])
        return

    # Soft gate for AI: if no top keywords and no buyer signal, drop early
    if not has_top_kw and not has_buyer_request:
        metrics['no_global_keyword_match'] += 1
        return
    
    # Если в сообщении есть предложение услуги, но нет запроса - отсекаем
    if has_offer and not has_buyer_request:
        metrics['pre_offer_filtered'] += 1
        log_info_event("DROP_OFFER", chat_id=chat_id, group_name=group_name, msg=text[:180])
        return
        
    # Если в сообщении есть отзыв, но нет запроса - отсекаем
    if has_review and not has_buyer_request:
        metrics['pre_review_filtered'] += 1
        log_info_event("DROP_REVIEW", chat_id=chat_id, group_name=group_name, msg=text[:180])
        return
        
    # Если совсем нет никаких признаков запроса - отсекаем
    if not has_buyer_request and len(clean_text) > 100:  # Для коротких сообщений не применяем
        # Проверяем на наличие других потенциальных триггеров
        potential_triggers = ["ищем", "надо", "можно", "интересует", "занимается", "занимайтесь"]
        has_potential_trigger = any(trigger in lower_clean_text for trigger in potential_triggers)
        
        if not has_potential_trigger:
            metrics['pre_no_trigger_filtered'] += 1
            log_info_event("DROP_NOTRIGGER", chat_id=chat_id, group_name=group_name, msg=text[:180])
            return

    # Ограничиваем длину текста для AI до 400 символов для предотвращения обрезки ответа
    if len(clean_text) > 400:
        parts = re.split(r"(?<=[.!?\n])\s+", clean_text)
        key_sents = [p for p in parts if any(bt in p.lower() for bt in buyer_triggers)]
        head = clean_text[:200]
        ai_input_text = (head + "\n" + "\n".join(key_sents))[:400]
    else:
        ai_input_text = clean_text

    # Единственный AI‑чек: классификация (включает определение релевантности)

    # --- AI_CALL log ---
    logger.debug(
        "AI_CALL | chat=%s (%s) | cat=%s | msg=%r",
        chat_id, group_name, category_heuristic, ai_input_text[:180]
    )

    # AI classification with caching and timeout
    # Add timeout to prevent hanging on AI calls (configurable via env var)
    ai_timeout = float(os.getenv("AI_TIMEOUT", "60.0"))  # Increased default to 60 seconds
    try:
        # Only use [category_heuristic] if present, else full list
        subscriber_cats = {c for prefs in subscriptions.values() for c in prefs.get("categories", [])}
        base_cats = list(subscriber_cats) or list(categories.keys())
        if category_heuristic and category_heuristic not in base_cats:
            base_cats.append(category_heuristic)
        cats_to_use = base_cats

        cla = await asyncio.wait_for(
            asyncio.to_thread(
                classify_text_with_ai,
                ai_input_text,
                cats_to_use,
                CANONICAL_LOCATIONS,
                client_ai
            ),
            timeout=ai_timeout
        )
        # --- AI_OK log ---
        logger.debug(
            "AI_OK | chat=%s (%s) | conf=%.2f | cat=%s | region=%s | exp=%r",
            chat_id, group_name,
            (cla or {}).get("confidence", 0.0),
            (cla or {}).get("category"),
            (cla or {}).get("region"),
            ((cla or {}).get("explanation") or "")[:80]
        )
    except asyncio.TimeoutError:
        logger.error(f"AI_TIMEOUT: Classification timed out after {ai_timeout}s")
        from config import notify_admin_error
        asyncio.create_task(notify_admin_error(f"AI_TIMEOUT: Classification timed out for message: {ai_input_text[:100]}"))
        return
    except Exception as e:
        logger.error(f"AI_ERROR: {e}")
        # Уведомляем админа
        from config import notify_admin_error
        asyncio.create_task(notify_admin_error(f"AI_ERROR: {e}"))
        return

    # Override AI classification with heuristics and post-hoc rules
    if isinstance(cla, dict):
        cla["region"] = region
        cla = apply_overrides(cla, lower_text, category_heuristic)

    # Validate AI subcategory belongs to chosen category (strict contract)
    try:
        assigned_cat = (cla or {}).get("category")
        assigned_sub = (cla or {}).get("subcategory")
        if assigned_cat and assigned_sub:
            cat_obj = categories.get(assigned_cat)
            valid_subs = set((cat_obj or {}).get("subcategories", {}).keys())
            if valid_subs and assigned_sub not in valid_subs:
                cla["relevant"] = False
                cla["explanation"] = "Невалидная подкатегория для категории"
    except Exception:
        # Fail-closed is safer; let subsequent checks discard if needed
        pass

    # Drop if no response or not relevant, with debug explanation
    if not cla or not cla.get("relevant", False):
        metrics['ai_dropped'] += 1
        log_info_event(
            "DROP_AI",
            chat_id=chat_id,
            group_name=group_name,
            cat=(cla or {}).get('category'),
            conf=(cla or {}).get('confidence'),
            msg=text[:180],
            extra=f"exp='{(cla or {}).get('explanation')}'",
        )
        # Append to ai_rejected.log with classification details and concise timestamp
        try:
            ts = now_istanbul().strftime("%m-%d %H:%M")
            with open("ai_rejected.log", "a", encoding="utf-8") as rf:
                rf.write(
                    f"{ts} | {chat_id} ({group_name}) | {text} | "
                    f"relevant:{cla.get('relevant')}, "
                    f"category:{cla.get('category')}, "
                    f"region:{cla.get('region')}, "
                    f"explanation:{cla.get('explanation')}, "
                    f"confidence:{cla.get('confidence')}\n"
                )
        except Exception as e:
            logger.error(f"Failed to write to ai_rejected.log: {e}")
        return

    # Handle low-confidence yet relevant cases
    confidence = cla.get("confidence", 0.0)
    if confidence < DISCARD_THRESHOLD:
        metrics['discarded_low_confidence'] += 1
        log_info_event(
            "DISCARD",
            chat_id=chat_id,
            group_name=group_name,
            cat=cla.get('category'),
            conf=confidence,
            msg=text[:180],
            extra=f"exp='{cla.get('explanation')}'",
        )
        # Append to ai_discarded.log for later analysis
        try:
            ts = now_istanbul().strftime("%m-%d %H:%M")
            with open("ai_discarded.log", "a", encoding="utf-8") as lf:
                lf.write(
                    f"{ts} | {chat_id} ({group_name}) | {text} | "
                    f"relevant:{cla.get('relevant')}, "
                    f"category:{cla.get('category')}, "
                    f"region:{cla.get('region')}, "
                    f"explanation:{cla.get('explanation')}, "
                    f"confidence:{confidence}\n"
                )
        except Exception as e:
            logger.error(f"Failed to write to ai_discarded.log: {e}")
        return
    elif confidence < CONF_THRESHOLD:
        # Gate review by AI category validity and top-level keyword presence
        assigned_cat = cla.get("category")
        if not assigned_cat or assigned_cat not in categories:
            metrics['ai_no_category'] += 1
            logger.debug("Review drop: AI has no valid category")
            return
        assigned_top_kw_stems = {
            _stem(kw) for kw in categories.get(assigned_cat, {}).get("keywords", [])
        }
        if assigned_top_kw_stems and not any(s in stems_in_text for s in assigned_top_kw_stems):
            metrics['ai_cat_no_kw_match'] += 1
            logger.debug(
                f"Review drop: AI cat '{assigned_cat}' but no top-keyword stem present"
            )
            return
        metrics['low_confidence'] += 1
        log_info_event(
            "REVIEW",
            chat_id=chat_id,
            group_name=group_name,
            cat=cla.get('category'),
            conf=confidence,
            msg=text[:180],
            extra=f"exp='{cla.get('explanation')}'",
        )
        
        # Initialize variables that will be used later
        ts = now_istanbul().strftime("%m-%d %H:%M")
        # Build message link for supergroups
        if str(chat_id).startswith("-100"):
            short = str(chat_id)[4:]
            link = f"https://t.me/c/{short}/{event.id}"
        else:
            link = ""
        
        # Admin review (optional via env)
        if os.getenv("ENABLE_ADMIN_REVIEW", "1") == "1":
            # Append to ai_review.log for human check
            try:
                with open("ai_review.log", "a", encoding="utf-8") as lf:
                    lf.write(
                        f"{ts} | {chat_id} ({group_name}) | {text} | "
                        f"relevant:{cla.get('relevant')}, "
                        f"category:{cla.get('category')}, "
                        f"region:{cla.get('region')}, "
                        f"explanation:{cla.get('explanation')}, "
                        f"confidence:{confidence}\n"
                    )
            except Exception as e:
                logger.error(f"Failed to write to ai_review.log: {e}")
            # Send to admin for review
            try:
                from review_handler import send_review_to_admin
                asyncio.create_task(send_review_to_admin({
                    "timestamp": ts,
                    "chat_info": f"{chat_id} ({group_name})",
                    "text": text,
                    "details": f"relevant:{cla.get('relevant')}, "
                               f"category:{cla.get('category')}, "
                               f"region:{cla.get('region')}, "
                               f"explanation:{cla.get('explanation')}, "
                               f"confidence:{confidence}",
                    "link": link,
                    "sender_username": sender_username,
                    "sender_id": sender_id,
                    "category": cla.get('category'),
                    "region": cla.get('region'),
                    "subcategory": cla.get('subcategory'),
                    "confidence": confidence,
                    "explanation": cla.get('explanation')
                }))
            except Exception as e:
                logger.error(f"Failed to send review to admin: {e}")
        return

    # --- FINAL CATEGORY FILTER: строго только твои категории ---
    assigned_cat = cla.get("category")
    if not assigned_cat or assigned_cat not in categories:
        metrics['ai_no_category'] += 1
        return

    # Validate: текст должен содержать хотя бы один TOP-level keyword назначенной категории
    assigned_top_kw_stems = set()
    try:
        for kw in categories.get(assigned_cat, {}).get("keywords", []):
            for tok in WORD_RE.findall(str(kw).lower()):
                assigned_top_kw_stems.add(_stem(tok))
    except Exception:
        assigned_top_kw_stems = set()
    if assigned_top_kw_stems and not any(s in stems_in_text for s in assigned_top_kw_stems):
        if cla.get('confidence', 0.0) >= 0.92:
            metrics['ai_kw_soft_bypass'] += 1
        else:
            metrics['ai_cat_no_kw_match'] += 1
            logger.debug(
                f"Drop: AI cat '{assigned_cat}' but no top-keyword stem present"
            )
            return

    # Дополнительное логирование для анализа точности
    if cla.get('confidence', 0.0) < CONF_THRESHOLD:
        logger.warning(
            f"LOW CONFIDENCE LEAD: conf={cla.get('confidence'):.2f} "
            f"| cat={cla.get('category')} reg={cla.get('region')} "
            f"| exp='{cla.get('explanation')}' | msg='{text[:180]}'"
        )
    
    # Optionally validate region/category but keep region from heuristics

    # Build message link for supergroups
    if str(chat_id).startswith("-100"):
        short = str(chat_id)[4:]
        link = f"https://t.me/c/{short}/{event.id}"
    else:
        link = ""
    # For transfer category, derive route (pickup -> destination) and target regions
    route = None
    regions_for_delivery = {region} if region else set()
    if assigned_cat == "трансфер":
        pickup, dest = extract_transfer_route(lower_text, region)
        route = (pickup, dest)
        regions_for_delivery = {r for r in (pickup, dest) if r}
        if not regions_for_delivery and region:
            regions_for_delivery = {region}

    # Final subscriber check for actual delivery regions
    target_regions = list(regions_for_delivery) if isinstance(regions_for_delivery, (set, list, tuple)) else ([region] if region else [])
    subs_target = [
        prefs for prefs in subscriptions.values()
        if any(r in prefs.get("locations", []) for r in target_regions)
    ]
    if not subs_target:
        metrics['no_subscribers_for_region'] += 1
        log_info_event(
            "DROP_NOSUBS",
            chat_id=chat_id,
            group_name=group_name,
            region=",".join([str(r) for r in target_regions if r]) or None,
        )
        return

    sent_uids, failed_uids = await send_lead_to_users(
        chat_id=chat_id,
        group_name=group_name,
        group_username=getattr(dialog, "username", None),
        sender_name=sender_name,
        sender_id=sender_id,
        sender_username=sender_username,
        text=text,
        link=link,
        region=region,  # backward-compat
        regions=list(regions_for_delivery),
        detected_category=assigned_cat,
        subcategory=cla.get("subcategory"),
        route=route,
        confidence=cla.get("confidence", 0.9)  # Pass confidence level
    )

    sent_count = len(sent_uids)
    extra_info = f"exp='{cla.get('explanation')}' sent={sent_count}"
    if sent_count and failed_uids:
        extra_info += f" failed={len(failed_uids)}"
    sent_log = log_evt(
        "SENT",
        chat_id=chat_id,
        group_name=group_name,
        region=cla.get('region'),
        cat=cla.get('category'),
        conf=cla.get('confidence'),
        kw=matched_stem,
        msg=text[:180],
        extra=extra_info,
    )
    if sent_log:
        logger.info(sent_log)
    if failed_uids:
        logger.warning(f"Lead send failures: {failed_uids}")

# Global variables for bot identity (moved here from later in file)

async def worker(name):
    logger.info(f"⚙️ Worker {name} started")
    while True:
        queue_row = None
        queue_db_id = None
        event_dict = None
        try:
            # Dequeue next event from SQLite
            queue_row = await message_queue.dequeue()
            if not queue_row:
                await asyncio.sleep(0.1)
                continue
            queue_db_id, event_dict = queue_row

            # Воссоздаём событие
            # Создаём фиктивный объект события (минимальный)
            class FakeEvent:
                def __init__(self, data):
                    self.id = data["id"]
                    self.chat_id = data["chat_id"]
                    self._sender_id = data["sender_id"]  # Используем _sender_id
                    self.raw_text = data["text"]
                    self.date = datetime.fromisoformat(data["date"]) if data["date"] else None
                    self.is_group = bool(data.get("is_group", True))
                    self.is_channel = bool(data.get("is_channel", False))
                    self._data = data  # Сохраняем данные
                    self.is_forwarded = bool(data.get("is_forwarded"))
                    self.fwd_from_name = data.get("fwd_from_name")
                    self.fwd_from_id = data.get("fwd_from_id")

                @property
                def sender_id(self):
                    return self._sender_id

                async def get_chat(self):
                    class FakeChat:
                        def __init__(self, data):
                            self.title = data.get("chat_title") or "Восстановленный чат"
                            self.username = data.get("chat_username")
                    return FakeChat(self._data)

                async def get_sender(self):
                    if not self._sender_id:
                        return None
                    class FakeSender:
                        def __init__(self, data):
                            self.id = data["sender_id"]
                            self.first_name = data.get("sender_name", "Неизвестный")
                            self.username = data.get("sender_username")
                    return FakeSender(self._data)

            event = FakeEvent(event_dict)
            await process_message(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"Worker {name} error: {e}\n{tb}")
            if queue_db_id is not None:
                try:
                    await message_queue.mark_failed(queue_db_id, str(e))
                except Exception as mark_err:
                    logger.error(f"Queue mark_failed error: {mark_err}")
            from config import notify_admin_error
            asyncio.create_task(notify_admin_error(f"Worker {name} error: {e}\n{tb}"))
        else:
            if queue_db_id is not None:
                try:
                    await message_queue.mark_completed(queue_db_id)
                except Exception as mark_err:
                    logger.error(f"Queue mark_completed error: {mark_err}")

async def watch_categories():
    """Наблюдение за изменением categories.json (без повторного обновления на старте)."""
    try:
        last_mtime = os.stat("categories.json").st_mtime
    except Exception:
        last_mtime = 0
    while True:
        try:
            stat = os.stat("categories.json")
            if stat.st_mtime > last_mtime:
                last_mtime = stat.st_mtime
                from ai_utils2 import update_categories
                update_categories()
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Ошибка наблюдения за categories.json: {e}")
            await asyncio.sleep(10)
async def main():
    global SELF_ID, SELF_USERNAME
    
    try:
        # Инициализация системы обратной связи
        await initialize_feedback_system()
        
        # Инициализируем очередь
        await message_queue.init_db()
        
        # Обновляем категории
        from ai_utils2 import update_categories
        update_categories()

        # Наблюдение за categories.json
        asyncio.create_task(watch_categories())
        
        # Validate bot token before starting
        if bot_token is None:
            logger.error("❌ Токен бота не указан")
            return

        # Start parser (user) client with authentication first
        logger.info("🚀 Starting parser (user) client...")
        logger.info(f"Env: ENV={os.getenv('ENV')} TG_SESSION={session_name} ALLOWED={sorted(list(ALLOWED_CHATS)) if ALLOWED_CHATS else None}")
        try:
            await client.start()  # type: ignore
            logger.info("✅ Parser client authenticated and started")
        except Exception as e:
            logger.error(f"❌ Failed to start parser client: {e}")
            # Continue with bot client only
        
        # Start bot client with token
        logger.info("🚀 Starting bot client...")
        try:
            await bot_client.start(bot_token=bot_token)  # type: ignore
            me = await bot_client.get_me()
            logger.info("✅ Bot client started and authenticated")
            # Ensure we are running the intended bot (avoid wrong cached session)
            desired_bot_id_str = os.getenv("TARGET_BOT_ID") or os.getenv("BOT_ID") or "8295190028"
            try:
                desired_bot_id = int(desired_bot_id_str)
            except Exception:
                desired_bot_id = None
            if desired_bot_id and getattr(me, 'id', None) and me.id != desired_bot_id:
                logger.error(
                    f"❌ Wrong bot session in use: logged as id={me.id}, expected id={desired_bot_id}. "
                    f"Set BOT_SESSION to a new name or delete the old session file and rerun."
                )
                # Stop early to prevent sending from the wrong bot
                return
        except Exception as e:
            logger.error(f"🤖 Не удалось запустить бота: {e}")
            # If both clients failed, abort; otherwise continue
            try:
                if not client.is_connected():
                    return
            except Exception:
                return
        
        # Now register clients with connection manager for monitoring
        logger.info("🔍 Setting up connection monitoring...")
        add_telegram_client("parser", client, is_bot=False)
        add_telegram_client("bot", bot_client, is_bot=True)

        # Safe extraction of bot information
        try:
            SELF_ID = getattr(me, 'id', None)
            SELF_USERNAME = getattr(me, 'username', None)
            logger.info(f"✅ Bot logged in as @{SELF_USERNAME} (id={SELF_ID})")
        except Exception as e:
            logger.warning(f"Bot login info unavailable: {e}")
            SELF_ID = None
            SELF_USERNAME = None

        logger.info("🚀 Both clients running")
        
        # Connection monitoring will be enabled manually if needed
        # For now, skip monitoring to avoid health check issues during startup
        logger.info("🔍 Starting connection monitoring...")
        monitor_tasks = await start_connection_monitoring(check_interval=60.0)
        
        if VERBOSE_DEBUG:
            # Start background tasks
            asyncio.create_task(metrics_dump_task())

        # Start message workers
        for i in range(MAX_WORKERS):
            asyncio.create_task(worker(f"worker-{i}"))
        logger.info(f"🧵 Started {MAX_WORKERS} worker(s)")

        # Run both clients until disconnected with improved error handling
        try:
            # Создаем задачи для обоих клиентов
            client_task = asyncio.create_task(client.run_until_disconnected())  # type: ignore
            bot_client_task = asyncio.create_task(bot_client.run_until_disconnected())  # type: ignore
            
            # Wait for either client to disconnect
            done, pending = await asyncio.wait(
                [client_task, bot_client_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                
        except KeyboardInterrupt:
            logger.info("📴 Бот остановлен пользователем")
        except Exception as e:
            logger.error(f"❌ Ошибка в основном цикле клиента: {e}")
            # Enhanced error reporting
            import traceback
            tb = traceback.format_exc()
            logger.error(f"Main loop error details: {tb}")
        finally:
            # Clean shutdown
            logger.info("🔌 Shutting down connections...")
            await disconnect_all_clients()
            
    except KeyboardInterrupt:
        logger.info("📴 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при запуске: {e}")
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Startup error details: {tb}")
    finally:
        # Final cleanup
        try:
            await disconnect_all_clients()
        except Exception as e:
            logger.error(f"Error during final cleanup: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("📴 Bot stopped by user")
        # Graceful exit on Ctrl+C
        logger.info("=== Metrics Summary ===")
        for key, count in metrics.items():
            logger.info(f"{key}: {count}")
