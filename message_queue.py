# queue.py
import aiosqlite
import json
import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Union, Optional, Dict, Any, Tuple, Iterable, List
from db_lock_resolver import SafeDatabaseManager

DB_PATH = os.getenv("QUEUE_DB", "queue.db")
logger = logging.getLogger("queue")

# Initialize safe database manager
db_manager = SafeDatabaseManager(DB_PATH)

# Database optimization settings
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "10000"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL_HOURS", "24"))  # hours
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))  # days
OUTBOX_PAYLOAD_VERSION = 1


def build_delivery_event_id(chat_id: int, message_id: int) -> str:
    """Stable Telegram identity used by per-recipient delivery idempotency."""
    return f"{int(chat_id)}:{int(message_id)}"


async def init_db():
    """Apply additive, versioned queue/outbox migrations."""

    # Initialize safe database manager first
    if not await db_manager.initialize():
        logger.error("❌ Failed to initialize database manager")
        raise RuntimeError("Database initialization failed")

    async with db_manager.get_connection() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP NULL,
                priority INTEGER DEFAULT 0,
                processing_started TIMESTAMP NULL,
                lease_until TIMESTAMP NULL,
                lease_token TEXT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP NULL,
                last_error TEXT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                recipient_uid INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'processing', 'delivered', 'retry', 'dead')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP NULL,
                lease_until TIMESTAMP NULL,
                telegram_message_id INTEGER NULL,
                last_error TEXT NULL,
                payload TEXT NOT NULL,
                payload_version INTEGER NOT NULL DEFAULT 1,
                lease_token TEXT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                delivered_at TIMESTAMP NULL,
                UNIQUE(event_id, recipient_uid)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_decisions (
                event_id TEXT PRIMARY KEY,
                category TEXT NULL,
                subcategory TEXT NULL,
                location TEXT NULL,
                raw_confidence REAL NULL,
                calibrated_confidence REAL NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                model TEXT NULL,
                prompt_id TEXT NULL,
                prompt_version TEXT NULL,
                config_version TEXT NULL,
                policy_version TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        queue_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(queue)")).fetchall()
        }
        queue_additions = {
            "status": "TEXT DEFAULT 'pending'",
            "created_at": "TIMESTAMP",
            "processed_at": "TIMESTAMP NULL",
            "priority": "INTEGER DEFAULT 0",
            "processing_started": "TIMESTAMP NULL",
            "lease_until": "TIMESTAMP NULL",
            "lease_token": "TEXT NULL",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_attempt_at": "TIMESTAMP NULL",
            "last_error": "TEXT NULL",
        }
        for column, definition in queue_additions.items():
            if column not in queue_columns:
                await db.execute(f"ALTER TABLE queue ADD COLUMN {column} {definition}")
        await db.execute(
            "UPDATE queue SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        await db.execute("UPDATE queue SET status = 'pending' WHERE status IS NULL")
        await db.execute("UPDATE queue SET attempts = 0 WHERE attempts IS NULL")
        await db.execute("UPDATE queue SET priority = 0 WHERE priority IS NULL")

        outbox_columns = {
            row[1]
            for row in await (await db.execute("PRAGMA table_info(delivery_outbox)")).fetchall()
        }
        required_core = {"id", "event_id", "recipient_uid"}
        if not required_core <= outbox_columns:
            missing = sorted(required_core - outbox_columns)
            raise RuntimeError(
                f"delivery_outbox partial schema is missing non-additive core columns: {missing}"
            )
        outbox_additions = {
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_attempt_at": "TIMESTAMP NULL",
            "lease_until": "TIMESTAMP NULL",
            "telegram_message_id": "INTEGER NULL",
            "last_error": "TEXT NULL",
            "payload": "TEXT NULL",
            "payload_version": "INTEGER NOT NULL DEFAULT 1",
            "lease_token": "TEXT NULL",
            "created_at": "TIMESTAMP NULL",
            "delivered_at": "TIMESTAMP NULL",
        }
        for column, definition in outbox_additions.items():
            if column not in outbox_columns:
                await db.execute(
                    f"ALTER TABLE delivery_outbox ADD COLUMN {column} {definition}"
                )
        await db.execute(
            "UPDATE delivery_outbox SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        await db.execute(
            "UPDATE delivery_outbox SET status = 'pending' WHERE status IS NULL"
        )
        await db.execute(
            "UPDATE delivery_outbox SET attempts = 0 WHERE attempts IS NULL"
        )
        await db.execute(
            """
            UPDATE delivery_outbox
            SET payload_version = 1
            WHERE payload_version IS NULL
            """
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON queue(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON queue(created_at)")
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_priority_status
            ON queue(priority DESC, status, next_attempt_at, created_at)
            """
        )
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_outbox_event_recipient
            ON delivery_outbox(event_id, recipient_uid)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_delivery_outbox_ready
            ON delivery_outbox(status, next_attempt_at, lease_until, id)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shadow_decisions_created
            ON shadow_decisions(created_at)
            """
        )
        await db.executemany(
            """
            INSERT OR IGNORE INTO schema_migrations(version, name)
            VALUES (?, ?)
            """,
            [
                (1, "legacy_queue_additive_columns"),
                (2, "delivery_outbox"),
                (3, "fenced_leases_and_payload_version"),
                (4, "shadow_borderline_telemetry"),
            ],
        )

        # Finish additive DDL/DML before changing connection safety pragmas.
        await db.commit()

        # SQLite performance optimizations
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA cache_size=10000;")
        await db.execute("PRAGMA temp_store=MEMORY;")
        await db.execute("PRAGMA mmap_size=268435456;")  # 256MB

        await db.commit()
    logger.info("✅ Очередь SQLite инициализирована/мигрирована")
    # Optional: clear pending queue on start (useful to stop duplicates after code changes)
    if os.getenv("CLEAR_QUEUE_ON_START", "0") == "1":
        try:
            async with db_manager.get_connection() as db:
                await db.execute("DELETE FROM queue WHERE status = 'pending'")
                await db.commit()
            logger.info("🧹 Очередь очищена по флагу CLEAR_QUEUE_ON_START=1")
        except Exception as e:
            logger.error(f"Не удалось очистить очередь: {e}")


async def record_shadow_decision(
    *,
    event_id: Union[int, str],
    category: Optional[str],
    subcategory: Optional[str],
    location: Optional[str],
    raw_confidence: Optional[float],
    calibrated_confidence: float,
    decision: str,
    reason: str,
    model: Optional[str],
    prompt_id: Optional[str],
    prompt_version: Optional[str],
    config_version: Optional[str],
    policy_version: str,
    manager: Optional[SafeDatabaseManager] = None,
) -> None:
    """Durably upsert one privacy-minimal shadow decision per Telegram event."""
    async with (manager or db_manager).get_connection() as db:
        await db.execute(
            """
            INSERT INTO shadow_decisions(
                event_id, category, subcategory, location,
                raw_confidence, calibrated_confidence,
                decision, reason, model, prompt_id, prompt_version,
                config_version, policy_version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(event_id) DO UPDATE SET
                category = excluded.category,
                subcategory = excluded.subcategory,
                location = excluded.location,
                raw_confidence = excluded.raw_confidence,
                calibrated_confidence = excluded.calibrated_confidence,
                decision = excluded.decision,
                reason = excluded.reason,
                model = excluded.model,
                prompt_id = excluded.prompt_id,
                prompt_version = excluded.prompt_version,
                config_version = excluded.config_version,
                policy_version = excluded.policy_version,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                str(event_id),
                category,
                subcategory,
                location,
                raw_confidence,
                float(calibrated_confidence),
                decision,
                reason,
                model,
                prompt_id,
                prompt_version,
                config_version,
                policy_version,
            ),
        )
        await db.commit()


async def get_shadow_decision(
    event_id: Union[int, str],
    *,
    manager: Optional[SafeDatabaseManager] = None,
) -> Optional[Dict[str, Any]]:
    async with (manager or db_manager).get_connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM shadow_decisions WHERE event_id = ?",
            (str(event_id),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def create_delivery_outbox_entries(
    event_id: Union[int, str],
    entries: Iterable[Dict[str, Any]],
    *,
    manager: Optional[SafeDatabaseManager] = None,
) -> int:
    """Atomically insert routed deliveries, ignoring already-routed recipients."""
    return len(
        await persist_routing_outbox(
            event_id,
            entries,
            manager=manager,
        )
    )


async def persist_routing_outbox(
    event_id: Union[int, str],
    entries: Iterable[Dict[str, Any]],
    *,
    manager: Optional[SafeDatabaseManager] = None,
) -> List[int]:
    """Persist a recipient set and return only recipients newly queued."""
    rows = _prepare_outbox_rows(event_id, entries)
    if not rows:
        return []

    async with (manager or db_manager).get_connection() as db:
        inserted_uids = await _insert_outbox_rows(db, rows)
        await db.commit()
    return inserted_uids


def _prepare_outbox_rows(
    event_id: Union[int, str],
    entries: Iterable[Dict[str, Any]],
) -> List[Tuple[str, int, str, int]]:
    return [
        (
            str(event_id),
            int(entry["recipient_uid"]),
            json.dumps(entry["payload"], ensure_ascii=False),
            int(entry.get("payload_version", OUTBOX_PAYLOAD_VERSION)),
        )
        for entry in entries
    ]


async def _insert_outbox_rows(db, rows: Iterable[Tuple[str, int, str, int]]) -> List[int]:
    inserted_uids = []
    for event_id, recipient_uid, payload, payload_version in rows:
        cursor = await db.execute(
            """
            INSERT INTO delivery_outbox(
                event_id, recipient_uid, status, attempts,
                payload, payload_version, created_at
            )
            VALUES (?, ?, 'pending', 0, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(event_id, recipient_uid) DO NOTHING
            """,
            (event_id, recipient_uid, payload, payload_version),
        )
        if cursor.rowcount == 1:
            inserted_uids.append(recipient_uid)
    return inserted_uids


async def persist_routing_and_complete_queue(
    *,
    queue_id: int,
    queue_lease_token: str,
    event_id: Union[int, str],
    entries: Iterable[Dict[str, Any]],
) -> List[int]:
    """Persist the recipient set and complete its queue event in one transaction."""
    rows = _prepare_outbox_rows(event_id, entries)
    async with db_manager.get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        inserted_uids = await _insert_outbox_rows(db, rows)
        cursor = await db.execute(
            """
            UPDATE queue
            SET status = 'completed',
                processed_at = CURRENT_TIMESTAMP,
                processing_started = NULL,
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = NULL,
                last_error = NULL
            WHERE id = ?
              AND status = 'processing'
              AND lease_token = ?
            """,
            (queue_id, queue_lease_token),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise RuntimeError(
                f"queue completion lease lost for event row {queue_id}"
            )
        await db.commit()
        return inserted_uids


class PermanentOutboxPayloadError(ValueError):
    pass


def _decode_outbox_payload(raw_payload: Optional[str], payload_version: Any) -> Dict[str, Any]:
    try:
        version = int(payload_version)
    except (TypeError, ValueError) as error:
        raise PermanentOutboxPayloadError(
            f"invalid payload_version={payload_version!r}"
        ) from error
    if version != OUTBOX_PAYLOAD_VERSION:
        raise PermanentOutboxPayloadError(
            f"unsupported payload_version={version}"
        )
    try:
        payload = json.loads(raw_payload) if raw_payload is not None else None
    except (TypeError, json.JSONDecodeError) as error:
        raise PermanentOutboxPayloadError(f"malformed payload JSON: {error}") from error
    if not isinstance(payload, dict):
        raise PermanentOutboxPayloadError("payload must be a JSON object")
    required = {
        "message": str,
        "feedback_message_id": str,
        "feedback": dict,
    }
    for field, expected_type in required.items():
        if not isinstance(payload.get(field), expected_type):
            raise PermanentOutboxPayloadError(
                f"payload field {field!r} must be {expected_type.__name__}"
            )
    feedback = payload["feedback"]
    if not isinstance(feedback.get("ai_classification"), dict):
        raise PermanentOutboxPayloadError(
            "payload feedback.ai_classification must be an object"
        )
    for field in (
        "message_text",
        "ai_classification",
        "category",
        "region",
        "confidence",
    ):
        if field not in feedback:
            raise PermanentOutboxPayloadError(
                f"payload feedback field {field!r} is required"
            )
    return payload


async def claim_delivery_outbox(
    event_id: Optional[Union[int, str]] = None,
    lease_seconds: Optional[int] = None,
    *,
    max_attempts: Optional[int] = None,
    manager: Optional[SafeDatabaseManager] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically claim and fence one valid, due outbox delivery."""
    if lease_seconds is None:
        lease_seconds = int(os.getenv("DELIVERY_OUTBOX_LEASE_SECONDS", "120"))
    if max_attempts is None:
        max_attempts = int(os.getenv("DELIVERY_MAX_ATTEMPTS", "5"))
    lease_modifier = f"+{max(1, int(lease_seconds))} seconds"
    event_clause = "AND event_id = ?" if event_id is not None else ""
    params: List[Any] = []
    if event_id is not None:
        params.append(str(event_id))

    async with (manager or db_manager).get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            UPDATE delivery_outbox
            SET status = 'dead',
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = NULL,
                last_error = COALESCE(last_error, 'delivery lease expired; retry budget exhausted')
            WHERE status = 'processing'
              AND lease_until IS NOT NULL
              AND lease_until <= CURRENT_TIMESTAMP
              AND attempts >= ?
            """,
            (max_attempts,),
        )
        await db.execute(
            """
            UPDATE delivery_outbox
            SET status = 'dead',
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = NULL,
                last_error = COALESCE(last_error, 'delivery retry budget exhausted')
            WHERE status IN ('pending', 'retry')
              AND attempts >= ?
            """,
            (max_attempts,),
        )
        await db.execute(
            """
            UPDATE delivery_outbox
            SET status = 'retry',
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = CURRENT_TIMESTAMP,
                last_error = COALESCE(last_error, 'delivery lease expired')
            WHERE status = 'processing'
              AND lease_until IS NOT NULL
              AND lease_until <= CURRENT_TIMESTAMP
              AND attempts < ?
            """
            ,
            (max_attempts,),
        )

        while True:
            cursor = await db.execute(
                f"""
                SELECT id, event_id, recipient_uid, attempts, payload, payload_version
                FROM delivery_outbox
                WHERE status IN ('pending', 'retry')
                  AND attempts < ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)
                  {event_clause}
                ORDER BY id
                LIMIT 1
                """,
                [max_attempts, *params],
            )
            row = await cursor.fetchone()
            if not row:
                await db.commit()
                return None
            try:
                payload = _decode_outbox_payload(row[4], row[5])
            except PermanentOutboxPayloadError as error:
                await db.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'dead',
                        last_error = ?,
                        lease_until = NULL,
                        lease_token = NULL,
                        next_attempt_at = NULL
                    WHERE id = ? AND status IN ('pending', 'retry')
                    """,
                    (str(error)[:2000], row[0]),
                )
                continue

            lease_token = uuid.uuid4().hex
            await db.execute(
                """
                UPDATE delivery_outbox
                SET status = 'processing',
                    attempts = attempts + 1,
                    lease_until = datetime('now', ?),
                    lease_token = ?
                WHERE id = ? AND status IN ('pending', 'retry')
                """,
                (lease_modifier, lease_token, row[0]),
            )
            await db.commit()
            return {
                "id": row[0],
                "event_id": row[1],
                "recipient_uid": row[2],
                "attempts": row[3] + 1,
                "payload": payload,
                "payload_version": row[5],
                "lease_token": lease_token,
            }


async def mark_delivery_outbox_delivered(
    delivery_id: int,
    lease_token: str,
    telegram_message_id: Optional[int],
    *,
    manager: Optional[SafeDatabaseManager] = None,
) -> bool:
    async with (manager or db_manager).get_connection() as db:
        cursor = await db.execute(
            """
            UPDATE delivery_outbox
            SET status = 'delivered',
                telegram_message_id = ?,
                delivered_at = CURRENT_TIMESTAMP,
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = NULL,
                last_error = NULL
            WHERE id = ?
              AND status = 'processing'
              AND lease_token = ?
            """,
            (telegram_message_id, delivery_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1


async def renew_delivery_outbox_lease(
    delivery_id: int,
    lease_token: str,
    lease_seconds: int,
    *,
    manager: Optional[SafeDatabaseManager] = None,
) -> bool:
    """Extend an active delivery lease without changing attempt ownership."""
    lease_modifier = f"+{max(1, int(lease_seconds))} seconds"
    async with (manager or db_manager).get_connection() as db:
        cursor = await db.execute(
            """
            UPDATE delivery_outbox
            SET lease_until = datetime('now', ?)
            WHERE id = ?
              AND status = 'processing'
              AND lease_token = ?
            """,
            (lease_modifier, delivery_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1


async def mark_delivery_outbox_dead(
    delivery_id: int,
    lease_token: str,
    error: str,
    *,
    manager: Optional[SafeDatabaseManager] = None,
) -> bool:
    async with (manager or db_manager).get_connection() as db:
        cursor = await db.execute(
            """
            UPDATE delivery_outbox
            SET status = 'dead',
                last_error = ?,
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = NULL
            WHERE id = ?
              AND status = 'processing'
              AND lease_token = ?
            """,
            (error[:2000], delivery_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1


async def mark_delivery_outbox_retry(
    delivery_id: int,
    lease_token: str,
    error: str,
    delay_seconds: int,
    *,
    max_attempts: int,
    manager: Optional[SafeDatabaseManager] = None,
) -> Optional[str]:
    """Schedule another attempt, or exhaust the row into ``dead``."""
    delay_modifier = f"+{max(0, int(delay_seconds))} seconds"
    async with (manager or db_manager).get_connection() as db:
        cursor = await db.execute(
            """
            SELECT attempts FROM delivery_outbox
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (delivery_id, lease_token),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        attempts = int(row[0])
        status = "dead" if attempts >= max_attempts else "retry"
        next_attempt_at = None if status == "dead" else delay_modifier
        cursor = await db.execute(
            """
            UPDATE delivery_outbox
            SET status = ?,
                last_error = ?,
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = CASE
                    WHEN ? IS NULL THEN NULL
                    ELSE datetime('now', ?)
                END
            WHERE id = ?
              AND status = 'processing'
              AND lease_token = ?
            """,
            (
                status,
                error[:2000],
                next_attempt_at,
                next_attempt_at,
                delivery_id,
                lease_token,
            ),
        )
        await db.commit()
        return status if cursor.rowcount == 1 else None


async def get_delivery_outbox_rows(
    event_id: Optional[Union[int, str]] = None,
) -> List[Dict[str, Any]]:
    """Read-only helper used by diagnostics and deterministic tests."""
    query = """
        SELECT id, event_id, recipient_uid, status, attempts, next_attempt_at,
               lease_until, lease_token, telegram_message_id, last_error, payload,
               payload_version, created_at, delivered_at
        FROM delivery_outbox
    """
    params: Tuple[Any, ...] = ()
    if event_id is not None:
        query += " WHERE event_id = ?"
        params = (str(event_id),)
    query += " ORDER BY id"
    async with db_manager.get_connection() as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
    keys = (
        "id", "event_id", "recipient_uid", "status", "attempts",
        "next_attempt_at", "lease_until", "lease_token", "telegram_message_id",
        "last_error", "payload", "payload_version", "created_at", "delivered_at",
    )
    result = []
    for row in rows:
        item = dict(zip(keys, row))
        try:
            item["payload"] = json.loads(item["payload"])
        except (TypeError, json.JSONDecodeError):
            item["payload"] = None
        result.append(item)
    return result

async def enqueue(event_dict: Dict[str, Any], priority: int = 0) -> bool:
    """Добавляет событие в очередь с приоритетом (с ретраями при lock)."""
    max_attempts = 6
    backoff = 0.05
    payload = json.dumps(event_dict, ensure_ascii=False)
    for attempt in range(1, max_attempts + 1):
        try:
            async with db_manager.get_connection() as db:
                # Check queue size and prevent overflow
                cursor = await db.execute("SELECT COUNT(*) FROM queue WHERE status = 'pending'")
                count = (await cursor.fetchone())[0]

                if count >= MAX_QUEUE_SIZE:
                    logger.warning(f"Очередь переполнена ({count}/{MAX_QUEUE_SIZE}), пропускаем сообщение")
                    return False

                # De-duplication: skip if same (chat_id, id) already pending
                try:
                    chat_id = event_dict.get("chat_id")
                    msg_id = event_dict.get("id")
                    if chat_id is not None and msg_id is not None:
                        dedup_cur = await db.execute(
                            """
                            SELECT 1 FROM queue 
                            WHERE status = 'pending' 
                              AND json_extract(event, '$.chat_id') = ? 
                              AND json_extract(event, '$.id') = ? 
                            LIMIT 1
                            """,
                            (chat_id, msg_id)
                        )
                        if await dedup_cur.fetchone():
                            logger.debug(f"queue: skip duplicate chat_id={chat_id} id={msg_id}")
                            await db.commit()
                            return False
                except Exception:
                    # If json_extract not available, ignore and insert
                    pass

                await db.execute(
                    """
                    INSERT INTO queue (event, priority, status, created_at)
                    VALUES (?, ?, 'pending', CURRENT_TIMESTAMP)
                    """,
                    (payload, priority)
                )
                await db.commit()
                return True
        except Exception as e:
            msg = str(e)
            if "database is locked" in msg or "database is busy" in msg:
                if attempt < max_attempts:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 0.8)
                    continue
            logger.error(f"Ошибка добавления в очередь: {e}")
            return False
    return False

def _decode_queue_event(raw_event: str) -> Dict[str, Any]:
    try:
        event = json.loads(raw_event)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"malformed queue event JSON: {error}") from error
    if not isinstance(event, dict):
        raise ValueError("queue event must be a JSON object")
    for field in ("id", "chat_id", "text"):
        if field not in event:
            raise ValueError(f"queue event field {field!r} is required")
    return event


async def dequeue(
    lease_seconds: Optional[int] = None,
    *,
    max_attempts: Optional[int] = None,
    recovery_enabled: Optional[bool] = None,
    manager: Optional[SafeDatabaseManager] = None,
) -> Union[Tuple[int, Dict[str, Any], str], None]:
    """Atomically claim one due queue event with a fenced, expiring lease."""
    if recovery_enabled is None:
        recovery_enabled = (
            os.getenv("WRITE_OUTBOX", "0") == "1"
            and os.getenv("DELIVERY_OUTBOX_WORKER", "0") == "1"
        )
    if lease_seconds is None:
        lease_seconds = int(os.getenv("QUEUE_LEASE_SECONDS", "300"))
    if max_attempts is None:
        max_attempts = int(os.getenv("QUEUE_MAX_ATTEMPTS", "5"))
    lease_modifier = f"+{max(1, int(lease_seconds))} seconds"
    lock_max_attempts = 6
    backoff = 0.05
    for lock_attempt in range(1, lock_max_attempts + 1):
        try:
            async with (manager or db_manager).get_connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                if recovery_enabled:
                    await db.execute(
                        """
                        UPDATE queue
                        SET status = 'dead',
                            processed_at = CURRENT_TIMESTAMP,
                            processing_started = NULL,
                            lease_until = NULL,
                            lease_token = NULL,
                            next_attempt_at = NULL,
                            last_error = COALESCE(last_error, 'queue lease expired; retry budget exhausted')
                        WHERE status = 'processing'
                          AND lease_until IS NOT NULL
                          AND lease_until <= CURRENT_TIMESTAMP
                          AND attempts >= ?
                        """,
                        (max_attempts,),
                    )
                    await db.execute(
                        """
                        UPDATE queue
                        SET status = 'retry',
                            processing_started = NULL,
                            lease_until = NULL,
                            lease_token = NULL,
                            next_attempt_at = CURRENT_TIMESTAMP,
                            last_error = COALESCE(last_error, 'queue lease expired')
                        WHERE status = 'processing'
                          AND lease_until IS NOT NULL
                          AND lease_until <= CURRENT_TIMESTAMP
                          AND attempts < ?
                        """,
                        (max_attempts,),
                    )

                while True:
                    available_statuses = "('pending', 'retry')" if recovery_enabled else "('pending')"
                    cursor = await db.execute(
                        f"""
                        SELECT id, event, attempts
                        FROM queue
                        WHERE status IN {available_statuses}
                          AND attempts < ?
                          AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)
                        ORDER BY priority DESC, created_at ASC
                        LIMIT 1
                        """,
                        (max_attempts,),
                    )
                    row = await cursor.fetchone()
                    if not row:
                        await db.commit()
                        return None
                    try:
                        event = _decode_queue_event(row[1])
                    except ValueError as error:
                        await db.execute(
                            """
                            UPDATE queue
                            SET status = 'dead',
                                processed_at = CURRENT_TIMESTAMP,
                                last_error = ?,
                                lease_until = NULL,
                                lease_token = NULL,
                                next_attempt_at = NULL
                            WHERE id = ? AND status IN ('pending', 'retry')
                            """,
                            (str(error)[:2000], row[0]),
                        )
                        continue

                    lease_token = uuid.uuid4().hex
                    cursor = await db.execute(
                        """
                        UPDATE queue
                        SET status = 'processing',
                            processed_at = CURRENT_TIMESTAMP,
                            processing_started = CURRENT_TIMESTAMP,
                            lease_until = CASE
                                WHEN ? THEN datetime('now', ?)
                                ELSE NULL
                            END,
                            lease_token = ?,
                            attempts = attempts + 1,
                            next_attempt_at = NULL
                        WHERE id = ? AND status IN ('pending', 'retry')
                        """,
                        (
                            int(recovery_enabled),
                            lease_modifier,
                            lease_token,
                            row[0],
                        ),
                    )
                    if cursor.rowcount != 1:
                        await db.rollback()
                        continue
                    await db.commit()
                    return row[0], event, lease_token
        except Exception as e:
            msg = str(e)
            if "database is locked" in msg or "database is busy" in msg:
                if lock_attempt < lock_max_attempts:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 0.8)
                    continue
            logger.error(f"Ошибка извлечения из очереди: {e}")
            return None
    return None

async def count_pending() -> int:
    """Возвращает количество ожидающих сообщений в очереди."""
    async with db_manager.get_connection() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM queue WHERE status = 'pending'")
        row = await cursor.fetchone()
        return row[0] if row else 0

async def mark_completed(event_id: int, lease_token: str) -> bool:
    """Complete a queue event only while the caller still owns its lease."""
    async with db_manager.get_connection() as db:
        cursor = await db.execute(
            """
            UPDATE queue
            SET status = 'completed',
                processed_at = CURRENT_TIMESTAMP,
                processing_started = NULL,
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = NULL,
                last_error = NULL
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (event_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1


async def mark_queue_retry(
    event_id: int,
    lease_token: str,
    error: str,
    *,
    delay_seconds: int = 5,
    max_attempts: Optional[int] = None,
) -> Optional[str]:
    """Retry a failed queue attempt, or dead-letter it at the attempt budget."""
    if max_attempts is None:
        max_attempts = int(os.getenv("QUEUE_MAX_ATTEMPTS", "5"))
    async with db_manager.get_connection() as db:
        cursor = await db.execute(
            """
            SELECT attempts FROM queue
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (event_id, lease_token),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        status = "dead" if int(row[0]) >= max_attempts else "retry"
        delay_modifier = f"+{max(0, int(delay_seconds))} seconds"
        cursor = await db.execute(
            """
            UPDATE queue
            SET status = ?,
                processed_at = CURRENT_TIMESTAMP,
                processing_started = NULL,
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = CASE
                    WHEN ? = 'dead' THEN NULL
                    ELSE datetime('now', ?)
                END,
                last_error = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (
                status,
                status,
                delay_modifier,
                error[:2000],
                event_id,
                lease_token,
            ),
        )
        await db.commit()
        return status if cursor.rowcount == 1 else None


async def mark_queue_failed(
    event_id: int,
    lease_token: str,
    error: str,
) -> bool:
    """Legacy-mode terminal failure without automatic reclaim."""
    async with db_manager.get_connection() as db:
        cursor = await db.execute(
            """
            UPDATE queue
            SET status = 'failed',
                processed_at = CURRENT_TIMESTAMP,
                processing_started = NULL,
                lease_until = NULL,
                lease_token = NULL,
                next_attempt_at = NULL,
                last_error = ?
            WHERE id = ? AND status = 'processing' AND lease_token = ?
            """,
            (error[:2000], event_id, lease_token),
        )
        await db.commit()
        return cursor.rowcount == 1

async def cleanup_old_messages():
    """Очистка старых обработанных сообщений."""
    try:
        cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
        
        async with db_manager.get_connection() as db:
            # Remove old completed and failed messages
            cursor = await db.execute(
                "DELETE FROM queue WHERE status IN ('completed', 'failed') AND processed_at < ?",
                (cutoff_date.isoformat(),)
            )
            deleted_count = cursor.rowcount
            
            if (
                os.getenv("WRITE_OUTBOX", "0") == "1"
                and os.getenv("DELIVERY_OUTBOX_WORKER", "0") == "1"
            ):
                queue_max_attempts = int(os.getenv("QUEUE_MAX_ATTEMPTS", "5"))
                # Only reclaim rows created by the explicit outbox-mode lease
                # policy. Legacy processing rows have lease_until=NULL.
                await db.execute(
                    """
                    UPDATE queue
                    SET status = CASE
                            WHEN attempts >= ? THEN 'dead'
                            ELSE 'retry'
                        END,
                        processing_started = NULL,
                        lease_until = NULL,
                        lease_token = NULL,
                        next_attempt_at = CASE
                            WHEN attempts >= ? THEN NULL
                            ELSE CURRENT_TIMESTAMP
                        END,
                        last_error = COALESCE(last_error, 'queue lease expired')
                    WHERE status = 'processing'
                      AND lease_until IS NOT NULL
                      AND lease_until <= CURRENT_TIMESTAMP
                    """,
                    (queue_max_attempts, queue_max_attempts),
                )
            
            await db.commit()
            
        if deleted_count > 0:
            logger.info(f"🧹 Очищено {deleted_count} старых сообщений")
    except Exception as e:
        logger.error(f"Ошибка очистки: {e}")

async def get_queue_stats():
    """Получает статистику очереди."""
    async with db_manager.get_connection() as db:
        cursor = await db.execute("""
            SELECT status, COUNT(*) as count 
            FROM queue 
            GROUP BY status
        """)
        stats = {row[0]: row[1] for row in await cursor.fetchall()}
        
        # Get oldest pending message
        cursor = await db.execute(
            "SELECT MIN(created_at) FROM queue WHERE status = 'pending'"
        )
        oldest = await cursor.fetchone()
        
        return {
            "stats": stats,
            "oldest_pending": oldest[0] if oldest and oldest[0] else None,
            "total": sum(stats.values())
        }

async def start_periodic_cleanup():
    """Запускает периодическую очистку очереди."""
    while True:
        try:
            await cleanup_old_messages()
            await asyncio.sleep(CLEANUP_INTERVAL * 3600)  # Convert hours to seconds
        except Exception as e:
            logger.error(f"Ошибка в периодической очистке: {e}")
            await asyncio.sleep(3600)  # Retry in 1 hour

async def restore_queue(target_queue: asyncio.Queue):
    """Загружает все сообщения из БД в очередь при запуске."""
    count = 0
    while True:
        item = await dequeue()
        if not item:
            break
        event_id, event_dict, lease_token = item
        await target_queue.put((event_id, event_dict, lease_token))
        count += 1
    logger.info(f"📥 Восстановлено {count} сообщений из SQLite")
