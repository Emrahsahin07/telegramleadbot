from __future__ import annotations

from collections import defaultdict, deque
from types import SimpleNamespace
from typing import Any


class RecordingBot:
    """Small async Telethon substitute with per-recipient failure plans."""

    def __init__(self, failures: dict[int, list[Exception]] | None = None) -> None:
        self.calls: list[int] = []
        self._failures = defaultdict(deque)
        for uid, planned in (failures or {}).items():
            self._failures[uid].extend(planned)

    async def get_me(self) -> Any:
        return SimpleNamespace(id=999_001)

    async def send_message(self, uid: int, *_args: Any, **_kwargs: Any) -> Any:
        self.calls.append(uid)
        if self._failures[uid]:
            raise self._failures[uid].popleft()
        return SimpleNamespace(id=len(self.calls))


class ReviewEvent:
    def __init__(self, lead_id: str) -> None:
        self.data = f"ap:{lead_id}".encode()
        self.answers: list[str] = []
        self.deleted = False

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)

    async def delete(self) -> None:
        self.deleted = True


def active_prefs() -> dict[str, Any]:
    return {
        "subscription_end": "2099-01-01T00:00:00+00:00",
        "categories": ["трансфер"],
        "locations": ["Анталия"],
        "subcats": {},
    }


def expired_trial_prefs() -> dict[str, Any]:
    return {
        "trial_start": "2000-01-01T00:00:00+00:00",
        "categories": ["трансфер"],
        "locations": ["Анталия"],
        "subcats": {},
    }


def lead_kwargs() -> dict[str, Any]:
    return {
        "chat_id": -100123,
        "group_name": "Test group",
        "group_username": "test_group",
        "sender_name": "Sender",
        "sender_id": 777,
        "sender_username": "sender",
        "text": "Нужен трансфер в Анталии",
        "link": "https://t.me/test_group/10",
        "region": "Анталия",
        "regions": ["Анталия"],
        "detected_category": "трансфер",
        "subcategory": None,
        "route": None,
        "confidence": 0.9,
    }


async def configure_temp_queue(monkeypatch, tmp_path) -> str:
    import message_queue
    from db_lock_resolver import SafeDatabaseManager

    db_path = str(tmp_path / "queue.db")
    monkeypatch.setattr(message_queue, "DB_PATH", db_path)
    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))
    monkeypatch.setattr(message_queue, "MAX_QUEUE_SIZE", 100)
    await message_queue.init_db()
    return db_path


async def create_queue_event() -> str:
    import message_queue

    assert await message_queue.enqueue(
        {
            "chat_id": -100123,
            "id": 10,
            "sender_id": 777,
            "text": "Нужен трансфер в Анталии",
            "date": "2026-01-01T00:00:00+00:00",
        }
    )
    return message_queue.build_delivery_event_id(-100123, 10)


def valid_outbox_payload(message: str = "lead") -> dict[str, Any]:
    return {
        "message": message,
        "link": "https://t.me/test_group/10",
        "sender_username": "sender",
        "sender_id": 777,
        "feedback_message_id": "feedback-1",
        "feedback": {
            "message_text": "Нужен трансфер в Анталии",
            "ai_classification": {"category": "трансфер"},
            "category": "трансфер",
            "region": "Анталия",
            "confidence": 0.9,
        },
    }


async def make_outbox_retries_due(event_id) -> None:
    import message_queue

    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            """
            UPDATE delivery_outbox
            SET next_attempt_at = CURRENT_TIMESTAMP
            WHERE event_id = ? AND status = 'retry'
            """,
            (event_id,),
        )
        await db.commit()
