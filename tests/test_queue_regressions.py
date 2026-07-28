from __future__ import annotations

import pytest

import message_queue
from db_lock_resolver import SafeDatabaseManager


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
    queue_id, _ = await message_queue.dequeue()
    await message_queue.mark_completed(queue_id)

    assert await message_queue.enqueue(payload) is False


@pytest.mark.known_bug
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Known bug: init_db does not reclaim processing rows after a worker crash "
        "or process restart"
    ),
)
async def test_xfail_processing_event_is_reclaimed_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """XFAIL: a claimed event must become processable after its worker disappears."""

    db_path = await use_temp_queue(monkeypatch, tmp_path)
    payload = event()
    assert await message_queue.enqueue(payload) is True
    first_claim = await message_queue.dequeue()
    assert first_claim is not None

    # Simulate a fresh process opening the same DB. No completion/failure is
    # recorded for the first claim, exactly as after a worker crash.
    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))
    await message_queue.init_db()

    reclaimed = await message_queue.dequeue()
    assert reclaimed is not None
    assert reclaimed[0] == first_claim[0]
