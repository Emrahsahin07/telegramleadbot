# review_handler.py
import asyncio
import json
import os
import re
from datetime import datetime
from telethon import Button
from telethon import events
import hashlib
from config import bot_client, ADMIN_ID, logger, metrics
from delivery import send_lead_to_users
from feedback_manager import feedback_manager

REVIEW_FILE = "ai_review.log"
FEEDBACK_FILE = "feedback.log"

# Храним временные данные о лидах (в production — в БД)
pending_leads = {}

async def load_pending_reviews():
    """Загружает сообщения из ai_review.log"""
    reviews = []
    if not os.path.exists(REVIEW_FILE):
        return reviews
    with open(REVIEW_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    parts = line.split(" | ")
                    ts = parts[0]
                    chat_info = parts[1]
                    text = parts[2]
                    details = " | ".join(parts[3:])
                    reviews.append({
                        "timestamp": ts,
                        "chat_info": chat_info,
                        "text": text,
                        "details": details
                    })
                except Exception as e:
                    logger.error(f"Ошибка парсинга строки: {line} | {e}")
    return reviews

async def send_review_to_admin(review):
    """Отправляет сообщение на проверку админу"""
    # Extract fields from review dictionary
    region = review.get("region")
    category = review.get("category") or review.get("detected_category")
    subcategory = review.get("subcategory")
    route = review.get("route")  # tuple/list like (pickup, dest)
    confidence = review.get("confidence", 0.0)
    explanation = review.get("explanation", "")

    # Parse 'details' string if direct fields are not present
    if not category or not region or not explanation:
        details = review.get("details", "")
        if details:
            # Try to extract data from details string if direct fields are missing
            if not category and "category:" in details:
                category_match = re.search(r"category:([^,]+)", details)
                if category_match:
                    category = category_match.group(1).strip()
            if not region and "region:" in details:
                region_match = re.search(r"region:([^,]+)", details)
                if region_match:
                    region = region_match.group(1).strip()
            if not explanation and "explanation:" in details:
                explanation_match = re.search(r"explanation:([^,]+)", details)
                if explanation_match:
                    explanation = explanation_match.group(1).strip()

    # Build tags
    tags = []
    if route and any(route):
        a, b = (route + [None, None])[:2] if isinstance(route, list) else route
        if a and b:
            tags.append(f"#{str(a).lower()} → #{str(b).lower()}")
        elif a:
            tags.append(f"#{str(a).lower()}")
        elif b:
            tags.append(f"#{str(b).lower()}")
    elif region:
        tags.append(f"#{str(region).lower()}")
    if category:
        tags.append(f"#{str(category).lower()}")
    if subcategory:
        tags.append(f"#{str(subcategory).lower()}")

    tags_str = (" " + " ".join(tags)) if tags else ""

    # Format confidence as percentage
    confidence_str = f" ({confidence*100:.0f}%)" if confidence else ""
    
    # Add explanation if available
    explanation_str = f"\n\n💡 {explanation}" if explanation else ""

    msg = (
        f"⚠️ Проверь лид{confidence_str}:{tags_str}\n"
        f"{review['text']}{explanation_str}"
    )
    # Стабильный ID лида по (timestamp|chat_info|text)
    raw_key = f"{review.get('timestamp','')}|{review.get('chat_info','')}|{review.get('text','')}"
    lead_id = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]  # 16-символьный hex
    pending_leads[lead_id] = review  # Сохраняем для отправки (ключ строковый)

    # Кнопки: короткий payload <= 64 байт: ap:<id> / rj:<id>
    link = review.get("link")
    row = [
        Button.inline("✅ Отправить", data=f"ap:{lead_id}".encode()),
        Button.inline("❌ Отклонить", data=f"rj:{lead_id}".encode()),
    ]
    # Add URL button only if link looks safe/valid
    try:
        import re as _re
        if isinstance(link, str) and (
            _re.match(r"^https://t\.me/(c/\d+/\d+|[A-Za-z0-9_]+)/?\d*$", link)
            or _re.match(r"^tg://", link)
        ):
            row.append(Button.url("🔗 Сообщение", link))
    except Exception:
        pass
    buttons = [row]
    # Ensure correct bot identity before sending admin review
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
        logger.error(f"SKIP admin review send: wrong bot id={current_bot_id}, expected id={desired_bot_id}")
        return

    await bot_client.send_message(ADMIN_ID, msg, buttons=buttons)

async def handle_review_callback(event):
    """Обрабатывает нажатие кнопок"""
    data = event.data.decode()
    if data.startswith("ap:"):
        lead_id = data.split(":", 1)[1]
        lead = pending_leads.get(lead_id)
        if lead:
            # Отправляем подписчикам
            logger.info(f"✅ Админ одобрил лид: {data}")
            
            # Сохраняем админское решение в feedback.db для обучения ИИ
            await _store_admin_decision(lead, "useful", lead_id)
            # Извлекаем данные из лида
            try:
                parts = lead['chat_info'].split(" (")
                chat_id = int(parts[0])
                group_name = parts[1][:-1] if len(parts) > 1 else "Неизвестный чат"
            except:
                chat_id = 0
                group_name = "Неизвестный чат"
            # Admin-approved leads should be sent with high confidence
            sender_username = lead.get("sender_username", "")
            sender_id = lead.get("sender_id", 0)
            display_name = f"@{sender_username}" if sender_username else "Проверено админом"
            delivery_result = await send_lead_to_users(
                chat_id=chat_id,
                group_name=group_name,
                group_username=None,
                sender_name=display_name,
                sender_id=sender_id,
                sender_username=sender_username,
                text=lead['text'],
                link=lead.get("link", ""),
                region=lead.get("region"),
                regions=lead.get("regions"),
                detected_category=(lead.get("category") or lead.get("detected_category")),
                subcategory=lead.get("subcategory"),
                route=lead.get("route"),
                confidence=lead.get("confidence", 0.9),  # Admin-approved leads should have high confidence
                event_id=lead.get("event_id"),
            )
            logger.info(
                "REVIEW_DELIVERY | delivered=%s queued=%s failed=%s | lead=%s",
                len(delivery_result.delivered_uids),
                len(delivery_result.queued_uids),
                len(delivery_result.failed_uids),
                lead_id,
            )
            if delivery_result.mode == "outbox":
                metrics["leads_queued"] += 1
                await event.answer("✅ Лид одобрен и поставлен в очередь доставки!")
            else:
                if delivery_result.delivered_uids:
                    metrics["leads_sent"] += 1
                await event.answer("✅ Лид одобрен и отправлен пользователям!")
            # Удаляем сообщение
            try:
                await event.delete()
            except:
                pass
            del pending_leads[lead_id]
        else:
            await event.answer("Ошибка: лид не найден!")
    elif data.startswith("rj:"):
        lead_id = data.split(":", 1)[1]
        lead = pending_leads.get(lead_id)
        
        # Сохраняем админское решение в feedback.db для обучения ИИ
        if lead:
            await _store_admin_decision(lead, "not_useful", lead_id)
        
        # Записываем в feedback
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} | {data}\n")
        logger.info(f"❌ Админ отклонил лид: {data}")
        await event.answer("❌ Лид отклонён!")
        # Удаляем сообщение
        try:
            await event.delete()
        except:
            pass
        if lead_id in pending_leads:
            del pending_leads[lead_id]
async def _store_admin_decision(lead, feedback_type, lead_id):
    """Сохраняет админское решение в feedback.db для обучения ИИ"""
    try:
        # Инициализируем базу данных если нужно
        await feedback_manager.init_db()
        
        # Создаём уникальный message_id для админского решения
        admin_message_id = f"admin_{lead_id}_{int(datetime.now().timestamp())}"
        
        # Извлекаем данные из лида
        message_text = lead.get('text', '')
        category = lead.get('category') or lead.get('detected_category')
        region = lead.get('region')
        confidence = lead.get('confidence', 0.5)  # Лиды на проверке обычно низкой уверенности
        
        # Сохраняем в базу данных
        await feedback_manager.store_lead_sent(
            message_id=admin_message_id,
            user_id="admin",  # Специальный user_id для админских решений
            message_text=message_text,
            ai_classification={
                "category": category,
                "region": region,
                "confidence": confidence,
                "source": "admin_review"
            },
            category=category,
            region=region,
            confidence=confidence
        )
        
        # Записываем feedback
        success = await feedback_manager.record_feedback(admin_message_id, feedback_type)
        
        if success:
            logger.info(f"📚 Админское решение сохранено для обучения: {feedback_type} | {admin_message_id}")
        else:
            logger.warning(f"⚠️ Не удалось записать админское решение: {admin_message_id}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка при сохранении админского решения: {e}")

async def migrate_feedback_log_to_db():
    """Мигрирует существующие админские решения из feedback.log в feedback.db"""
    try:
        if not os.path.exists(FEEDBACK_FILE):
            logger.info("📁 feedback.log не найден, миграция не требуется")
            return 0
        
        await feedback_manager.init_db()
        migrated_count = 0
        
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("20"):  # Пропускаем пустые строки и неформатированные
                    continue
                
                try:
                    # Парсим строку: "2025-08-24 19:28:40.320397 | rj:ac0012dd7587a55c"
                    parts = line.split(" | ")
                    if len(parts) != 2:
                        continue
                    
                    timestamp_str = parts[0]
                    decision_data = parts[1]
                    
                    if decision_data.startswith("rj:"):
                        feedback_type = "not_useful"
                        lead_id = decision_data[3:]  # Убираем "rj:"
                    elif decision_data.startswith("ap:"):
                        feedback_type = "useful"
                        lead_id = decision_data[3:]  # Убираем "ap:"
                    else:
                        continue
                    
                    # Создаём уникальный message_id
                    admin_message_id = f"migrated_admin_{lead_id}_{timestamp_str.replace(' ', '_').replace(':', '-')}"
                    
                    # Проверяем, не мигрировали ли уже этот лид
                    existing = await _check_if_migrated(admin_message_id)
                    if existing:
                        continue
                    
                    # Сохраняем миграционную запись
                    await feedback_manager.store_lead_sent(
                        message_id=admin_message_id,
                        user_id="admin",
                        message_text=f"Мигрированное админское решение (ID: {lead_id})",
                        ai_classification={
                            "category": "unknown",
                            "region": "unknown", 
                            "confidence": 0.5,
                            "source": "migrated_admin_decision",
                            "original_timestamp": timestamp_str
                        },
                        category="unknown",
                        region="unknown",
                        confidence=0.5
                    )
                    
                    # Записываем feedback
                    await feedback_manager.record_feedback(admin_message_id, feedback_type)
                    migrated_count += 1
                    
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка при миграции строки: {line} | {e}")
                    continue
        
        logger.info(f"✅ Мигрировано {migrated_count} админских решений из feedback.log")
        return migrated_count
        
    except Exception as e:
        logger.error(f"❌ Ошибка при миграции feedback.log: {e}")
        return 0

async def _check_if_migrated(message_id):
    """Проверяет, был ли уже мигрирован данный message_id"""
    try:
        import aiosqlite
        async with aiosqlite.connect(feedback_manager.db_path) as db:
            cursor = await db.execute(
                "SELECT id FROM feedback WHERE message_id = ?",
                (message_id,)
            )
            result = await cursor.fetchone()
            return result is not None
    except Exception:
        return False


# Регистрация обработчика инлайн-кнопок
@bot_client.on(events.CallbackQuery(pattern=b'^(ap|rj):'))
async def _on_review_callback(event):
    await handle_review_callback(event)
