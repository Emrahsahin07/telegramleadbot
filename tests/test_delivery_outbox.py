from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from telethon.errors import (
    FloodWaitError,
    InputUserDeactivatedError,
    MessageTooLongError,
    PeerIdInvalidError,
    RPCError,
    UserIsBlockedError,
)

import delivery
import message_queue
from db_lock_resolver import SafeDatabaseManager
from tests.helpers import (
    RecordingBot,
    active_prefs,
    configure_temp_queue,
    create_queue_event,
    lead_kwargs,
    make_outbox_retries_due,
    valid_outbox_payload,
)


def configure_outbox_delivery(
    monkeypatch: pytest.MonkeyPatch,
    bot: RecordingBot,
    users: dict,
) -> None:
    monkeypatch.setattr(delivery, "bot_client", bot)
    monkeypatch.setattr(delivery, "subscriptions", users)
    monkeypatch.setattr(
        delivery,
        "categories",
        {"трансфер": {"keywords": ["трансфер"], "subcategories": {}}},
    )
    monkeypatch.setattr(delivery, "metrics", Counter())
    monkeypatch.setattr(delivery, "save_subscriptions", lambda: None)
    monkeypatch.setattr(
        delivery.feedback_manager,
        "store_lead_sent",
        AsyncMock(return_value=None),
    )
    monkeypatch.setenv("WRITE_OUTBOX", "1")
    monkeypatch.setenv("DELIVERY_OUTBOX_WORKER", "1")
    monkeypatch.setenv("DELIVERY_MAX_ATTEMPTS", "5")


async def route_event(event_id: str):
    kwargs = lead_kwargs()
    kwargs["event_id"] = event_id
    return await delivery.send_lead_to_users(**kwargs)


class HangingBot(RecordingBot):
    async def send_message(self, uid, *_args, **_kwargs):
        self.calls.append(uid)
        await asyncio.Event().wait()


class ControlledSendBot(RecordingBot):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message(self, uid, *_args, **_kwargs):
        self.calls.append(uid)
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(id=len(self.calls))


def configure_fast_lease_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_OUTBOX_LEASE_SECONDS", "3")
    monkeypatch.setenv("DELIVERY_SEND_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("DB_BUSY_TIMEOUT_SEC", "0")
    monkeypatch.setenv("DELIVERY_LEASE_SAFETY_MARGIN_SECONDS", "0.2")
    monkeypatch.setenv("DELIVERY_LEASE_HEARTBEAT_SECONDS", "0.05")


@pytest.mark.correct
def test_pass_outbox_event_identity_is_stable_telegram_identity() -> None:
    assert message_queue.build_delivery_event_id(-100123, 10) == "-100123:10"
    assert (
        message_queue.build_delivery_event_id(-100123, 10)
        == message_queue.build_delivery_event_id(-100123, 10)
    )
    assert (
        message_queue.build_delivery_event_id(-100123, 10)
        != message_queue.build_delivery_event_id(-100123, 11)
    )


@pytest.mark.correct
async def test_pass_outbox_migration_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)

    await message_queue.init_db()
    await message_queue.init_db()

    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_outbox'"
        )
        assert await cursor.fetchone() == ("delivery_outbox",)
        cursor = await db.execute("SELECT version FROM schema_migrations ORDER BY version")
        assert [row[0] for row in await cursor.fetchall()] == [1, 2, 3]


@pytest.mark.correct
@pytest.mark.parametrize(
    "partial_columns",
    [
        "id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, recipient_uid INTEGER NOT NULL",
        (
            "id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, "
            "recipient_uid INTEGER NOT NULL, status TEXT, payload TEXT"
        ),
    ],
)
async def test_pass_partial_outbox_schema_is_completed_additively(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    partial_columns: str,
) -> None:
    db_path = str(tmp_path / "partial.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY, event TEXT NOT NULL)")
        await db.execute(f"CREATE TABLE delivery_outbox ({partial_columns})")
        await db.commit()
    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))

    await message_queue.init_db()
    await message_queue.init_db()

    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute("PRAGMA table_info(delivery_outbox)")
        columns = {row[1] for row in await cursor.fetchall()}
    assert {
        "payload_version",
        "lease_token",
        "attempts",
        "lease_until",
        "last_error",
        "delivered_at",
    } <= columns


@pytest.mark.correct
async def test_pass_legacy_queue_db_opens_after_outbox_migration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = str(tmp_path / "legacy-queue.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL
            )
            """
        )
        await db.execute("INSERT INTO queue(event) VALUES ('{\"id\": 10}')")
        await db.commit()

    monkeypatch.setattr(message_queue, "DB_PATH", db_path)
    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))
    await message_queue.init_db()

    async with message_queue.db_manager.get_connection() as db:
        cursor = await db.execute("SELECT event FROM queue")
        assert await cursor.fetchone() == ('{"id": 10}',)
        cursor = await db.execute("PRAGMA table_info(delivery_outbox)")
        columns = {row[1] for row in await cursor.fetchall()}
    assert {
        "event_id",
        "recipient_uid",
        "status",
        "attempts",
        "payload",
    } <= columns


@pytest.mark.correct
async def test_pass_unique_event_recipient_constraint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()

    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            "INSERT INTO delivery_outbox(event_id, recipient_uid, payload) VALUES (?, ?, '{}')",
            (event_id, 101),
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO delivery_outbox(event_id, recipient_uid, payload) VALUES (?, ?, '{}')",
                (event_id, 101),
            )


@pytest.mark.correct
async def test_pass_repeated_outbox_creation_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    entries = [
        {"recipient_uid": 101, "payload": valid_outbox_payload("one")},
        {"recipient_uid": 202, "payload": valid_outbox_payload("two")},
    ]

    assert await message_queue.create_delivery_outbox_entries(event_id, entries) == 2
    assert await message_queue.create_delivery_outbox_entries(event_id, entries) == 0
    rows = await message_queue.get_delivery_outbox_rows(event_id)
    assert [(row["recipient_uid"], row["status"]) for row in rows] == [
        (101, "pending"),
        (202, "pending"),
    ]


@pytest.mark.correct
async def test_pass_concurrent_claims_cannot_lease_same_delivery_twice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    await message_queue.create_delivery_outbox_entries(
        event_id,
        [{"recipient_uid": 101, "payload": valid_outbox_payload("one")}],
    )

    manager_a = SafeDatabaseManager(message_queue.db_manager.db_path)
    manager_b = SafeDatabaseManager(message_queue.db_manager.db_path)
    claims = await asyncio.gather(
        message_queue.claim_delivery_outbox(event_id, manager=manager_a),
        message_queue.claim_delivery_outbox(event_id, manager=manager_b),
    )

    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    assert claim["recipient_uid"] == 101
    assert claim["attempts"] == 1


@pytest.mark.correct
async def test_pass_stale_outbox_lease_token_cannot_transition_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    await message_queue.create_delivery_outbox_entries(
        event_id,
        [{"recipient_uid": 101, "payload": valid_outbox_payload()}],
    )
    manager_a = SafeDatabaseManager(message_queue.db_manager.db_path)
    manager_b = SafeDatabaseManager(message_queue.db_manager.db_path)

    claim_a = await message_queue.claim_delivery_outbox(
        event_id, lease_seconds=1, manager=manager_a
    )
    async with manager_b.get_connection() as db:
        await db.execute(
            "UPDATE delivery_outbox SET lease_until = datetime('now', '-1 second')"
        )
        await db.commit()
    claim_b = await message_queue.claim_delivery_outbox(
        event_id, lease_seconds=30, manager=manager_b
    )

    assert claim_a["lease_token"] != claim_b["lease_token"]
    assert await message_queue.mark_delivery_outbox_retry(
        claim_a["id"],
        claim_a["lease_token"],
        "stale retry",
        0,
        max_attempts=5,
        manager=manager_a,
    ) is None
    assert not await message_queue.mark_delivery_outbox_delivered(
        claim_a["id"],
        claim_a["lease_token"],
        1,
        manager=manager_a,
    )
    row = (await message_queue.get_delivery_outbox_rows(event_id))[0]
    assert row["status"] == "processing"
    assert row["lease_token"] == claim_b["lease_token"]
    assert await message_queue.mark_delivery_outbox_delivered(
        claim_b["id"],
        claim_b["lease_token"],
        2,
        manager=manager_b,
    )


@pytest.mark.correct
async def test_pass_partial_success_is_persisted_per_recipient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = RecordingBot({101: [TimeoutError("temporary")]})
    configure_outbox_delivery(
        monkeypatch,
        bot,
        {"101": active_prefs(), "202": active_prefs(), "303": active_prefs()},
    )

    result = await route_event(event_id)
    outcomes = [
        await delivery.deliver_next_outbox(event_id),
        await delivery.deliver_next_outbox(event_id),
        await delivery.deliver_next_outbox(event_id),
    ]

    rows = await message_queue.get_delivery_outbox_rows(event_id)
    assert result.queued_uids == [101, 202, 303]
    assert outcomes == [("retry", 101), ("delivered", 202), ("delivered", 303)]
    assert {row["recipient_uid"]: row["status"] for row in rows} == {
        101: "retry",
        202: "delivered",
        303: "delivered",
    }


@pytest.mark.correct
@pytest.mark.parametrize(
    "recipient_error",
    [
        pytest.param(UserIsBlockedError(request=None), id="user-blocked"),
        pytest.param(InputUserDeactivatedError(request=None), id="user-deactivated"),
        pytest.param(PeerIdInvalidError(request=None), id="peer-invalid"),
        pytest.param(MessageTooLongError(request=None), id="message-too-long"),
    ],
)
async def test_pass_permanent_failure_marks_only_that_recipient_dead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    recipient_error: Exception,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = RecordingBot({101: [recipient_error]})
    configure_outbox_delivery(
        monkeypatch,
        bot,
        {"101": active_prefs(), "202": active_prefs()},
    )

    result = await route_event(event_id)
    outcomes = [
        await delivery.deliver_next_outbox(event_id),
        await delivery.deliver_next_outbox(event_id),
    ]

    rows = await message_queue.get_delivery_outbox_rows(event_id)
    assert result.queued_uids == [101, 202]
    assert outcomes == [("dead", 101), ("delivered", 202)]
    assert {row["recipient_uid"]: row["status"] for row in rows} == {
        101: "dead",
        202: "delivered",
    }


@pytest.mark.correct
@pytest.mark.parametrize(
    "recipient_error",
    [
        pytest.param(TimeoutError("timeout"), id="timeout"),
        pytest.param(ConnectionError("network unavailable"), id="network"),
        pytest.param(FloodWaitError(request=None, capture=7), id="flood-wait"),
        pytest.param(RPCError(request=None, message="temporary rpc", code=500), id="rpc"),
    ],
)
async def test_pass_transient_failure_schedules_only_that_recipient_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    recipient_error: Exception,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = RecordingBot({101: [recipient_error]})
    configure_outbox_delivery(
        monkeypatch,
        bot,
        {"101": active_prefs(), "202": active_prefs()},
    )

    await route_event(event_id)
    assert await delivery.deliver_next_outbox(event_id) == ("retry", 101)
    assert await delivery.deliver_next_outbox(event_id) == ("delivered", 202)

    rows = {row["recipient_uid"]: row for row in await message_queue.get_delivery_outbox_rows(event_id)}
    assert rows[101]["status"] == "retry"
    assert rows[101]["attempts"] == 1
    assert rows[101]["next_attempt_at"] is not None
    assert rows[202]["status"] == "delivered"
    assert rows[101]["lease_token"] is None


@pytest.mark.correct
async def test_pass_send_timeout_is_shorter_than_lease_and_schedules_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = HangingBot()
    configure_outbox_delivery(monkeypatch, bot, {"101": active_prefs()})
    monkeypatch.setenv("DELIVERY_OUTBOX_LEASE_SECONDS", "1")
    monkeypatch.setenv("DELIVERY_SEND_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("DB_BUSY_TIMEOUT_SEC", "0")
    monkeypatch.setenv("DELIVERY_LEASE_SAFETY_MARGIN_SECONDS", "0.1")
    monkeypatch.setenv("DELIVERY_LEASE_HEARTBEAT_SECONDS", "0.05")

    await route_event(event_id)
    assert await delivery.deliver_next_outbox(event_id) == ("retry", 101)

    row = (await message_queue.get_delivery_outbox_rows(event_id))[0]
    assert row["status"] == "retry"
    assert row["lease_token"] is None
    assert row["lease_until"] is None


@pytest.mark.correct
async def test_pass_heartbeat_renews_lease_expiring_during_active_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = ControlledSendBot()
    configure_outbox_delivery(monkeypatch, bot, {"101": active_prefs()})
    configure_fast_lease_heartbeat(monkeypatch)
    await route_event(event_id)

    delivery_task = asyncio.create_task(delivery.deliver_next_outbox(event_id))
    await asyncio.wait_for(bot.started.wait(), timeout=1)
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            """
            UPDATE delivery_outbox
            SET lease_until = datetime('now', '-1 second')
            WHERE event_id = ? AND status = 'processing'
            """,
            (event_id,),
        )
        await db.commit()

    renewed = False
    for _ in range(20):
        async with message_queue.db_manager.get_connection() as db:
            cursor = await db.execute(
                """
                SELECT lease_until > CURRENT_TIMESTAMP
                FROM delivery_outbox
                WHERE event_id = ?
                """,
                (event_id,),
            )
            renewed = bool((await cursor.fetchone())[0])
        if renewed:
            break
        await asyncio.sleep(0.02)
    assert renewed

    second_manager = SafeDatabaseManager(message_queue.db_manager.db_path)
    assert await message_queue.claim_delivery_outbox(
        event_id,
        manager=second_manager,
    ) is None

    bot.release.set()
    assert await delivery_task == ("delivered", 101)
    row = (await message_queue.get_delivery_outbox_rows(event_id))[0]
    assert row["status"] == "delivered"
    assert bot.calls == [101]


@pytest.mark.correct
async def test_pass_second_worker_cannot_reclaim_during_permitted_active_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = ControlledSendBot()
    configure_outbox_delivery(monkeypatch, bot, {"101": active_prefs()})
    configure_fast_lease_heartbeat(monkeypatch)
    await route_event(event_id)

    delivery_task = asyncio.create_task(delivery.deliver_next_outbox(event_id))
    await asyncio.wait_for(bot.started.wait(), timeout=1)
    await asyncio.sleep(0.12)

    second_manager = SafeDatabaseManager(message_queue.db_manager.db_path)
    assert await message_queue.claim_delivery_outbox(
        event_id,
        manager=second_manager,
    ) is None

    bot.release.set()
    assert await delivery_task == ("delivered", 101)
    assert bot.calls == [101]


@pytest.mark.correct
async def test_pass_retry_sends_only_failed_recipient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = RecordingBot({101: [TimeoutError("temporary")]})
    configure_outbox_delivery(
        monkeypatch,
        bot,
        {"101": active_prefs(), "202": active_prefs()},
    )

    await route_event(event_id)
    assert await delivery.deliver_next_outbox(event_id) == ("retry", 101)
    assert await delivery.deliver_next_outbox(event_id) == ("delivered", 202)
    await make_outbox_retries_due(event_id)
    outcome = await delivery.deliver_next_outbox(event_id)

    assert outcome == ("delivered", 101)
    assert bot.calls == [101, 202, 101]
    assert bot.calls.count(202) == 1


@pytest.mark.correct
async def test_pass_delivered_recipient_is_not_sent_again_when_event_is_rerouted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = RecordingBot()
    configure_outbox_delivery(monkeypatch, bot, {"101": active_prefs()})

    first = await route_event(event_id)
    assert first.queued_uids == [101]
    assert bot.calls == []
    assert await delivery.deliver_next_outbox(event_id) == ("delivered", 101)
    second = await route_event(event_id)
    assert second.queued_uids == []

    rows = await message_queue.get_delivery_outbox_rows(event_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"
    assert rows[0]["attempts"] == 1
    assert bot.calls == [101]


@pytest.mark.correct
async def test_pass_retry_budget_moves_transient_failure_to_dead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    bot = RecordingBot(
        {101: [TimeoutError("temporary-1"), TimeoutError("temporary-2")]}
    )
    configure_outbox_delivery(monkeypatch, bot, {"101": active_prefs()})
    monkeypatch.setenv("DELIVERY_MAX_ATTEMPTS", "2")

    await route_event(event_id)
    assert await delivery.deliver_next_outbox(event_id) == ("retry", 101)
    await make_outbox_retries_due(event_id)
    assert await delivery.deliver_next_outbox(event_id) == ("dead", 101)

    row = (await message_queue.get_delivery_outbox_rows(event_id))[0]
    assert row["status"] == "dead"
    assert row["attempts"] == 2
    assert await delivery.deliver_next_outbox(event_id) is None


@pytest.mark.correct
@pytest.mark.parametrize(
    ("raw_payload", "payload_version", "error_fragment"),
    [
        ("{bad json", 1, "malformed payload JSON"),
        ('{"message": "missing fields"}', 1, "feedback_message_id"),
        ("{}", 999, "unsupported payload_version"),
        ("{}", "invalid", "invalid payload_version"),
    ],
)
async def test_pass_poison_outbox_payload_is_dead_lettered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    raw_payload: str,
    payload_version,
    error_fragment: str,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = "poison:1"
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            """
            INSERT INTO delivery_outbox(
                event_id, recipient_uid, payload, payload_version
            ) VALUES (?, 101, ?, ?)
            """,
            (event_id, raw_payload, payload_version),
        )
        await db.commit()

    assert await message_queue.claim_delivery_outbox(event_id) is None
    row = (await message_queue.get_delivery_outbox_rows(event_id))[0]
    assert row["status"] == "dead"
    assert error_fragment in row["last_error"]
    assert row["lease_token"] is None


@pytest.mark.correct
async def test_pass_expired_outbox_leases_respect_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = "lease-budget:1"
    await message_queue.create_delivery_outbox_entries(
        event_id,
        [{"recipient_uid": 101, "payload": valid_outbox_payload()}],
    )

    first = await message_queue.claim_delivery_outbox(event_id, max_attempts=2)
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            "UPDATE delivery_outbox SET lease_until = datetime('now', '-1 second')"
        )
        await db.commit()
    second = await message_queue.claim_delivery_outbox(event_id, max_attempts=2)
    assert first["lease_token"] != second["lease_token"]
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            "UPDATE delivery_outbox SET lease_until = datetime('now', '-1 second')"
        )
        await db.commit()

    assert await message_queue.claim_delivery_outbox(event_id, max_attempts=2) is None
    row = (await message_queue.get_delivery_outbox_rows(event_id))[0]
    assert row["status"] == "dead"
    assert row["attempts"] == 2


@pytest.mark.correct
async def test_pass_queue_crash_recovery_does_not_duplicate_delivered_recipient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = await configure_temp_queue(monkeypatch, tmp_path)
    queue_payload = {
        "chat_id": -100123,
        "id": 10,
        "sender_id": 777,
        "text": "Нужен трансфер в Анталии",
        "date": "2026-01-01T00:00:00+00:00",
    }
    assert await message_queue.enqueue(queue_payload)
    crashed_claim = await message_queue.dequeue(recovery_enabled=True)
    async with message_queue.db_manager.get_connection() as db:
        await db.execute(
            "UPDATE queue SET lease_until = datetime('now', '-1 second') WHERE id = ?",
            (crashed_claim[0],),
        )
        await db.commit()

    monkeypatch.setattr(message_queue, "db_manager", SafeDatabaseManager(db_path))
    await message_queue.init_db()
    recovered_claim = await message_queue.dequeue(recovery_enabled=True)
    assert recovered_claim[0] == crashed_claim[0]
    assert recovered_claim[2] != crashed_claim[2]

    bot = RecordingBot()
    configure_outbox_delivery(monkeypatch, bot, {"101": active_prefs()})
    kwargs = lead_kwargs()
    kwargs.update(
        {
            "event_id": message_queue.build_delivery_event_id(-100123, 10),
            "queue_id": recovered_claim[0],
            "queue_lease_token": recovered_claim[2],
        }
    )
    routed = await delivery.send_lead_to_users(**kwargs)
    assert routed.queued_uids == [101]
    assert await delivery.deliver_next_outbox(kwargs["event_id"]) == ("delivered", 101)

    # The current queue still permits reinsertion after completion. Stable
    # outbox identity nevertheless prevents another recipient delivery.
    assert await message_queue.enqueue(queue_payload)
    repeated_claim = await message_queue.dequeue(recovery_enabled=True)
    kwargs["queue_id"] = repeated_claim[0]
    kwargs["queue_lease_token"] = repeated_claim[2]
    repeated = await delivery.send_lead_to_users(**kwargs)

    assert repeated.queued_uids == []
    assert await delivery.deliver_next_outbox(kwargs["event_id"]) is None
    assert bot.calls == [101]
