# Импортируем напрямую из config.py
from config import (
    bot_client,
    subscriptions,
    ADMIN_ID,
    save_subscriptions,
    categories,
    CANONICAL_LOCATIONS,
    logger,
    parse_iso_datetime,
)
from feedback_manager import feedback_manager

def has_subcats(cat: str) -> bool:
    """Возвращает True, если у категории есть подкатегории в categories.json"""
    return bool(categories.get(cat, {}).get('subcategories'))

from datetime import datetime, timedelta, timezone
from telethon import events, Button
ISTANBUL_TZ = timezone(timedelta(hours=3))
from telethon.errors.rpcerrorlist import MessageNotModifiedError

import time

_last_start_ts = {}

# Constants
ITEMS_PER_PAGE = 8
TRIAL_DAYS = 2
START_COOLDOWN = 10

# UI Text Constants
UI_TEXTS = {
    'welcome': '👋 Добро пожаловать!',
    'settings': '⚙️ Настройки',
    'subscription': '💎 Pro',
    'help': '❓ Помощь',
    'test': '📝 Тест',
    'reset': '🔄 Сброс',
    'back': '◀️ Назад',
    'close': '❌ Закрыть',
    'categories': '📂 Категории',
    'locations': '📍 Локации'
}

# Helper: Build a toggle menu (checkbox list with Back/Close), with pagination
def build_toggle_menu(title: str, items: list, selected: list,
                     prefix: str, back_key: bytes,
                     page: int = 0):
    """
    Build a toggle menu: each item with a checkbox, plus Back and Close buttons, paginated.
    """
    total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = items[start:end]
    buttons = []
    for it in page_items:
        mark = '✅ ' if it in selected else ''
        buttons.append([Button.inline(f"{mark}{it}", f"{prefix}:{it}")])
    # Pagination navigation
    nav_row = []
    if page > 0:
        nav_row.append(Button.inline('⬅️', f'{prefix}_page:{page-1}'))
    if page < total_pages - 1:
        nav_row.append(Button.inline('➡️', f'{prefix}_page:{page+1}'))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([Button.inline('◀️ Назад', back_key)])
    return title, buttons

# Helper: Safe edit wrapper to ignore MessageNotModifiedError
async def safe_edit(event, *args, **kwargs):
    """Wrapper for event.edit to ignore MessageNotModifiedError when content is unchanged."""
    try:
        await event.edit(*args, **kwargs)
    except MessageNotModifiedError:
        pass

@bot_client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    uid = str(event.sender_id)
    now = time.time()
    if now - _last_start_ts.get(uid, 0) < START_COOLDOWN:
        return  # игнор повторов
    _last_start_ts[uid] = now
    # Use subcats in prefs defaults
    prefs = subscriptions.get(uid, {'categories': [], 'locations': [], 'subcats': {}})
    cats = prefs.get('categories', [])
    locs = prefs.get('locations', [])
    # Compact status display
    filters_status = f"📊 {len(cats)}к•{len(locs)}л" if cats or locs else "📊 Настроить"
    # Compact subscription status
    trial = prefs.get('trial_start')
    sub_end = prefs.get('subscription_end')
    if sub_end:
        end_dt = parse_iso_datetime(sub_end)
        end = end_dt.astimezone(ISTANBUL_TZ) if end_dt else datetime.now(ISTANBUL_TZ)
        subscription_status = f"🛡 до {end.strftime('%d.%m')}"
        subscription_button = "💎 Pro активен"
    elif trial:
        start = parse_iso_datetime(trial) or datetime.now(timezone.utc)
        end_dt = start + timedelta(days=TRIAL_DAYS)
        end = end_dt.astimezone(ISTANBUL_TZ)
        subscription_status = f"🎁 до {end.strftime('%d.%m %H:%M')}"
        subscription_button = "💎 Pro"
    else:
        subscription_status = "💎 Активировать Pro"
        subscription_button = "💎 Pro"
    
    full_header = f"{UI_TEXTS['welcome']}\n{filters_status} | {subscription_status}"
    await event.reply(
        full_header,
        buttons=[
            [Button.inline(f"{UI_TEXTS['settings']} {filters_status}", b'menu:settings'),
             Button.inline(subscription_button, b'menu:subscribe')],
            [Button.inline(UI_TEXTS['test'], b'menu:sample'),
             Button.inline(UI_TEXTS['help'], b'menu:faq')],
            [Button.inline(UI_TEXTS['close'], b'menu:close')]
        ]
    )

@bot_client.on(events.CallbackQuery)
async def callback(event):
    try:
        data = event.data.decode()
        uid = str(event.sender_id)
        
        # Skip if this is an admin review callback (handled by review_handler)
        if data.startswith(('ap:', 'rj:')):
            return
            
        # Handle feedback callbacks
        if data.startswith('feedback:'):
            # Format: feedback:message_id:useful/not_useful
            try:
                _, message_id, feedback_type = data.split(':', 2)
                success = await feedback_manager.record_feedback(message_id, feedback_type)
                
                if success:
                    if feedback_type == 'useful':
                        await event.answer('✅ Спасибо! Отметили как полезный лид', alert=True)
                    else:
                        await event.answer('✅ Спасибо за обратную связь!', alert=True)
                    
                    # Remove feedback buttons after user responds
                    try:
                        await event.edit(event.message.text, buttons=None)
                    except Exception:
                        pass  # If edit fails, just continue
                        
                else:
                    await event.answer('❌ Ошибка при сохранении отзыва', alert=True)
                    
            except Exception as e:
                logger.error(f"Error handling feedback: {e}")
                await event.answer('❌ Ошибка при обработке отзыва', alert=True)
            return
            
        # Initialize or retrieve user preferences, ensuring keys exist
        prefs = subscriptions.setdefault(uid, {'categories': [], 'locations': [], 'subcats': {}})
        # Ensure 'subcats' key exists even for старые записи
        prefs.setdefault('subcats', {})
        
        # Helper function for compact status
        def get_compact_status():
            cats = prefs.get('categories', [])
            locs = prefs.get('locations', [])
            filters_status = f"📊 {len(cats)}к•{len(locs)}л" if cats or locs else "📊 Настроить"
            
            trial = prefs.get('trial_start')
            sub_end = prefs.get('subscription_end')
            if sub_end:
                end_dt = parse_iso_datetime(sub_end)
                end = end_dt.astimezone(ISTANBUL_TZ) if end_dt else datetime.now(ISTANBUL_TZ)
                subscription_status = f"🛡 до {end.strftime('%d.%m')}"
            elif trial:
                start = parse_iso_datetime(trial) or datetime.now(timezone.utc)
                end_dt = start + timedelta(days=TRIAL_DAYS)
                end = end_dt.astimezone(ISTANBUL_TZ)
                subscription_status = f"🎁 до {end.strftime('%d.%m %H:%M')}"
            else:
                subscription_status = "💎 Активировать Pro"
            
            return filters_status, subscription_status

        # Pagination for categories
        if data.startswith('cat_page:'):
            page = int(data.split(':',1)[1])
            title, buttons = build_toggle_menu(
                'Категории (✅ = выбрано)',
                list(categories.keys()),
                prefs['categories'],
                'cat',
                b'menu:settings',
                page=page
            )
            await safe_edit(event, title, buttons=buttons)
            return

        # Pagination for locations
        elif data.startswith('loc_page:'):
            page = int(data.split(':',1)[1])
            title, buttons = build_toggle_menu(
                'Локации (✅ = выбрано)',
                CANONICAL_LOCATIONS,
                prefs['locations'],
                'loc',
                b'menu:settings',
                page=page
            )
            await safe_edit(event, title, buttons=buttons)
            return

        # Toggle individual subcategory (format subcat:<cat>:<sub>)
        elif data.startswith('subcat:') and '_page:' not in data and data.count(':') == 2:
            _, cat, sub = data.split(':', 2)
            selected = prefs['subcats'].setdefault(cat, [])
            if sub in selected:
                selected.remove(sub)
            else:
                selected.append(sub)

            # Синхронизируем основную категорию: добавляем, если есть хоть одна подкатегория
            if selected and cat not in prefs['categories']:
                prefs['categories'].append(cat)
            # Если все подкатегории сняты — убираем категорию из списка
            if not selected and cat in prefs['categories']:
                prefs['categories'].remove(cat)

            subscriptions[uid] = prefs
            save_subscriptions()
            # Refresh subcategory menu (stay on page 0)
            subcats = list(categories[cat]['subcategories'].keys())
            title, buttons = build_toggle_menu(
                f'Подкатегории «{cat}» (✅ = выбрано)',
                subcats,
                selected,
                f'subcat:{cat}',
                b'menu:settings',
                page=0
            )
            await safe_edit(event, title, buttons=buttons)
            return
        # Main menu callback
        elif data == 'menu:main':
            filters_status, subscription_status = get_compact_status()
            
            # Determine subscription button text
            sub_end = prefs.get('subscription_end')
            subscription_button = "💎 Pro активен" if sub_end else "💎 Pro"
            
            full_header = f"{UI_TEXTS['welcome']}\n{filters_status} | {subscription_status}"
            await safe_edit(
                event,
                full_header,
                buttons=[
                    [Button.inline(f"{UI_TEXTS['settings']} {filters_status}", b'menu:settings'),
                     Button.inline(subscription_button, b'menu:subscribe')],
                    [Button.inline(UI_TEXTS['test'], b'menu:sample'),
                     Button.inline(UI_TEXTS['help'], b'menu:faq')],
                    [Button.inline(UI_TEXTS['close'], b'menu:close')]
                ]
            )

        # Settings submenu
        elif data == 'menu:settings':
            # Show filter counts in buttons
            cat_count = len([c for c in categories.keys() if c in prefs['categories'] or prefs['subcats'].get(c)])
            loc_count = len(prefs['locations'])
            
            await safe_edit(
                event,
                '⚙️ Настройки фильтров:',
                buttons=[
                    [Button.inline(f"{UI_TEXTS['categories']} ({cat_count})", b'menu:categories'),
                     Button.inline(f"{UI_TEXTS['locations']} ({loc_count})", b'menu:locations')],
                    [Button.inline('Мои фильтры', b'menu:my_filters'),
                     Button.inline(UI_TEXTS['reset'], b'menu:reset_confirm')],
                    [Button.inline(UI_TEXTS['back'], b'menu:main'),
                     Button.inline(UI_TEXTS['close'], b'menu:close')]
                ]
            )
            # Start trial for new users
            if 'trial_start' not in prefs:
                prefs['trial_start'] = datetime.now(timezone.utc).isoformat()
                subscriptions[uid] = prefs
                save_subscriptions()
                await event.answer(f'🎁 Активирован {TRIAL_DAYS}-дневный пробный период!', alert=True)

        # Categories submenu
        elif data == 'menu:categories':
            selected_cats = [
                cat for cat in categories.keys()
                if cat in prefs['categories'] or prefs['subcats'].get(cat)
            ]
            title, buttons = build_toggle_menu(
                'Категории (✅ = выбрано)',
                list(categories.keys()),
                selected_cats,
                'cat',
                b'menu:settings',
                page=0
            )
            await safe_edit(event, title, buttons=buttons)

        # Toggle category (with subcat opening)
        elif data.startswith('cat:'):
            cat = data.split(':', 1)[1]
            # Если у категории есть подкатегории, открываем их меню
            if has_subcats(cat):
                subcats = list(categories[cat]['subcategories'].keys())
                selected = prefs['subcats'].get(cat, [])
                title, buttons = build_toggle_menu(
                    f'Подкатегории «{cat}» (✅ = выбрано)',
                    subcats,
                    selected,
                    f'subcat:{cat}',
                    b'menu:settings',
                    page=0
                )
                await safe_edit(event, title, buttons=buttons)
                return
            # Обычный toggle для всех категорий
            if cat in prefs['categories']:
                prefs['categories'].remove(cat)
            else:
                prefs['categories'].append(cat)
            subscriptions[uid] = prefs
            save_subscriptions()
            # Refresh categories submenu (stay on page 0)
            selected_cats = [
                c for c in categories.keys()
                if c in prefs['categories'] or prefs['subcats'].get(c)
            ]
            title, buttons = build_toggle_menu(
                'Категории (✅ = выбрано)',
                list(categories.keys()),
                selected_cats,
                'cat',
                b'menu:settings',
                page=0
            )
            await safe_edit(event, title, buttons=buttons)
            return

        # Locations submenu
        elif data == 'menu:locations':
            title, buttons = build_toggle_menu(
                'Локации (✅ = выбрано)',
                CANONICAL_LOCATIONS,
                prefs['locations'],
                'loc',
                b'menu:settings',
                page=0
            )
            await safe_edit(event, title, buttons=buttons)

        # Toggle location
        elif data.startswith('loc:'):
            loc = data.split(':', 1)[1]
            # loc is now canonical display name
            if loc in prefs['locations']:
                prefs['locations'].remove(loc)
            else:
                prefs['locations'].append(loc)
            subscriptions[uid] = prefs
            save_subscriptions()
            # Refresh locations submenu, default to page 0
            title, buttons = build_toggle_menu(
                'Локации (✅ = выбрано)',
                CANONICAL_LOCATIONS,
                prefs['locations'],
                'loc',
                b'menu:settings',
                page=0
            )
            await safe_edit(event, title, buttons=buttons)
            return

        # Close menu
        elif data == 'menu:close':
            await event.delete()

        # Show current filters
        elif data == 'menu:my_filters':
            cats = prefs.get('categories', [])
            locs = prefs.get('locations', [])
            subcats = prefs.get('subcats', {})
            
            # Build detailed filter display
            filter_lines = []
            if cats:
                filter_lines.append(f"📂 Категории ({len(cats)}): {', '.join(cats)}")
            if subcats:
                for cat, subs in subcats.items():
                    if subs:
                        filter_lines.append(f"   └ {cat}: {', '.join(subs)}")
            if locs:
                filter_lines.append(f"📍 Локации ({len(locs)}): {', '.join(locs)}")
            
            if not filter_lines:
                text = "📋 Фильтры не настроены\n\nНастройте категории и локации для получения релевантных лидов."
            else:
                text = "📋 Активные фильтры:\n\n" + "\n".join(filter_lines)
            
            await safe_edit(
                event, 
                text, 
                buttons=[
                    [Button.inline(UI_TEXTS['back'], b'menu:settings')]
                ]
            )

        # Reset confirmation
        elif data == 'menu:reset_confirm':
            cats_count = len(prefs.get('categories', []))
            locs_count = len(prefs.get('locations', []))
            subcats_count = sum(len(subs) for subs in prefs.get('subcats', {}).values())
            total_filters = cats_count + locs_count + subcats_count
            
            if total_filters == 0:
                await event.answer('❌ Нет фильтров для сброса', alert=True)
                return
                
            await safe_edit(
                event,
                f'🔄 Сбросить все фильтры?\n({total_filters} активных)',
                buttons=[
                    [Button.inline('✅ Да, сбросить', b'menu:reset_do'),
                     Button.inline('❌ Отмена', b'menu:main')]
                ]
            )

        # Execute reset
        elif data == 'menu:reset_do':
            # Clear user's filters
            prefs['categories'] = []
            prefs['locations'] = []
            prefs['subcats'] = {}
            subscriptions[uid] = prefs
            save_subscriptions()
            
            await event.answer('🔄 Все фильтры сброшены', alert=True)
            
            # Return to main menu
            filters_status, subscription_status = get_compact_status()
            sub_end = prefs.get('subscription_end')
            subscription_button = "💎 Pro активен" if sub_end else "💎 Pro"
            
            full_header = f"{UI_TEXTS['welcome']}\n{filters_status} | {subscription_status}"
            await safe_edit(
                event,
                full_header,
                buttons=[
                    [Button.inline(f"{UI_TEXTS['settings']} {filters_status}", b'menu:settings'),
                     Button.inline(subscription_button, b'menu:subscribe')],
                    [Button.inline(UI_TEXTS['test'], b'menu:sample'),
                     Button.inline(UI_TEXTS['help'], b'menu:faq')],
                    [Button.inline(UI_TEXTS['close'], b'menu:close')]
                ]
            )

        # Show plan info
        elif data == 'menu:plan':
            ts = prefs.get('trial_start')
            if ts:
                start = parse_iso_datetime(ts) or datetime.now(timezone.utc)
                end_dt = start + timedelta(days=2)
                end_local = end_dt.astimezone(ISTANBUL_TZ)
                end_str = end_local.strftime('%Y-%m-%d %H:%M (UTC+3)')
                trial_text = f"Пробный период до: {end_str}"
            else:
                trial_text = "Пробный период не активирован"
            text = (
                "🏷 Ваш тариф: Free\n"
                f"{trial_text}\n\n"
                "Чтобы перейти на Pro, нажмите кнопку «Подписка»"
            )
            await safe_edit(
                event,
                text,
                buttons=[[Button.inline('💳 Подписаться', b'menu:subscribe')], [Button.inline('◀️ Назад', b'menu:settings')]]
            )

        # Send a sample lead with close button
        elif data == 'menu:sample':
            example = (
                "📝 Пример лида:\n\n"
                "🗨 ТурыАнталия | @ivan_tourist\n"
                "— Ищу гида на завтра в Кемер, группа 4 человека. "
                "Бюджет до 100$. Кто может помочь?\n\n"
                "#кемер #экскурсии #гид"
            )
            await safe_edit(
                event,
                example,
                buttons=[[Button.inline(UI_TEXTS['back'], b'menu:main')]]
            )

        # FAQ / Help with structured info
        elif data == 'menu:faq':
            faq_text = (
                "❓ Справка\n\n"
                "🔧 Настройка: ⚙️ Настройки → выберите категории и локации\n\n"
                "⏰ Пробный период: 2 дня бесплатно\n\n"
                "💎 Pro подписка:\n"
                "• 1 мес — 20 USD\n"
                "• 3 мес — 54 USD\n"
                "• 6 мес — 96 USD\n\n"
                "💳 Оплата: через кнопку '💎 Pro' → '✅ Я оплатил'\n\n"
                "📞 Поддержка: @support_bot"
            )
            await safe_edit(
                event,
                faq_text,
                buttons=[[Button.inline(UI_TEXTS['back'], b'menu:main')]]
            )

        # Show subscription prices and manual payment details merged
        elif data == 'menu:subscribe':
            # Check current status
            sub_end = prefs.get('subscription_end')
            if sub_end:
                end_dt = parse_iso_datetime(sub_end)
                end = end_dt.astimezone(ISTANBUL_TZ) if end_dt else datetime.now(ISTANBUL_TZ)
                text = (
                    f"💎 Pro активна до {end.strftime('%d.%m.%Y %H:%M')}\n\n"
                    "🔄 Продлить подписку:\n"
                    "• 1 мес — 20 USD\n"
                    "• 3 мес — 54 USD\n"
                    "• 6 мес — 96 USD"
                )
            else:
                text = (
                    "💎 Pro подписка\n\n"
                    "📦 Тарифы:\n"
                    "• 1 мес — 20 USD\n"
                    "• 3 мес — 54 USD (-10%)\n"
                    "• 6 мес — 96 USD (-20%)\n\n"
                    "💳 Оплата: карта, Bitcoin, Ethereum"
                )
            
            await safe_edit(
                event,
                text,
                buttons=[
                    [Button.inline('💳 Реквизиты', b'menu:payment_details')],
                    [Button.inline('✅ Я оплатил', b'menu:paid')],
                    [Button.inline(UI_TEXTS['back'], b'menu:main')]
                ]
            )

        # Payment details
        elif data == 'menu:payment_details':
            text = (
                "💳 Реквизиты для оплаты:\n\n"
                "💵 Карта:\n1234 5678 9012 3456\n(Иван Иванов)\n\n"
                "₿ Bitcoin:\n1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\n\n"
                "Ξ Ethereum:\n0xAbC1234Ef567890BcDEF1234567890AbCdEF1234\n\n"
                "📸 После оплаты нажмите '✅ Я оплатил' и прикрепите скриншот."
            )
            await safe_edit(
                event,
                text,
                buttons=[
                    [Button.inline('✅ Я оплатил', b'menu:paid')],
                    [Button.inline(UI_TEXTS['back'], b'menu:subscribe')]
                ]
            )

        elif data == 'menu:paid':
            # Notify admin that user pressed “Я оплатил” with their filters
            prefs = subscriptions.get(uid, {})
            cats = prefs.get('categories', [])
            locs = prefs.get('locations', [])
            # Try to get username, otherwise show ID
            try:
                user_entity = await bot_client.get_entity(int(uid))
                uname = user_entity.username or user_entity.first_name or uid
            except:
                uname = uid
            info = (
                f"📢 Пользователь @{uname} (ID={uid}) нажал «Я оплатил» и готов к проверке.\n"
                f"Категории: {', '.join(cats) if cats else '—'}\n"
                f"Локации: {', '.join(locs) if locs else '—'}"
            )
            # Send info to admin
            await bot_client.send_message(ADMIN_ID, info)
            # Prompt user to attach screenshot
            await event.answer("✅ Отлично! Теперь прикрепите скрин оплаты.", alert=True)
            # Mark that we're awaiting payment screenshot from this user
            prefs = subscriptions.get(uid, {})
            prefs['awaiting_screenshot'] = True
            subscriptions[uid] = prefs
            save_subscriptions()

        # Admin approval callbacks
        elif data.startswith('approve:'):
            # Only admin can approve
            if event.sender_id != ADMIN_ID:
                await event.answer("❌ У вас нет прав на это действие", alert=True)
                return
            _, uid_str, months_str = data.split(':')
            months = int(months_str)
            uid = uid_str
            end = datetime.now(timezone.utc) + timedelta(days=30 * months)
            end_local = end.astimezone(ISTANBUL_TZ)
            prefs = subscriptions.setdefault(uid, {})
            prefs['subscription_end'] = end.isoformat()
            # Clear trial flags
            prefs.pop('trial_start', None)
            prefs.pop('trial_expired_notified', None)
            prefs.pop('paid_expired_notified', None)
            prefs.pop('awaiting_screenshot', None)
            # Save updated subscriptions
            save_subscriptions()
            # Notify admin and user with local time
            end_str = end_local.strftime('%d.%m.%Y %H:%M')
            await safe_edit(event, f"✅ Подписка пользователя {uid} активирована до {end_str}")
            await bot_client.send_message(
                int(uid),
                f"🎉 Ваша 💎 Pro подписка активирована до {end_str}!"
            )

        elif data.startswith('reject:'):
            # Only admin can reject
            if event.sender_id != ADMIN_ID:
                await event.answer("❌ У вас нет прав на это действие", alert=True)
                return
            _, uid_str = data.split(':')
            uid = uid_str
            # Clear awaiting screenshot flag
            prefs = subscriptions.get(uid, {})
            prefs.pop('awaiting_screenshot', None)
            subscriptions[uid] = prefs
            save_subscriptions()
            
            await safe_edit(event, f"❌ Подписка для пользователя {uid} отменена")
            await bot_client.send_message(
                int(uid),
                "❌ К сожалению, оплата не была подтверждена. Попробуйте ещё раз или свяжитесь с поддержкой."
            )

        else:
            # Unknown callback - acknowledge to prevent timeout
            await event.answer("⚠️ Неизвестная команда", alert=False)
            
    except MessageNotModifiedError:
        # Ignore if content is the same
        pass
    except Exception as e:
        # Log error and notify user
        logger.error(f"Callback error for {uid}: {e}")
        try:
            await event.answer("❌ Произошла ошибка. Попробуйте ещё раз.", alert=True)
        except Exception:
            pass  # If we can't even send error message, just pass
    
@bot_client.on(events.NewMessage(func=lambda e: e.is_private and (e.photo or e.document)))
async def handle_payment_screenshot(event):
    """Handle payment screenshot uploads"""
    try:
        user_id = str(event.sender_id)
        prefs = subscriptions.get(user_id, {})
        
        # Only handle screenshot if user has pressed "Я оплатил"
        if not prefs.get('awaiting_screenshot'):
            return
        
        # Clear the flag so future photos won't trigger
        prefs.pop('awaiting_screenshot', None)
        subscriptions[user_id] = prefs
        save_subscriptions()
        
        # Get user info for admin
        try:
            user_entity = await bot_client.get_entity(int(user_id))
            uname = user_entity.username or user_entity.first_name or user_id
        except Exception:
            uname = user_id
        
        # Notify admin via Telethon
        await bot_client.send_message(
            ADMIN_ID,
            f"📸 Получен скрин оплаты от @{uname} (ID: {user_id})"
        )
        
        # Forward media via Telethon
        await bot_client.forward_messages(
            ADMIN_ID,
            event.id,
            event.chat_id
        )
        
        # Present admin with subscription approval options
        await bot_client.send_message(
            ADMIN_ID,
            f"Выберите период подписки для пользователя {user_id}:",
            buttons=[
                [Button.inline("1 мес.", f"approve:{user_id}:1"),
                 Button.inline("3 мес.", f"approve:{user_id}:3")],
                [Button.inline("6 мес.", f"approve:{user_id}:6")],
                [Button.inline("❌ Отказать", f"reject:{user_id}")]
            ]
        )
        
        # Acknowledge user
        await event.reply("✅ Спасибо! Получили ваш скриншот оплаты. Как только проверим — активируем подписку.")
        
    except Exception as e:
        logger.error(f"Payment screenshot handling error: {e}")
        try:
            await event.reply("❌ Произошла ошибка при обработке скриншота. Обратитесь в поддержку.")
        except Exception:
            pass
