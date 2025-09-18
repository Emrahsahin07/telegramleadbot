#!/usr/bin/env python3
"""
db_lock_resolver.py

Lightweight async SQLite connection manager to minimize "database is locked" errors
by serializing write access and setting pragmatic options (WAL, busy_timeout).
Used by feedback_manager.SafeDatabaseManager interface.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator
import aiosqlite
import logging

logger = logging.getLogger(__name__)


class SafeDatabaseManager:
    """
    Provides an async context manager for SQLite connections with:
      - Global async lock to serialize concurrent DB access
      - Pragmas: WAL journal, busy_timeout, sane defaults
    """

    def __init__(self, db_path: str, *, busy_timeout_sec: float = 5.0, connect_timeout_sec: float = 30.0):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        # Allow environment overrides for timeouts
        try:
            import os
            busy_env = float(os.getenv("DB_BUSY_TIMEOUT_SEC", str(busy_timeout_sec)))
            conn_env = float(os.getenv("DB_CONNECT_TIMEOUT_SEC", str(connect_timeout_sec)))
        except Exception:
            busy_env = busy_timeout_sec
            conn_env = connect_timeout_sec
        self._busy_timeout_ms = int(busy_env * 1000)
        self._connect_timeout = conn_env

    async def initialize(self) -> bool:
        """Initialize DB pragmas once. Returns True on success."""
        try:
            async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
                # WAL mode improves concurrency for readers; suitable for our simple writes
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms};")
                await db.execute("PRAGMA synchronous=NORMAL;")
                await db.execute("PRAGMA temp_store=MEMORY;")
                await db.execute("PRAGMA foreign_keys=ON;")
                await db.commit()
            return True
        except Exception as e:
            logger.error(f"DB initialize error: {e}")
            return False

    @asynccontextmanager
    async def get_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """
        Acquire a global lock and yield a configured aiosqlite connection.
        Ensures busy_timeout is set for the connection.
        """
        await self._lock.acquire()
        try:
            async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
                await db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms};")
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute("PRAGMA foreign_keys=ON;")
                yield db
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                # Lock may not be held if an unexpected error happened
                pass
