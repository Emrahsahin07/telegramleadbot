from __future__ import annotations

import os
import json
import logging
from logging.handlers import RotatingFileHandler
from collections import Counter
from dotenv import load_dotenv
from datetime import datetime
from telethon import TelegramClient

# Base directory path for this config file
BASE_DIR = os.path.dirname(__file__)

# Load environment variables
# Check if we're in dev mode and load appropriate env file
env_mode = os.getenv("ENV", "production")
if env_mode == "dev":
    load_dotenv(os.path.join(BASE_DIR, ".env.dev"))
else:
    load_dotenv(os.path.join(BASE_DIR, ".env"))

# Debug mode flag
DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "yes")

# Telegram and OpenAI credentials
api_id_str = os.getenv("API_ID")
if not api_id_str:
    raise RuntimeError("API_ID is not set in .env")
API_ID = int(api_id_str)

API_HASH = os.getenv("API_HASH")
if not API_HASH:
    raise RuntimeError("API_HASH is not set in .env")

BOT_TOKEN = os.getenv("LEADBOT_TOKEN", os.getenv("BOT_TOKEN"))
if not BOT_TOKEN:
    raise RuntimeError("LEADBOT_TOKEN is not set in .env")

ADMIN_ID_STR = os.getenv("ADMIN_ID", "459865003")
if not ADMIN_ID_STR.isdigit():
    raise RuntimeError("ADMIN_ID must be integer")
ADMIN_ID = int(ADMIN_ID_STR)

# Load categories.json
categories_path = os.path.join(BASE_DIR, "categories.json")
try:
    with open(categories_path, encoding="utf-8") as f:
        categories = json.load(f)
except Exception as e:
    raise RuntimeError(f"Failed to load categories.json: {e}")

# Load subscriptions.json or initialize
subscriptions_path = os.path.join(BASE_DIR, "subscriptions.json")
if os.path.exists(subscriptions_path):
    with open(subscriptions_path, encoding="utf-8") as f:
        subscriptions = json.load(f)
else:
    subscriptions = {}

def save_subscriptions() -> None:
    """Save current subscriptions back to JSON file (atomic write)."""
    tmp_path = subscriptions_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(subscriptions, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, subscriptions_path)

# Initialize logger
logger = logging.getLogger("leadbot")
# Honor DEBUG env by elevating logger level
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        os.path.join(BASE_DIR, "bot.log"),
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%m-%d %H:%M")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Also log to console (useful in dev)
    if DEBUG:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

logger.propagate = False

# Metrics counter
metrics = Counter({
    "received": 0,
    "deduplicated": 0,
    "negative_ctx_filtered": 0,
    "no_global_keyword_match": 0,
    "no_region": 0,
    "region_detected": 0,
    "category_heuristic_detected": 0,
    "category_not_detected": 0,
    "coverage_ok": 0,
    "no_subscribers_for_region": 0,
    "no_regional_keyword_match": 0,
    "no_category_match": 0,
    "pre_ad_filtered": 0,
    "pre_offer_filtered": 0,
    "pre_review_filtered": 0,
    "pre_no_trigger_filtered": 0,
    "ai_dropped": 0,
    "discarded_low_confidence": 0,
    "low_confidence": 0,
    "ai_no_category": 0,
    "ai_cat_no_kw_match": 0,
    "leads_sent": 0,
    "send_errors": 0,
    "dedup_text": 0,
    "sub_expired_skipped": 0,
    "trial_expired_skipped": 0,
    "pref_region_skipped": 0,
    "pref_ai_category_skipped": 0,
    "pref_ai_subcategory_skipped": 0,
    "pref_category_skipped": 0
})

# Create bot client with enhanced connection settings
# Allow overriding bot session filename to avoid SQLite locks from stale processes
BOT_SESSION_NAME = os.getenv("BOT_SESSION", "bot_session")
bot_client = TelegramClient(
    os.path.join(BASE_DIR, BOT_SESSION_NAME), 
    API_ID, 
    API_HASH,
    connection_retries=10,  # Increased retries
    retry_delay=2,  # Delay between retries  
    timeout=30,  # Connection timeout
    request_retries=5,  # Request retries
    flood_sleep_threshold=60  # Auto-sleep on flood wait
)

LOCATION_ALIAS = {
    # Russian aliases
    "анталия": "Анталия",
    "анталья": "Анталия",
    "анталии": "Анталия",  # dative case
    "анталию": "Анталия",  # accusative case
    "алания": "Алания",
    "аланья": "Алания",
    "авсаллар": "Авсаллар",
    "кемер": "Кемер",
    "кемера": "Кемер",
    "кемере": "Кемер",
    "стамбул": "Стамбул",
    "стамбула": "Стамбул",
    "стамбуле": "Стамбул",
    "белдиби": "Бельдиби",
    "бельдиби": "Бельдиби",
    "белека": "Белек",
    "белеке": "Белек",
    "гейнюк": "Гёйнюк",
    "гёйнюк": "Гёйнюк",
    "манавгат": "Манавгат",
    "чамьюва": "Чамьюва",
    "турция": "Турция",
    "турции": "Турция",
    "турцию": "Турция",
    "мерсин": "Мерсин",
    "мерсина": "Мерсин",
    "мерсине": "Мерсин",
    "сиде": "Сиде",
    "фетхие": "Фетхие",
    # Latin aliases (strict word boundaries are handled in filters)
    "antalya": "Анталия",
    "alanya": "Алания",
    "mersin": "Мерсин",
    "side": "Сиде",
    "fethiye": "Фетхие",
    "kemer": "Кемер",
    "istanbul": "Стамбул",
}

# Configuration validation function
def validate_config():
    """Validate all required configuration settings"""
    errors = []
    
    # Check required environment variables
    required_env_vars = [
        "API_ID", "API_HASH", "OPENAI_API_KEY"
    ]
    
    for var in required_env_vars:
        if not os.getenv(var):
            errors.append(f"Missing required environment variable: {var}")
    
    # Validate bot token
    if not BOT_TOKEN:
        errors.append("Missing bot token (LEADBOT_TOKEN or BOT_TOKEN)")
    
    # Validate categories file
    if not categories:
        errors.append("categories.json is empty or invalid")
    
    # Validate OpenAI model
    # Default to the requested model
    model = os.getenv("OPENAI_MODEL", "gpt-5-nano")
    if not model.startswith(("gpt-", "5-")):
        logger.warning(f"Unknown OpenAI model: {model}")
    
    if errors:
        error_msg = "Configuration validation failed:\n" + "\n".join(f"• {error}" for error in errors)
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    logger.info("✅ Configuration validation passed")

# Validate configuration on import
try:
    validate_config()
except Exception as e:
    logger.error(f"Configuration validation failed: {e}")
    raise

# --- Telegram Notification on Error ---
async def notify_admin_error(error_msg: str):
    try:
        if not BOT_TOKEN:
            logger.error("Cannot notify admin: BOT_TOKEN not set")
            return
            
        if not bot_client.is_connected():
            await bot_client.start(bot_token=BOT_TOKEN)  # type: ignore
        await bot_client.send_message(
            ADMIN_ID,
            f"⚠️ Ошибка в боте!\n"
            f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Ошибка: {error_msg}"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить админа: {e}")
