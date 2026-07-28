from __future__ import annotations

import pytest

import message_queue
from db_lock_resolver import SafeDatabaseManager
from tests.helpers import valid_outbox_payload


async def use_temp_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> str:
    db_path = str(tmp_path / "queue.db")
    monkeypatch.setattr(message_queue, "DB_PATH", db_path)
    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))
    monkeypatch.setattr(message_queue, "MAX_QUEUE_SIZE", 100)
    await message_queue.init_db()
    return db_path


def event(chat_id: int = -100123, message_id: int = 10, text: str = "lead") -> dict:
    return {
        "chat_id": chat_id,
        "id": message_id,
        "sender_id": 777,
        "text": text,
        "date": "2026-01-01T00:00:00+00:00",
    }


@pytest.mark.correct
async def test_pass_event_identity_is_chat_id_and_message_id_while_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """PASS: mutable payload fields do not alter pending event identity."""

    await use_temp_queue(monkeypatch, tmp_path)

    assert await message_queue.enqueue(event(text="first text")) is True
    assert await message_queue.enqueue(event(text="edited text")) is False
    assert await message_queue.enqueue(event(message_id=11, text="first text")) is True
    assert await message_queue.count_pending() == 2


@pytest.mark.correct
async def test_pass_duplicate_pending_event_is_not_inserted_twice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """PASS: the current pending-state duplicate check rejects a second insert."""

    await use_temp_queue(monkeypatch, tmp_path)
    payload = event()

    assert await message_queue.enqueue(payload) is True
    assert await message_queue.enqueue(payload) is False
    assert await message_queue.count_pending() == 1


@pytest.mark.known_bug
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Known bug: identity is checked only among pending rows, so a completed "
        "Telegram event can be inserted and processed again"
    ),
)
async def test_xfail_event_identity_remains_unique_after_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """XFAIL: durable Telegram identity must survive status transitions."""

    await use_temp_queue(monkeypatch, tmp_path)
    payload = event()
    assert await message_queue.enqueue(payload) is True
    queue_id, _, lease_token = await message_queue.dequeue()
    await message_queue.mark_completed(queue_id, lease_token)

    assert await message_queue.enqueue(payload) is False


@pytest.mark.correct
async def test_pass_processing_event_is_reclaimed_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """XFAIL: a claimed event must become processable after its worker disappears."""

    db_path = await use_temp_queue(monkeypatch, tmp_path)
    payload = event()
    assert await message_queue.enqueue(payload) is True
    first_claim = await message_queue.dequeue(recovery_enabled=True)
    assert first_claim is not None
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            "UPDATE queue SET lease_until = datetime('now', '-1 second') WHERE id = ?",
            (first_claim[0],),
        )
        await db.commit()

    # Simulate a fresh process opening the same DB. No completion/failure is
    # recorded for the first claim, exactly as after a worker crash.
    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))
    await message_queue.init_db()

    reclaimed = await message_queue.dequeue(recovery_enabled=True)
    assert reclaimed is not None
    assert reclaimed[0] == first_claim[0]
    assert reclaimed[2] != first_claim[2]


@pytest.mark.correct
async def test_pass_routing_outbox_and_queue_completion_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await use_temp_queue(monkeypatch, tmp_path)
    assert await message_queue.enqueue(event())
    queue_id, payload, lease_token = await message_queue.dequeue()
    event_id = message_queue.build_delivery_event_id(payload["chat_id"], payload["id"])

    queued = await message_queue.persist_routing_and_complete_queue(
        queue_id=queue_id,
        queue_lease_token=lease_token,
        event_id=event_id,
        entries=[{"recipient_uid": 101, "payload": valid_outbox_payload()}],
    )

    assert queued == [101]
    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute("SELECT status FROM queue WHERE id = ?", (queue_id,))
        assert await cursor.fetchone() == ("completed",)
        cursor = await db.execute(
            "SELECT recipient_uid, status FROM delivery_outbox WHERE event_id = ?",
            (event_id,),
        )
        assert await cursor.fetchone() == (101, "pending")


@pytest.mark.correct
async def test_pass_lost_queue_lease_rolls_back_outbox_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await use_temp_queue(monkeypatch, tmp_path)
    assert await message_queue.enqueue(event())
    queue_id, payload, _lease_token = await message_queue.dequeue()
    event_id = message_queue.build_delivery_event_id(payload["chat_id"], payload["id"])

    with pytest.raises(RuntimeError, match="lease lost"):
        await message_queue.persist_routing_and_complete_queue(
            queue_id=queue_id,
            queue_lease_token="stale-token",
            event_id=event_id,
            entries=[{"recipient_uid": 101, "payload": valid_outbox_payload()}],
        )

    assert await message_queue.get_delivery_outbox_rows(event_id) == []
    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute("SELECT status FROM queue WHERE id = ?", (queue_id,))
        assert await cursor.fetchone() == ("processing",)


@pytest.mark.correct
async def test_pass_migration_does_not_reclaim_legacy_processing_without_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = await use_temp_queue(monkeypatch, tmp_path)
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            """
            INSERT INTO queue(event, status, created_at)
            VALUES (?, 'processing', CURRENT_TIMESTAMP)
            """,
            ('{"id":10,"chat_id":-100123,"text":"lead"}',),
        )
        await db.commit()

    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))
    await message_queue.init_db()

    assert await message_queue.dequeue() is None
    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute(
            "SELECT status, lease_until FROM queue WHERE status = 'processing'"
        )
        assert await cursor.fetchone() == ("processing", None)


@pytest.mark.correct
async def test_pass_queue_lease_expiration_respects_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await use_temp_queue(monkeypatch, tmp_path)
    assert await message_queue.enqueue(event())
    first = await message_queue.dequeue(max_attempts=2, recovery_enabled=True)
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            "UPDATE queue SET lease_until = datetime('now', '-1 second') WHERE id = ?",
            (first[0],),
        )
        await db.commit()
    second = await message_queue.dequeue(max_attempts=2, recovery_enabled=True)
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            "UPDATE queue SET lease_until = datetime('now', '-1 second') WHERE id = ?",
            (second[0],),
        )
        await db.commit()

    assert await message_queue.dequeue(max_attempts=2, recovery_enabled=True) is None
    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute(
            "SELECT status, attempts FROM queue WHERE id = ?",
            (first[0],),
        )
        assert await cursor.fetchone() == ("dead", 2)
