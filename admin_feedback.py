#!/usr/bin/env python3
"""
Admin Commands for Feedback Management
Provides admin interface for monitoring feedback and AI training
"""

import asyncio
from datetime import datetime
from telethon import events, Button
from config import bot_client, ADMIN_ID, logger
from feedback_manager import feedback_manager
from ai_trainer import ai_trainer
from fine_tuning_simple import fine_tuning_manager

@bot_client.on(events.NewMessage(pattern='/feedback_stats'))
async def cmd_feedback_stats(event):
    """Show feedback statistics (admin only)"""
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ Доступ запрещен")
        return
    
    try:
        # Initialize feedback database
        await feedback_manager.init_db()
        
        # Get training stats
        stats = await ai_trainer.get_training_stats()
        
        # Get feedback stats
        feedback_stats = await feedback_manager.get_feedback_stats()
        
        # Format statistics message
        total_feedback = stats.get('total_feedback', 0)
        useful_rate = stats.get('useful_leads_rate', 0)
        recent_feedback = stats.get('recent_feedback_7d', 0)
        training_examples = stats.get('training_examples', 0)
        ai_improvement = stats.get('ai_improvement_enabled', False)
        
        # Админская статистика
        admin_decisions = stats.get('admin_decisions', 0)
        admin_approved = stats.get('admin_approved', 0)
        admin_rejected = stats.get('admin_rejected', 0)
        
        message = f"""📊 **СТАТИСТИКА ОБРАТНОЙ СВЯЗИ**

🔢 **Общие показатели:**
• Всего отзывов: {total_feedback}
• Полезных лидов: {feedback_stats.get('useful_count', 0)}
• Неполезных лидов: {feedback_stats.get('not_useful_count', 0)}
• Качество лидов: {useful_rate}%

👨‍💼 **Решения администратора:**
• Всего решений: {admin_decisions}
• Одобрено: {admin_approved}
• Отклонено: {admin_rejected}

📈 **Активность:**
• Отзывов за 7 дней: {recent_feedback}
• Примеров для обучения: {training_examples}

🤖 **Улучшение ИИ:**
• Статус: {'✅ Активно' if ai_improvement else '❌ Недостаточно данных'}
• Примеров в промптах: {training_examples}

💡 **Рекомендации:**
"""
        
        if total_feedback < 50:
            message += "• Нужно больше отзывов для улучшения ИИ\n"
        elif useful_rate < 70:
            message += "• Качество лидов низкое - нужна настройка ИИ\n"
        elif useful_rate > 85:
            message += "• Отличное качество лидов! ✅\n"
        if admin_decisions >= 10:
            message += "• Ваши админские решения активно используются для обучения ИИ! 🎯\n"
        
        buttons = [
            [Button.inline("🔄 Обновить", "admin_feedback_refresh"),
             Button.inline("📤 Экспорт данных", "admin_feedback_export")],
            [Button.inline("📚 Мигрировать feedback.log", "admin_migrate_feedback"),
             Button.inline("🧠 Проверить fine-tuning", "admin_check_finetuning")],
            [Button.inline("🚀 Запустить fine-tuning", "admin_start_finetuning"),
             Button.inline("❌ Закрыть", "admin_close")]
        ]
        
        await event.reply(message, buttons=buttons, parse_mode='markdown')
        
    except Exception as e:
        logger.error(f"Error showing feedback stats: {e}")
        await event.reply(f"❌ Ошибка: {e}")

@bot_client.on(events.CallbackQuery(pattern=b'admin_feedback_refresh'))
async def callback_feedback_refresh(event):
    """Refresh feedback statistics"""
    if event.sender_id != ADMIN_ID:
        return
    
    # Trigger the stats command
    await cmd_feedback_stats(event)

@bot_client.on(events.CallbackQuery(pattern=b'admin_feedback_export'))
async def callback_feedback_export(event):
    """Export training data"""
    if event.sender_id != ADMIN_ID:
        return
    
    try:
        count = await ai_trainer.export_training_data()
        await event.answer(f"✅ Экспортировано {count} примеров в ai_training_data.jsonl", alert=True)
    except Exception as e:
        logger.error(f"Error exporting training data: {e}")
        await event.answer(f"❌ Ошибка экспорта: {e}", alert=True)

@bot_client.on(events.CallbackQuery(pattern=b'admin_migrate_feedback'))
async def callback_migrate_feedback(event):
    """Мигрирует feedback.log в базу данных"""
    if event.sender_id != ADMIN_ID:
        return
    
    try:
        await event.answer("🔄 Запуск миграции feedback.log...", alert=True)
        
        from review_handler import migrate_feedback_log_to_db
        migrated_count = await migrate_feedback_log_to_db()
        
        if migrated_count > 0:
            await bot_client.send_message(
                ADMIN_ID, 
                f"✅ Миграция завершена!\n📚 Мигрировано {migrated_count} админских решений в базу данных для обучения ИИ."
            )
        else:
            await bot_client.send_message(
                ADMIN_ID,
                "ℹ️ Миграция завершена, новых записей не найдено."
            )
        
    except Exception as e:
        logger.error(f"Error migrating feedback: {e}")
        await event.answer(f"❌ Ошибка миграции: {e}", alert=True)

@bot_client.on(events.CallbackQuery(pattern=b'admin_check_finetuning'))
async def callback_check_finetuning(event):
    """Check fine-tuning readiness"""
    if event.sender_id != ADMIN_ID:
        return
    
    try:
        readiness = await fine_tuning_manager.can_start_fine_tuning()
        
        if readiness['ready']:
            message = f"""✅ ГОТОВО К FINE-TUNING!

📊 Данные:
• Всего примеров: {readiness['total_examples']}
• Положительных: {readiness['positive_examples']}
• Отрицательных: {readiness['negative_examples']}

💡 {readiness['recommendation']}"""
        else:
            message = f"""⚠️ НЕ ГОТОВО К FINE-TUNING

📊 Текущие данные:
• Всего примеров: {readiness.get('total_examples', 0)}
• Положительных: {readiness.get('positive_examples', 0)}
• Отрицательных: {readiness.get('negative_examples', 0)}
• Нужно минимум: {readiness.get('min_required', 100)}

💡 {readiness.get('recommendation', 'Продолжайте собирать данные')}"""
        
        await event.answer(message, alert=True)
        
    except Exception as e:
        logger.error(f"Error checking fine-tuning readiness: {e}")
        await event.answer(f"❌ Ошибка: {e}", alert=True)

@bot_client.on(events.CallbackQuery(pattern=b'admin_start_finetuning'))
async def callback_start_finetuning(event):
    """Start fine-tuning process"""
    if event.sender_id != ADMIN_ID:
        return
    
    try:
        await event.answer("🚀 Запуск fine-tuning... Это может занять время.", alert=True)
        
        result = await fine_tuning_manager.full_fine_tuning_process()
        
        if result['success']:
            message = f"""✅ FINE-TUNING ЗАПУЩЕН!

Job ID: {result['job_id']}
Примеров: {result['examples_count']}

Процесс может занять 10-60 минут.
Используйте /check_finetuning {result['job_id']} для проверки статуса."""
        else:
            message = f"❌ Ошибка запуска fine-tuning:\n{result['message']}"
        
        await bot_client.send_message(ADMIN_ID, message)
        
    except Exception as e:
        logger.error(f"Error starting fine-tuning: {e}")
        await event.answer(f"❌ Ошибка: {e}", alert=True)

@bot_client.on(events.CallbackQuery(pattern=b'admin_close'))
async def callback_admin_close(event):
    """Close admin menu"""
    if event.sender_id != ADMIN_ID:
        return
    
    await event.delete()

@bot_client.on(events.NewMessage(pattern='/ai_quality'))
async def cmd_ai_quality(event):
    """Show AI quality metrics (admin only)"""
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ Доступ запрещен")
        return
    
    try:
        # Get recent feedback for quality analysis
        recent_feedback = await feedback_manager.get_recent_feedback(50)
        
        if not recent_feedback:
            await event.reply("📊 Нет данных обратной связи для анализа качества ИИ")
            return
        
        # Analyze feedback by confidence levels
        high_conf_useful = 0
        high_conf_not_useful = 0
        low_conf_useful = 0
        low_conf_not_useful = 0
        
        for item in recent_feedback:
            confidence = item.get('confidence', 0.5)
            feedback = item['user_feedback']
            
            if confidence >= 0.8:
                if feedback == 'useful':
                    high_conf_useful += 1
                else:
                    high_conf_not_useful += 1
            else:
                if feedback == 'useful':
                    low_conf_useful += 1
                else:
                    low_conf_not_useful += 1
        
        total_high = high_conf_useful + high_conf_not_useful
        total_low = low_conf_useful + low_conf_not_useful
        
        high_accuracy = (high_conf_useful / max(1, total_high)) * 100
        low_accuracy = (low_conf_useful / max(1, total_low)) * 100
        
        message = f"""🎯 **КАЧЕСТВО КЛАССИФИКАЦИИ ИИ**

📈 **Высокая уверенность (≥80%):**
• Всего: {total_high}
• Точность: {high_accuracy:.1f}%
• Полезных: {high_conf_useful}
• Неполезных: {high_conf_not_useful}

📉 **Низкая уверенность (<80%):**
• Всего: {total_low}
• Точность: {low_accuracy:.1f}%
• Полезных: {low_conf_useful}
• Неполезных: {low_conf_not_useful}

💡 **Анализ:**
"""
        
        if high_accuracy > 85:
            message += "✅ ИИ хорошо классифицирует уверенные случаи\n"
        else:
            message += "⚠️ ИИ ошибается даже в уверенных случаях\n"
            
        if low_accuracy < 50:
            message += "✅ ИИ правильно сомневается в сложных случаях\n"
        else:
            message += "⚠️ ИИ слишком часто сомневается в хороших лидах\n"
        
        await event.reply(message, parse_mode='markdown')
        
    except Exception as e:
        logger.error(f"Error showing AI quality: {e}")
        await event.reply(f"❌ Ошибка: {e}")

@bot_client.on(events.NewMessage(pattern=r'/check_finetuning\s+(\S+)'))
async def cmd_check_finetuning_status(event):
    """Check fine-tuning job status (admin only)"""
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ Доступ запрещен")
        return
    
    try:
        job_id = event.pattern_match.group(1)
        status = fine_tuning_manager.check_fine_tuning_status(job_id)
        
        if 'error' in status:
            await event.reply(f"❌ Ошибка проверки статуса: {status['error']}")
            return
        
        status_emoji = {
            'validating_files': '🔍',
            'queued': '⏳',
            'running': '🏃',
            'succeeded': '✅',
            'failed': '❌',
            'cancelled': '⛔'
        }.get(status['status'], '🔄')
        
        message = f"""🧠 **FINE-TUNING STATUS**

{status_emoji} **Статус**: {status['status']}
🆔 **Job ID**: {status['id']}
📅 **Создан**: {datetime.fromtimestamp(status['created_at']).strftime('%Y-%m-%d %H:%M')}"""
        
        if status['finished_at']:
            message += f"\n✅ **Завершен**: {datetime.fromtimestamp(status['finished_at']).strftime('%Y-%m-%d %H:%M')}"
        
        if status['model']:
            message += f"\n🤖 **Модель**: `{status['model']}`"
            message += f"\n\nℹ️ Для использования новой модели добавьте в .env:\n`FINE_TUNED_MODEL={status['model']}`"
        
        await event.reply(message, parse_mode='markdown')
        
    except Exception as e:
        logger.error(f"Error checking fine-tuning status: {e}")
        await event.reply(f"❌ Ошибка: {e}")

@bot_client.on(events.NewMessage(pattern='/list_models'))
async def cmd_list_finetuned_models(event):
    """List fine-tuned models (admin only)"""
    if event.sender_id != ADMIN_ID:
        await event.reply("❌ Доступ запрещен")
        return
    
    try:
        models = fine_tuning_manager.list_fine_tuned_models()
        
        if not models:
            await event.reply("🤖 Нет доступных fine-tuned моделей")
            return
        
        message = "🤖 **FINE-TUNED МОДЕЛИ**\n\n"
        
        for model in models:
            created_date = datetime.fromtimestamp(model['created']).strftime('%Y-%m-%d')
            message += f"• `{model['id']}`\n  📅 {created_date} | 🏢 {model['owned_by']}\n\n"
        
        message += "ℹ️ Для использования модели добавьте в .env:\n`FINE_TUNED_MODEL=ft:gpt-4o-mini:...`"
        
        await event.reply(message, parse_mode='markdown')
        
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        await event.reply(f"❌ Ошибка: {e}")

logger.info("✅ Admin feedback commands with fine-tuning loaded")