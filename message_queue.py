# queue.py
import aiosqlite
import json
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Union, Optional, Dict, Any, Tuple
from db_lock_resolver import SafeDatabaseManager

DB_PATH = os.getenv("QUEUE_DB", "queue.db")
logger = logging.getLogger("queue")

# Initialize safe database manager
db_manager = SafeDatabaseManager(DB_PATH)

# Database optimization settings
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "10000"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL_HOURS", "24"))  # hours
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))  # days

async def init_db():
    """Создаёт/мигрирует таблицу очереди с оптимизированной схемой и индексами."""

    # Initialize safe database manager first
    if not await db_manager.initialize():
        logger.error("❌ Failed to initialize database manager")
        raise RuntimeError("Database initialization failed")

    async with db_manager.get_connection() as db:
        # Ensure table exists (no-op if already created with old schema)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP NULL,
                priority INTEGER DEFAULT 0
            )
            """
        )

        # MIGRATION: Add missing columns for legacy databases
        try:
            cur = await db.execute("PRAGMA table_info(queue)")
            cols = {row[1] for row in await cur.fetchall()}  # row[1] is column name
        except Exception:
            cols = set()

        # Add columns if they are missing
        if "status" not in cols:
            await db.execute("ALTER TABLE queue ADD COLUMN status TEXT DEFAULT 'pending'")
        if "created_at" not in cols:
            await db.execute("ALTER TABLE queue ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        if "processed_at" not in cols:
            await db.execute("ALTER TABLE queue ADD COLUMN processed_at TIMESTAMP NULL")
        if "priority" not in cols:
            await db.execute("ALTER TABLE queue ADD COLUMN priority INTEGER DEFAULT 0")

        # Create indexes for efficient querying (safe if columns now exist)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON queue(status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON queue(created_at);")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_priority_status ON queue(priority DESC, status, created_at);"
        )

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
                    "INSERT INTO queue (event, priority, status) VALUES (?, ?, 'pending')",
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

async def dequeue() -> Union[Tuple[int, Dict[str, Any]], None]:
    """Извлекает одно событие, возвращая (queue_id, payload) и помечая его processing.
    Использует RETURNING, если доступно; иначе — оптимистичную схему. С ретраями при lock.
    """
    max_attempts = 6
    backoff = 0.05
    for attempt in range(1, max_attempts + 1):
        try:
            async with db_manager.get_connection() as db:
                # Preferred: single-statement update with returning
                try:
                    cursor = await db.execute(
                        """
                        WITH next AS (
                            SELECT id FROM queue
                            WHERE status = 'pending'
                            ORDER BY priority DESC, created_at ASC
                            LIMIT 1
                        )
                        UPDATE queue
                        SET status = 'processing', processed_at = CURRENT_TIMESTAMP
                        WHERE id = (SELECT id FROM next)
                        RETURNING id, event
                        """
                    )
                    row = await cursor.fetchone()
                    await db.commit()
                    if row:
                        row_id, event_str = row
                        return row_id, json.loads(event_str)
                    return None
                except Exception:
                    # Fallback if RETURNING unsupported
                    cursor = await db.execute(
                        "SELECT id, event FROM queue WHERE status = 'pending' ORDER BY priority DESC, created_at ASC LIMIT 1"
                    )
                    row = await cursor.fetchone()
                    if not row:
                        await db.commit()
                        return None
                    msg_id, event_str = row
                    cursor2 = await db.execute(
                        "UPDATE queue SET status = 'processing', processed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
                        (msg_id,)
                    )
                    await db.commit()
                    if cursor2.rowcount:
                        return msg_id, json.loads(event_str)
                    # Another worker took it; retry
                    continue
        except Exception as e:
            msg = str(e)
            if "database is locked" in msg or "database is busy" in msg:
                if attempt < max_attempts:
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

async def mark_completed(event_id: int):
    """Отмечает сообщение как обработанное."""
    async with db_manager.get_connection() as db:
        await db.execute(
            "UPDATE queue SET status = 'completed', processed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (event_id,)
        )
        await db.commit()

async def mark_failed(event_id: int, error: Optional[str] = None):
    """Отмечает сообщение как неудачное."""
    async with db_manager.get_connection() as db:
        await db.execute(
            "UPDATE queue SET status = 'failed', processed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (event_id,)
        )
        await db.commit()
    if error:
        logger.error(f"queue: event_id={event_id} marked failed: {error}")

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
            
            # Reset stuck processing messages (older than 1 hour)
            stuck_cutoff = datetime.now() - timedelta(hours=1)
            await db.execute(
                "UPDATE queue SET status = 'pending', processed_at = NULL WHERE status = 'processing' AND processed_at < ?",
                (stuck_cutoff.isoformat(),)
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
        event_id, event_dict = item
        await target_queue.put((event_id, event_dict))
        count += 1
    logger.info(f"📥 Восстановлено {count} сообщений из SQLite")
