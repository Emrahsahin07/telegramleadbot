import logging
import os
import re
from html import escape
import snowballstemmer
from telethon import Button
from telethon import events
from telethon.errors.rpcerrorlist import UserIsBlockedError
import hashlib
from datetime import datetime, timezone, timedelta
from filters import extract_stems
from config import bot_client, ADMIN_ID, categories, subscriptions, save_subscriptions, metrics, logger
from feedback_manager import feedback_manager
import asyncio

WORD_RE = re.compile(r"[а-яa-zё]+", re.IGNORECASE | re.UNICODE)
_ru_stemmer = snowballstemmer.stemmer('russian')
def _stem(word: str) -> str:
    return _ru_stemmer.stemWord(word.lower())

def _send_enabled() -> bool:
    # Controlled by SEND_NOTIFICATIONS env var; default is enabled ("1")
    return os.getenv("SEND_NOTIFICATIONS", "1") == "1"

# Create a lock for subscription updates
_subscription_lock = asyncio.Lock()

def build_lead_buttons(link, sender_username, sender_id, message_id=None):
    """Создаёт кнопки для лида: Сообщение + Пользователь + Feedback"""
    buttons = []
    
    # First row: Message and User buttons
    if link:
        # Link to user profile: by username if available, else by ID
        user_url = f"https://t.me/{sender_username}" if sender_username else f"tg://user?id={sender_id}"
        buttons.append([
            Button.url("Сообщение", link),
            Button.url("Пользователь", user_url)
        ])
    
    # Second row: Feedback buttons
    if message_id:
        buttons.append([
            Button.inline("👍 Полезно", f"feedback:{message_id}:useful"),
            Button.inline("👎 Не полезно", f"feedback:{message_id}:not_useful")
        ])
    
    return buttons if buttons else None

from typing import Union

async def send_lead_to_users(
    *,
    chat_id: int,
    group_name: str,
    group_username: Union[str, None],
    sender_name: str,
    sender_id: int,
    sender_username: Union[str, None],
    text: str,
    link: str,
    region: str,
    regions: list,
    detected_category: str,
    subcategory: Union[str, None] = None,
    route = None,
    confidence: float = 0.9  # Add confidence parameter
):
    # Ensure we are using the intended bot identity; skip if mismatch
    try:
        desired_bot_id_str = os.getenv("TARGET_BOT_ID") or os.getenv("BOT_ID")
        desired_bot_id = int(desired_bot_id_str) if desired_bot_id_str else None
    except Exception:
        desired_bot_id = None
    try:
        me = await bot_client.get_me()
        current_bot_id = getattr(me, 'id', None)
    except Exception:
        current_bot_id = None
    if desired_bot_id and current_bot_id and current_bot_id != desired_bot_id:
        logger.error(f"SKIP delivery: running under wrong bot id={current_bot_id}, expected id={desired_bot_id}")
        return

    if not _send_enabled():
        logger.info("[DEV] Delivery disabled; skipping notifications")
        return
    sent_uids: list[int] = []
    failed_uids = []
    # Send to each user based on their subscriptions
    for uid_str, prefs in subscriptions.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        now = datetime.now(timezone.utc)
        # Debug trial/subscription state
        logger.debug(f"[DEBUG TRIAL] User {uid_str}: subscription_end={prefs.get('subscription_end')}, trial_start={prefs.get('trial_start')}, now={now.isoformat()}")
        # Check paid subscription first
        sub_end = prefs.get('subscription_end')
        if sub_end:
            end = datetime.fromisoformat(sub_end)
            if now > end:
                # Paid subscription expired: notify user once
                if not prefs.get('paid_expired_notified'):
                    await bot_client.send_message(
                        uid,
                        "⌛ Ваша подписка закончилась. Чтобы продолжить получать лиды, нажмите кнопку:",
                        buttons=[[Button.inline("Подписаться", b"menu:subscribe")]]
                    )
                    async with _subscription_lock:
                        prefs['paid_expired_notified'] = True
                        # Save updated subscriptions
                        save_subscriptions()
                metrics['sub_expired_skipped'] += 1
                continue
        else:
            # No paid subscription: check trial
            ts = prefs.get('trial_start')
            if not ts:
                # Trial not started yet
                continue
            start = datetime.fromisoformat(ts)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if now - start > timedelta(days=2):
                # Trial expired: notify user once
                if not prefs.get('trial_expired_notified'):
                    await bot_client.send_message(
                        uid,
                        "⌛ Ваш пробный период закончился. Чтобы продолжить получать лиды, нажмите кнопку:",
                        buttons=[[Button.inline("Подписаться", b"menu:subscribe")]]
                    )
                    async with _subscription_lock:
                        prefs['trial_expired_notified'] = True
                        # Save updated subscriptions
                        save_subscriptions()
                metrics['trial_expired_skipped'] += 1
                continue
        keywords = []
        # Stems from выбранных категорий
        for cat in prefs.get("categories", []):
            keywords.extend(extract_stems(categories.get(cat, {})))

        # Stems from выбранных подкатегорий
        for cat, sub_list in prefs.get("subcats", {}).items():
            for sub in sub_list:
                sub_entry = categories.get(cat, {}).get("subcategories", {}).get(sub, {})
                keywords.extend(extract_stems(sub_entry.get("keywords", [])))

        keywords = [str(k) for k in keywords]
        locations = prefs.get("locations", [])
        target_regions = set(regions or ([region] if region else []))
        # Changed logic: Send if ANY of the detected regions match user's subscribed locations
        # This ensures users get transfer messages that involve their region, even if other regions are also mentioned
        user_locations_set = set(locations)
        if not target_regions or not user_locations_set.intersection(target_regions):
            metrics['pref_region_skipped'] += 1
            logger.debug(f"Drop user {uid}: regions {sorted(target_regions)} don't match any of {locations}")
            continue
        # --- strict AI‑category filter ---------------------------------
        # Отправляем лид только если AI определил категорию
        # и она входит в подписку пользователя.
        if detected_category and detected_category not in prefs.get("categories", []):
            metrics['pref_ai_category_skipped'] += 1
            logger.debug(f"Drop user {uid}: AI category '{detected_category}' not in {prefs.get('categories')}")
            continue

        # Если есть подкатегория — проверяем, подписан ли пользователь
        if subcategory and detected_category:
            user_subcats = prefs.get("subcats", {}).get(detected_category, [])
            if user_subcats and subcategory not in user_subcats:
                metrics['pref_ai_subcategory_skipped'] += 1
                logger.debug(f"Drop user {uid}: AI subcategory '{subcategory}' not in {user_subcats}")
                continue
        # Стемминг keywords и текста:
        keyword_stems = {_stem(kw.lower()) for kw in keywords}
        text_stems = {_stem(tok) for tok in WORD_RE.findall(text.lower())}

        if not keyword_stems & text_stems:
            metrics['pref_category_skipped'] += 1
            logger.debug(f"Drop user {uid}: no keyword stems match")
            continue
        # Build clickable group name using username if available
        if group_username:
            chat_url = f"https://t.me/{group_username}"
        else:
                        # If we have a direct message link to a private/supergroup, use it; avoid bare t.me/c/<id>
            if link and link.startswith("https://t.me/"):
                if re.search(r"/c/\d+/\d+$", link):
                    chat_url = link  # message link opens the app correctly
                elif "/c/" not in link:
                    parts = link.rsplit("/", 1)
                    chat_url = parts[0] if len(parts) == 2 else link
                else:
                    chat_url = ""
            else:
                chat_url = ""
        # Remove inline hashtags from original text to avoid duplicate tags in footer
        text_no_tags = re.sub(r"#\w+", "", text or "").strip()
        # Escape values for HTML output
        safe_group_name = escape(group_name or "")
        safe_sender_name = escape(sender_name or "")
        safe_sender_username = escape(sender_username or "")
        safe_text = escape(text_no_tags)
        # Prefer showing @username as sender when available
        display_sender = f"@{safe_sender_username}" if sender_username else safe_sender_name
        if chat_url:
            group_display = f'<a href="{chat_url}">{safe_group_name}</a>'
        else:
            group_display = safe_group_name
        if route and any(route):
            a, b = route
            if a and b:
                region_tag = f"#{a.lower()} → #{b.lower()}"
            elif a:
                region_tag = f"#{a.lower()}"
            elif b:
                region_tag = f"#{b.lower()}"
            else:
                region_tag = f"#{region.lower()}" if region else ""
        else:
            # fallback to list of regions if provided
            if regions:
                region_tag = " ".join(f"#{r.lower()}" for r in regions)
            else:
                region_tag = f"#{region.lower()}" if region else ""
        # Use AI-detected category if provided, fallback to subscriber's first category
        if detected_category:
            ai_category_tag = f"#{detected_category.lower()}"
            if subcategory:
                ai_category_tag += f" #{subcategory.lower()}"
        else:
            cats = prefs.get("categories", [])
            ai_category_tag = f"#{cats[0].lower()}" if cats else ""
        msg = (
            f"📩 {group_display} | {display_sender}\n\n"
            f"- {safe_text}\n\n"
            f"{region_tag} {ai_category_tag}".strip()
        )
        # Deliver only if confidence >= 0.79 (per routing contract)
        if confidence < 0.79:
            logger.debug(f"Below deliver threshold ({confidence:.2f}) - skip user {uid}; handled by review/discard")
            continue

        # High confidence - standard buttons (no feedback row by default)
        message_id = None
        buttons = build_lead_buttons(link, sender_username, sender_id, message_id=None)
        
        # Send message with the constructed buttons
        try:
            await bot_client.send_message(
                uid,
                msg,
                parse_mode="HTML",
                link_preview=False,
                buttons=buttons  # ← Используем кнопки с feedback
            )
            sent_uids.append(uid)
        except UserIsBlockedError:
            logger.info(f"User {uid} blocked the bot; skipping lead delivery")
            failed_uids.append(uid)
            continue
        except Exception as e:
            metrics['send_errors'] += 1
            failed_uids.append(uid)
            logger.error(f"Failed to send lead to {uid}: {e}")
    # Notify admin if any sends failed
    if failed_uids and os.getenv("NOTIFY_SEND_ERRORS", "1") == "1":
        try:
            try:
                me = await bot_client.get_me()
                bot_id_info = f" (bot id {getattr(me, 'id', None)})"
            except Exception:
                bot_id_info = ""
            await bot_client.send_message(
                ADMIN_ID,
                f"⚠️ Ошибка рассылки лида пользователям: {len(failed_uids)} не удалось отправить. UIDs: {failed_uids}{bot_id_info}"
            )
        except Exception as notify_error:
            logger.error(f"Failed to notify admin about send errors: {notify_error}")

    return sent_uids, failed_uids
