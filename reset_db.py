#!/usr/bin/env python3
"""
Simple script to reset the database with the correct schema
"""
import asyncio
import os
import aiosqlite

DB_PATH = os.getenv("QUEUE_DB", "queue.db")

async def reset_database():
    """Completely reset the database with correct schema"""
    try:
        # Remove old database if it exists
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
            print(f"🗑️ Removed old database: {DB_PATH}")
        
        # Create new database with correct schema
        async with aiosqlite.connect(DB_PATH) as db:
            # Create table with correct schema
            await db.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP NULL,
                    priority INTEGER DEFAULT 0
                )
            """)
            
            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON queue(status);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON queue(created_at);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_priority_status ON queue(priority DESC, status, created_at);")
            
            # SQLite optimizations
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA cache_size=10000;")
            await db.execute("PRAGMA temp_store=MEMORY;")
            await db.execute("PRAGMA mmap_size=268435456;")  # 256MB
            
            await db.commit()
            
        print("✅ Database reset successfully with correct schema")
        return True
        
    except Exception as e:
        print(f"❌ Error resetting database: {e}")
        return False

if __name__ == "__main__":
    result = asyncio.run(reset_database())
    if result:
        print("🎉 Database is ready! You can now start the bot.")
    else:
        print("💥 Database reset failed!")