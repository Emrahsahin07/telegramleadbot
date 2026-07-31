from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import Botparsing
import config
import message_queue
from ai_utils import AIClassificationTechnicalError
from db_lock_resolver import SafeDatabaseManager
from tests.helpers import configure_temp_queue


async def queue_status(queue_id: int) -> tuple[str, int, str | None]:
    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute(
            "SELECT status, attempts, last_error FROM queue WHERE id = ?",
            (queue_id,),
        )
        return await cursor.fetchone()


async def wait_for_status(queue_id: int, expected: str) -> tuple[str, int, str | None]:
    for _ in range(100):
        row = await queue_status(queue_id)
        if row[0] == expected:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"queue row did not reach {expected!r}: {row!r}")


async def stop_worker(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def enqueue_ai_event() -> int:
    assert await message_queue.enqueue(
        {
            "chat_id": -100123,
            "id": 10,
            "sender_id": 777,
            "text": "Нужен трансфер в Анталии",
            "date": "2026-01-01T00:00:00+00:00",
        }
    )
    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute("SELECT id FROM queue ORDER BY id DESC LIMIT 1")
        return (await cursor.fetchone())[0]


class ProcessEvent:
    id = 10
    chat_id = -100123
    sender_id = 777
    raw_text = "Нужен трансфер в Анталии"
    is_group = True
    is_channel = False

    async def get_chat(self):
        return SimpleNamespace(title="Test group", username="test_group")

    async def get_sender(self):
        return SimpleNamespace(
            id=self.sender_id,
            first_name="Sender",
            username="sender",
            bot=False,
        )


@pytest.mark.correct
async def test_pass_outer_ai_timeout_is_typed_for_queue_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_finishes():
        await asyncio.Event().wait()

    monkeypatch.setattr(
        Botparsing.asyncio,
        "to_thread",
        lambda *_args, **_kwargs: never_finishes(),
    )

    with pytest.raises(AIClassificationTechnicalError, match="timed out"):
        await Botparsing._classify_message_with_ai("lead", ["трансфер"], 0.01)


@pytest.mark.correct
async def test_pass_api_error_is_typed_for_queue_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_classification(*_args, **_kwargs):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(Botparsing, "classify_text_with_ai", fail_classification)

    with pytest.raises(AIClassificationTechnicalError, match="network unavailable"):
        await Botparsing._classify_message_with_ai("lead", ["трансфер"], 1)


@pytest.mark.correct
async def test_pass_process_message_propagates_typed_ai_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Botparsing, "metrics", Counter())
    monkeypatch.setattr(Botparsing, "SELF_ID", None)
    monkeypatch.setattr(Botparsing, "SELF_USERNAME", None)
    monkeypatch.setattr(Botparsing, "_should_drop_duplicate", lambda *_args: False)
    monkeypatch.setattr(Botparsing, "contains_negative", lambda *_args: False)
    monkeypatch.setattr(Botparsing, "is_advertisement", lambda *_args: False)
    monkeypatch.setattr(
        Botparsing,
        "infer_region_from_text",
        lambda *_args: "Анталия",
    )
    monkeypatch.setattr(
        Botparsing,
        "_all_locations_from_text",
        lambda *_args: ["Анталия"],
    )
    monkeypatch.setattr(
        Botparsing,
        "categories",
        {"трансфер": {"keywords": ["трансфер"], "subcategories": {}}},
    )
    monkeypatch.setattr(
        Botparsing,
        "subscriptions",
        {
            "101": {
                "categories": ["трансфер"],
                "locations": ["Анталия"],
                "subcats": {},
            }
        },
    )
    monkeypatch.setattr(
        Botparsing,
        "_classify_message_with_ai",
        AsyncMock(side_effect=AIClassificationTechnicalError("AI unavailable")),
    )
    monkeypatch.setattr(config, "notify_admin_error", AsyncMock(return_value=None))

    with pytest.raises(AIClassificationTechnicalError, match="AI unavailable"):
        await Botparsing.process_message(ProcessEvent())


@pytest.mark.correct
async def test_pass_ai_technical_error_retries_then_dies_at_queue_budget_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = await configure_temp_queue(monkeypatch, tmp_path)
    queue_id = await enqueue_ai_event()
    monkeypatch.setenv("WRITE_OUTBOX", "1")
    monkeypatch.setenv("DELIVERY_OUTBOX_WORKER", "1")
    monkeypatch.setenv("QUEUE_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(
        Botparsing,
        "process_message",
        AsyncMock(side_effect=AIClassificationTechnicalError("AI unavailable")),
    )
    monkeypatch.setattr(config, "notify_admin_error", AsyncMock(return_value=None))

    first_worker = asyncio.create_task(Botparsing.worker("ai-test-1", "outbox"))
    first_row = await wait_for_status(queue_id, "retry")
    await stop_worker(first_worker)
    assert first_row[1] == 1

    # Simulate a process restart: reopen the same SQLite DB and make retry due.
    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))
    await message_queue.init_db()
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            "UPDATE queue SET next_attempt_at = CURRENT_TIMESTAMP WHERE id = ?",
            (queue_id,),
        )
        await db.commit()

    second_worker = asyncio.create_task(Botparsing.worker("ai-test-2", "outbox"))
    dead_row = await wait_for_status(queue_id, "dead")
    await stop_worker(second_worker)

    assert dead_row[1] == 2
    assert "AI unavailable" in dead_row[2]
    assert await message_queue.get_shadow_decision("-100123:10") is None


@pytest.mark.correct
async def test_pass_normal_business_result_completes_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    queue_id = await enqueue_ai_event()
    monkeypatch.setenv("WRITE_OUTBOX", "1")
    monkeypatch.setenv("DELIVERY_OUTBOX_WORKER", "1")
    monkeypatch.setattr(Botparsing, "process_message", AsyncMock(return_value=None))

    worker_task = asyncio.create_task(Botparsing.worker("business-result", "outbox"))
    completed_row = await wait_for_status(queue_id, "completed")
    await stop_worker(worker_task)

    assert completed_row == ("completed", 1, None)
