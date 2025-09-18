#!/usr/bin/env python3
"""
Feedback Management System
Collects user feedback on lead quality for AI training
"""

import sqlite3
import json
import os
import asyncio
import aiosqlite
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from config import logger
from db_lock_resolver import SafeDatabaseManager

DB_PATH = "feedback.db"

class FeedbackManager:
    """Manages feedback collection and AI training data"""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.db_manager = SafeDatabaseManager(db_path)
        
    async def init_db(self):
        """Initialize feedback database schema"""
        
        # Initialize safe database manager first
        if not await self.db_manager.initialize():
            logger.error("❌ Failed to initialize feedback database manager")
            raise RuntimeError("Feedback database initialization failed")
        
        async with self.db_manager.get_connection() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE,
                    user_id TEXT,
                    message_text TEXT,
                    ai_classification TEXT,  -- JSON string
                    user_feedback TEXT,      -- 'useful' or 'not_useful'
                    category TEXT,
                    region TEXT,
                    confidence FLOAT,
                    timestamp DATETIME,
                    processed BOOLEAN DEFAULT FALSE
                )
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback(user_id)
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_timestamp ON feedback(timestamp)
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_processed ON feedback(processed)
            """)
            
            await db.commit()
            logger.info("✅ Feedback database initialized")

    async def store_lead_sent(self, message_id: str, user_id: str, message_text: str, 
                             ai_classification: dict, category: str, region: str, 
                             confidence: float):
        """Store lead data when sent to user (before feedback)"""
        try:
            async with self.db_manager.get_connection() as db:
                await db.execute("""
                    INSERT OR REPLACE INTO feedback 
                    (message_id, user_id, message_text, ai_classification, 
                     category, region, confidence, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    message_id,
                    user_id,
                    message_text,
                    json.dumps(ai_classification, ensure_ascii=False),
                    category,
                    region,
                    confidence,
                    datetime.now(timezone.utc).isoformat()
                ))
                await db.commit()
                logger.debug(f"Stored lead data for feedback: {message_id}")
        except Exception as e:
            logger.error(f"Error storing lead data: {e}")

    async def record_feedback(self, message_id: str, feedback: str) -> bool:
        """Record user feedback on a lead"""
        try:
            async with self.db_manager.get_connection() as db:
                cursor = await db.execute("""
                    UPDATE feedback 
                    SET user_feedback = ?, processed = FALSE
                    WHERE message_id = ?
                """, (feedback, message_id))
                
                if cursor.rowcount > 0:
                    await db.commit()
                    logger.info(f"Recorded feedback: {message_id} -> {feedback}")
                    return True
                else:
                    logger.warning(f"No lead found for feedback: {message_id}")
                    return False
        except Exception as e:
            logger.error(f"Error recording feedback: {e}")
            return False

    async def get_recent_feedback(self, limit: int = 20) -> List[Dict]:
        """Get recent feedback examples for prompt engineering"""
        try:
            async with self.db_manager.get_connection() as db:
                cursor = await db.execute("""
                    SELECT message_text, ai_classification, user_feedback, 
                           category, region, confidence, user_id
                    FROM feedback 
                    WHERE user_feedback IS NOT NULL
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (limit,))
                
                rows = await cursor.fetchall()
                feedback_data = []
                
                for row in rows:
                    try:
                        ai_classification = json.loads(row[1]) if row[1] else {}
                        feedback_data.append({
                            'message_text': row[0],
                            'ai_classification': ai_classification,
                            'user_feedback': row[2],
                            'category': row[3],
                            'region': row[4],
                            'confidence': row[5],
                            'user_id': row[6] if len(row) > 6 else ''
                        })
                    except json.JSONDecodeError:
                        continue
                
                return feedback_data
        except Exception as e:
            logger.error(f"Error getting recent feedback: {e}")
            return []

    async def get_feedback_stats(self) -> Dict:
        """Get feedback statistics"""
        try:
            async with self.db_manager.get_connection() as db:
                # Total feedback count
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM feedback WHERE user_feedback IS NOT NULL
                """)
                total_feedback = (await cursor.fetchone())[0]
                
                # Useful vs not useful
                cursor = await db.execute("""
                    SELECT user_feedback, COUNT(*) 
                    FROM feedback 
                    WHERE user_feedback IS NOT NULL 
                    GROUP BY user_feedback
                """)
                feedback_breakdown = dict(await cursor.fetchall())
                
                # Recent feedback (last 7 days)
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM feedback 
                    WHERE user_feedback IS NOT NULL 
                    AND datetime(timestamp) > datetime('now', '-7 days')
                """)
                recent_feedback = (await cursor.fetchone())[0]
                
                return {
                    'total_feedback': total_feedback,
                    'useful_count': feedback_breakdown.get('useful', 0),
                    'not_useful_count': feedback_breakdown.get('not_useful', 0),
                    'recent_feedback_7d': recent_feedback,
                    'feedback_rate': round(total_feedback / max(1, total_feedback) * 100, 2)
                }
        except Exception as e:
            logger.error(f"Error getting feedback stats: {e}")
            return {}

    async def export_training_data(self, output_file: str = "training_data.jsonl"):
        """Export feedback data in format suitable for fine-tuning"""
        try:
            async with self.db_manager.get_connection() as db:
                cursor = await db.execute("""
                    SELECT message_text, ai_classification, user_feedback, 
                           category, region
                    FROM feedback 
                    WHERE user_feedback IS NOT NULL
                    ORDER BY timestamp DESC
                """)
                
                rows = await cursor.fetchall()
                training_examples = []
                
                for row in rows:
                    message_text = row[0]
                    user_feedback = row[2]
                    category = row[3]
                    region = row[4]
                    
                    # Create training example
                    is_relevant = user_feedback == 'useful'
                    
                    training_example = {
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a lead classification AI. Classify messages as relevant leads or not."
                            },
                            {
                                "role": "user", 
                                "content": f"Classify this message: {message_text}"
                            },
                            {
                                "role": "assistant",
                                "content": json.dumps({
                                    "relevant": is_relevant,
                                    "category": category if is_relevant else None,
                                    "region": region if is_relevant else None,
                                    "confidence": 0.95 if is_relevant else 0.05
                                }, ensure_ascii=False)
                            }
                        ]
                    }
                    training_examples.append(training_example)
                
                # Write to JSONL file
                with open(output_file, 'w', encoding='utf-8') as f:
                    for example in training_examples:
                        f.write(json.dumps(example, ensure_ascii=False) + '\n')
                
                logger.info(f"Exported {len(training_examples)} training examples to {output_file}")
                return len(training_examples)
                
        except Exception as e:
            logger.error(f"Error exporting training data: {e}")
            return 0

# Global feedback manager instance
feedback_manager = FeedbackManager()