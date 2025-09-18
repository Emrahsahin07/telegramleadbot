# subscription_utils.py - Utility functions for subscription status checking
from datetime import datetime, timezone, timedelta

# Istanbul timezone
ISTANBUL_TZ = timezone(timedelta(hours=3))

def get_subscription_status(prefs: dict) -> dict:
    """
    Get subscription status for a user.
    
    Returns:
        dict with keys: status, end_date, is_active, status_text
    """
    now = datetime.now(timezone.utc)
    
    # Check paid subscription first
    sub_end = prefs.get('subscription_end')
    if sub_end:
        end_dt = datetime.fromisoformat(sub_end)
        end_local = end_dt.astimezone(ISTANBUL_TZ)
        is_active = now <= end_dt
        return {
            'status': 'paid',
            'end_date': end_dt,
            'end_local': end_local,
            'is_active': is_active,
            'status_text': f"🛡 Подписка до {end_local.strftime('%d.%m %H:%M')}" if is_active else "❌ Подписка истекла"
        }
    
    # Check trial subscription
    trial_start = prefs.get('trial_start')
    if trial_start:
        start = datetime.fromisoformat(trial_start)
        end_dt = start + timedelta(days=2)
        end_local = end_dt.astimezone(ISTANBUL_TZ)
        is_active = now <= end_dt
        return {
            'status': 'trial',
            'end_date': end_dt,
            'end_local': end_local,
            'is_active': is_active,
            'status_text': f"🎁 Пробный до {end_local.strftime('%d.%m %H:%M')}" if is_active else "❌ Пробный период истёк"
        }
    
    # No subscription
    return {
        'status': 'none',
        'end_date': None,
        'end_local': None,
        'is_active': False,
        'status_text': "🎁 Нет пробного"
    }

def is_user_active(prefs: dict) -> bool:
    """Check if user has active subscription or trial."""
    status = get_subscription_status(prefs)
    return status['is_active']

def get_subscription_type(prefs: dict) -> str:
    """Get subscription type: 'paid', 'trial', or 'none'."""
    status = get_subscription_status(prefs)
    return status['status']