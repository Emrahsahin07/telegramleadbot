import logging
import os
import re
from contextlib import suppress
from dataclasses import dataclass, field
from html import escape
import hashlib
import snowballstemmer
from telethon import Button
from telethon import events
from telethon.errors import (
    UserIsBlockedError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    FloodWaitError,
    RPCError,
    ChatWriteForbiddenError,
    MessageTooLongError,
    UserPrivacyRestrictedError,
)
from datetime import datetime, timezone, timedelta
from filters import extract_stems
from config import (
    bot_client,
    ADMIN_ID,
    categories,
    subscriptions,
    save_subscriptions,
    metrics,
    logger,
    normalize_location,
    parse_iso_datetime,
)
from feedback_manager import feedback_manager
import message_queue
import asyncio
from decision_policy import AUTO_SEND_THRESHOLD

WORD_RE = re.compile(r"[а-яa-zё]+", re.IGNORECASE | re.UNICODE)
_ru_stemmer = snowballstemmer.stemmer('russian')
def _stem(word: str) -> str:
    return _ru_stemmer.stemWord(word.lower())

def _send_enabled() -> bool:
    # Controlled by SEND_NOTIFICATIONS env var; default is enabled ("1")
    return os.getenv("SEND_NOTIFICATIONS", "1") == "1"


class StartupConfigurationError(RuntimeError):
    pass


def validate_delivery_mode() -> str:
    """Validate rollout flags and lease/send timing before runtime startup."""
    write_value = os.getenv("WRITE_OUTBOX", "0")
    worker_value = os.getenv("DELIVERY_OUTBOX_WORKER", "0")
    if write_value not in {"0", "1"} or worker_value not in {"0", "1"}:
        raise StartupConfigurationError(
            "WRITE_OUTBOX and DELIVERY_OUTBOX_WORKER must be exactly 0 or 1"
        )
    if write_value != worker_value:
        raise StartupConfigurationError(
            "WRITE_OUTBOX and DELIVERY_OUTBOX_WORKER must be enabled or disabled together"
        )
    if write_value == "0":
        return "legacy"

    try:
        lease_seconds = float(os.getenv("DELIVERY_OUTBOX_LEASE_SECONDS", "120"))
        send_timeout = float(os.getenv("DELIVERY_SEND_TIMEOUT_SECONDS", "25"))
        db_busy_timeout = float(os.getenv("DB_BUSY_TIMEOUT_SEC", "5"))
        safety_margin = float(os.getenv("DELIVERY_LEASE_SAFETY_MARGIN_SECONDS", "15"))
        heartbeat_seconds = float(os.getenv("DELIVERY_LEASE_HEARTBEAT_SECONDS", "10"))
        queue_lease_seconds = float(os.getenv("QUEUE_LEASE_SECONDS", "300"))
        delivery_attempts = int(os.getenv("DELIVERY_MAX_ATTEMPTS", "5"))
        queue_attempts = int(os.getenv("QUEUE_MAX_ATTEMPTS", "5"))
        retry_base = int(os.getenv("DELIVERY_RETRY_BASE_SECONDS", "5"))
        retry_cap = int(os.getenv("DELIVERY_RETRY_MAX_SECONDS", "900"))
    except ValueError as error:
        raise StartupConfigurationError(
            "outbox lease/retry settings must be numeric"
        ) from error
    if (
        send_timeout <= 0
        or lease_seconds <= 0
        or db_busy_timeout < 0
        or safety_margin <= 0
        or send_timeout + db_busy_timeout + safety_margin >= lease_seconds
    ):
        raise StartupConfigurationError(
            "delivery lease must exceed send timeout + SQLite busy timeout + "
            "DELIVERY_LEASE_SAFETY_MARGIN_SECONDS"
        )
    if (
        queue_lease_seconds <= 0
        or delivery_attempts < 1
        or queue_attempts < 1
        or retry_base < 1
        or retry_cap < retry_base
        or heartbeat_seconds <= 0
        or heartbeat_seconds + db_busy_timeout + safety_margin >= lease_seconds
    ):
        raise StartupConfigurationError(
            "queue/delivery leases, attempt budgets, and retry bounds are invalid"
        )
    return "outbox"


def configure_delivery_runtime(mode: str) -> None:
    if mode == "outbox":
        # Telethon must surface FloodWaitError immediately. The durable retry
        # scheduler owns the wait; Telegram calls must not sleep while leased.
        bot_client.flood_sleep_threshold = 0


def _outbox_enabled(event_id=None) -> bool:
    mode = validate_delivery_mode()
    if mode == "outbox" and event_id is None:
        raise ValueError("event_id is required in outbox mode")
    return mode == "outbox"


def _outbox_retry_delay(error: Exception, attempts: int) -> int:
    base = max(1, int(os.getenv("DELIVERY_RETRY_BASE_SECONDS", "5")))
    cap = max(base, int(os.getenv("DELIVERY_RETRY_MAX_SECONDS", "900")))
    delay = min(cap, base * (2 ** max(0, attempts - 1)))
    if isinstance(error, FloodWaitError):
        delay = max(delay, int(getattr(error, "seconds", delay)))
    return delay


def _outbox_retry_budget() -> int:
    return max(1, int(os.getenv("DELIVERY_MAX_ATTEMPTS", "5")))


PERMANENT_DELIVERY_ERRORS = (
    UserIsBlockedError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    ChatWriteForbiddenError,
    UserPrivacyRestrictedError,
    MessageTooLongError,
)


@dataclass
class DeliveryResult:
    mode: str
    queued_uids: list[int] = field(default_factory=list)
    delivered_uids: list[int] = field(default_factory=list)
    failed_uids: list[int] = field(default_factory=list)


async def _send_outbox_payload(recipient_uid: int, payload: dict):
    buttons = build_lead_buttons(
        payload.get("link"),
        payload.get("sender_username"),
        payload.get("sender_id"),
        message_id=payload.get("feedback_message_id"),
    )
    send_timeout = float(os.getenv("DELIVERY_SEND_TIMEOUT_SECONDS", "25"))
    return await asyncio.wait_for(
        bot_client.send_message(
            recipient_uid,
            payload["message"],
            parse_mode="HTML",
            link_preview=False,
            buttons=buttons,
        ),
        timeout=send_timeout,
    )


async def _maintain_delivery_lease(
    delivery_row: dict,
    send_task: asyncio.Task,
    ownership_lost: asyncio.Event,
) -> None:
    heartbeat_seconds = float(
        os.getenv("DELIVERY_LEASE_HEARTBEAT_SECONDS", "10")
    )
    lease_seconds = int(os.getenv("DELIVERY_OUTBOX_LEASE_SECONDS", "120"))
    while True:
        await asyncio.sleep(heartbeat_seconds)
        try:
            renewed = await message_queue.renew_delivery_outbox_lease(
                delivery_row["id"],
                delivery_row["lease_token"],
                lease_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Delivery lease heartbeat failed for outbox row %s: %s",
                delivery_row["id"],
                error,
            )
            renewed = False
        if renewed:
            continue
        ownership_lost.set()
        if not send_task.done():
            send_task.cancel()
        return


async def _store_outbox_feedback(recipient_uid: int, payload: dict, sent_message) -> None:
    feedback = payload["feedback"]
    classification = dict(feedback["ai_classification"])
    classification["telegram_message_id"] = getattr(sent_message, "id", None)
    await feedback_manager.store_lead_sent(
        message_id=payload["feedback_message_id"],
        user_id=str(recipient_uid),
        message_text=feedback["message_text"],
        ai_classification=classification,
        category=feedback["category"],
        region=feedback["region"],
        confidence=feedback["confidence"],
    )


async def deliver_next_outbox(event_id=None):
    """Attempt one due outbox row and persist its recipient-local outcome."""
    delivery_row = await message_queue.claim_delivery_outbox(event_id=event_id)
    if delivery_row is None:
        return None

    uid = delivery_row["recipient_uid"]
    lease_token = delivery_row["lease_token"]
    ownership_lost = asyncio.Event()
    send_task = asyncio.create_task(
        _send_outbox_payload(uid, delivery_row["payload"])
    )
    heartbeat_task = asyncio.create_task(
        _maintain_delivery_lease(delivery_row, send_task, ownership_lost)
    )
    try:
        try:
            sent_message = await send_task
        except asyncio.CancelledError:
            if ownership_lost.is_set():
                logger.error(
                    "Active Telegram send cancelled after lease ownership was lost "
                    "for outbox row %s",
                    delivery_row["id"],
                )
                return "stale", uid
            raise
        except PERMANENT_DELIVERY_ERRORS as error:
            if ownership_lost.is_set():
                return "stale", uid
            transitioned = await message_queue.mark_delivery_outbox_dead(
                delivery_row["id"],
                lease_token,
                f"{type(error).__name__}: {error}",
            )
            if not transitioned:
                logger.warning(
                    "Stale permanent transition ignored for outbox row %s",
                    delivery_row["id"],
                )
                return "stale", uid
            logger.info(
                "Permanent delivery failure for user %s: %s",
                uid,
                type(error).__name__,
            )
            return "dead", uid
        except Exception as error:
            if ownership_lost.is_set():
                return "stale", uid
            status = await message_queue.mark_delivery_outbox_retry(
                delivery_row["id"],
                lease_token,
                f"{type(error).__name__}: {error}",
                _outbox_retry_delay(error, delivery_row["attempts"]),
                max_attempts=_outbox_retry_budget(),
            )
            if status is None:
                logger.warning(
                    "Stale retry transition ignored for outbox row %s",
                    delivery_row["id"],
                )
                return "stale", uid
            metrics["send_errors"] += 1
            if isinstance(error, RPCError):
                logger.error(
                    "Unknown/retryable Telegram RPC error class=%s code=%s message=%s",
                    type(error).__name__,
                    getattr(error, "code", None),
                    getattr(error, "message", str(error)),
                )
            logger.error(
                "Transient delivery failure for user %s "
                "(attempt %s/%s, status=%s): %s",
                uid,
                delivery_row["attempts"],
                _outbox_retry_budget(),
                status,
                error,
            )
            return status, uid

        if ownership_lost.is_set():
            logger.error(
                "Telegram send returned after lease ownership was lost for outbox row %s",
                delivery_row["id"],
            )
            return "stale", uid

        transitioned = await message_queue.mark_delivery_outbox_delivered(
            delivery_row["id"],
            lease_token,
            getattr(sent_message, "id", None),
        )
        if not transitioned:
            logger.error(
                "Telegram send succeeded after lease ownership was lost for outbox row %s",
                delivery_row["id"],
            )
            return "stale", uid
        try:
            await _store_outbox_feedback(uid, delivery_row["payload"], sent_message)
        except Exception as error:
            # Telegram delivery is already durable. Feedback failure must never turn
            # a successful send into a retry that would duplicate the lead.
            logger.error(
                "Failed to store feedback for delivered outbox row %s: %s",
                delivery_row["id"],
                error,
            )
        return "delivered", uid
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


async def delivery_outbox_worker(poll_interval: float = 1.0):
    """Continuously process due outbox rows when rollout flags are enabled."""
    while True:
        try:
            outcome = await deliver_next_outbox()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Delivery outbox worker error: %s", error)
            await asyncio.sleep(poll_interval)
            continue
        if outcome is None:
            await asyncio.sleep(poll_interval)

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


def _build_feedback_message_id(uid: int, chat_id: int, text: str, link: str = "") -> str:
    nonce = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha1(f"{uid}:{chat_id}:{link}:{text}:{nonce}".encode("utf-8")).hexdigest()[:12]
    return f"lead_{uid}_{chat_id}_{digest}"

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
    confidence: float = 0.9,  # Add confidence parameter
    event_id: Union[int, str, None] = None,
    queue_id: Union[int, None] = None,
    queue_lease_token: Union[str, None] = None,
) -> DeliveryResult:
    use_outbox = _outbox_enabled(event_id)
    mode = "outbox" if use_outbox else "legacy"
    if use_outbox and ((queue_id is None) != (queue_lease_token is None)):
        raise ValueError("queue_id and queue_lease_token must be provided together")

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
        if use_outbox:
            raise RuntimeError(
                f"outbox delivery refused for wrong bot id={current_bot_id}"
            )
        return DeliveryResult(mode=mode)

    if not _send_enabled():
        logger.info("[DEV] Delivery disabled; skipping notifications")
        return DeliveryResult(mode=mode)
    sent_uids: list[int] = []
    failed_uids = []
    outbox_entries = []
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
            end = parse_iso_datetime(sub_end)
            if end is None:
                logger.warning(f"User {uid} has invalid subscription_end={sub_end!r}")
                continue
            if now > end:
                # Paid subscription expired: notify user once
                if not prefs.get('paid_expired_notified'):
                    try:
                        await bot_client.send_message(
                            uid,
                            "⌛ Ваша подписка закончилась. Чтобы продолжить получать лиды, нажмите кнопку:",
                            buttons=[[Button.inline("Подписаться", b"menu:subscribe")]]
                        )
                    except UserIsBlockedError:
                        logger.info(f"User {uid} blocked the bot; skipping")
                        continue
                    except InputUserDeactivatedError:
                        logger.info(f"User {uid} is deleted/deactivated; skipping")
                        continue
                    except PeerIdInvalidError:
                        logger.info(f"User {uid} has invalid Telegram peer; skipping")
                        continue
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
            start = parse_iso_datetime(ts)
            if start is None:
                logger.warning(f"User {uid} has invalid trial_start={ts!r}")
                continue
            if now - start > timedelta(days=2):
                # Trial expired: notify user once
                if not prefs.get('trial_expired_notified'):
                    try:
                        await bot_client.send_message(
                            uid,
                            "⌛ Ваш пробный период закончился. Чтобы продолжить получать лиды, нажмите кнопку:",
                            buttons=[[Button.inline("Подписаться", b"menu:subscribe")]]
                        )
                    except UserIsBlockedError:
                        logger.info(f"User {uid} blocked the bot during trial expiry notice; skipping notification")
                        continue
                    except InputUserDeactivatedError:
                        logger.info(
                            f"User {uid} is deleted/deactivated during trial expiry notice; skipping notification"
                        )
                        continue
                    except PeerIdInvalidError:
                        logger.info(
                            f"User {uid} has invalid Telegram peer during trial expiry notice; skipping notification"
                        )
                        continue
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
        locations = {
            normalize_location(loc)
            for loc in prefs.get("locations", [])
            if normalize_location(loc)
        }
        target_regions = {
            normalize_location(loc)
            for loc in (regions or ([region] if region else []))
            if normalize_location(loc)
        }
        # Changed logic: Send if ANY of the detected regions match user's subscribed locations
        # This ensures users get transfer messages that involve their region, even if other regions are also mentioned
        if not target_regions or not locations.intersection(target_regions):
            metrics['pref_region_skipped'] += 1
            logger.debug(f"Drop user {uid}: regions {sorted(target_regions)} don't match any of {sorted(locations)}")
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
        # Deliver only above the centralized automatic-decision threshold.
        if confidence < AUTO_SEND_THRESHOLD:
            logger.debug(f"Below deliver threshold ({confidence:.2f}) - skip user {uid}; handled by review/discard")
            continue

        # High confidence - standard buttons (no feedback row by default)
        feedback_message_id = _build_feedback_message_id(uid, chat_id, text, link)
        buttons = build_lead_buttons(link, sender_username, sender_id, message_id=feedback_message_id)

        if use_outbox:
            outbox_entries.append(
                {
                    "recipient_uid": uid,
                    "payload": {
                        "message": msg,
                        "link": link,
                        "sender_username": sender_username,
                        "sender_id": sender_id,
                        "feedback_message_id": feedback_message_id,
                        "feedback": {
                            "message_text": text,
                            "ai_classification": {
                                "category": detected_category,
                                "subcategory": subcategory,
                                "region": normalize_location(region),
                                "regions": sorted(target_regions),
                                "confidence": confidence,
                                "source": "delivery",
                            },
                            "category": detected_category,
                            "region": normalize_location(region),
                            "confidence": confidence,
                        },
                    },
                }
            )
            continue
        
        # Send message with the constructed buttons
        try:
            sent_message = await bot_client.send_message(
                uid,
                msg,
                parse_mode="HTML",
                link_preview=False,
                buttons=buttons  # ← Используем кнопки с feedback
            )
            await feedback_manager.store_lead_sent(
                message_id=feedback_message_id,
                user_id=str(uid),
                message_text=text,
                ai_classification={
                    "category": detected_category,
                    "subcategory": subcategory,
                    "region": normalize_location(region),
                    "regions": sorted(target_regions),
                    "confidence": confidence,
                    "source": "delivery",
                    "telegram_message_id": getattr(sent_message, "id", None),
                },
                category=detected_category,
                region=normalize_location(region),
                confidence=confidence,
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

    if use_outbox:
        if queue_id is not None:
            queued_uids = await message_queue.persist_routing_and_complete_queue(
                queue_id=queue_id,
                queue_lease_token=queue_lease_token,
                event_id=event_id,
                entries=outbox_entries,
            )
        else:
            queued_uids = await message_queue.persist_routing_outbox(
                event_id,
                outbox_entries,
            )
        return DeliveryResult(mode="outbox", queued_uids=queued_uids)

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

    return DeliveryResult(
        mode="legacy",
        delivered_uids=sent_uids,
        failed_uids=failed_uids,
    )
