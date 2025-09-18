#!/usr/bin/env python3
"""
AI Trainer with Feedback Integration
Uses user feedback to improve AI classification accuracy
"""

import json
import os
from typing import List, Dict, Optional
from feedback_manager import feedback_manager
from config import logger

class AITrainer:
    """Improves AI classification using feedback data"""
    
    def __init__(self):
        self.max_examples = 8  # Max feedback examples to include in prompts (было 10, стало 8)
        
    async def get_feedback_examples(self) -> str:
        """Get formatted feedback examples for prompt engineering"""
        try:
            feedback_data = await feedback_manager.get_recent_feedback(self.max_examples * 2)  # Берём больше, чтобы отфильтровать
            
            if not feedback_data:
                return ""
            
            # Разделяем на админские и пользовательские отзывы
            admin_examples = []
            user_examples = []
            
            for item in feedback_data:
                message = item['message_text']
                feedback = item['user_feedback']
                category = item['category']
                region = item['region']
                user_id = item.get('user_id', '')
                
                if user_id == "admin":
                    # Админское решение
                    if feedback == 'useful':
                        admin_examples.append(f"""
Сообщение: "{message}"
Классификация: РЕЛЕВАНТНЫЙ ЛИД (проверено администратором)
Категория: {category}
Регион: {region}
Объяснение: Администратор подтвердил этот лид как релевантный""")
                    else:
                        admin_examples.append(f"""
Сообщение: "{message}"
Классификация: НЕ РЕЛЕВАНТНЫЙ (отклонено администратором)
Объяснение: Администратор отклонил это как нерелевантное""")
                else:
                    # Пользовательский отзыв
                    if feedback == 'useful':
                        user_examples.append(f"""
Сообщение: "{message}"
Классификация: РЕЛЕВАНТНЫЙ ЛИД
Категория: {category}
Регион: {region}
Объяснение: Пользователи отметили этот лид как полезный""")
                    else:
                        user_examples.append(f"""
Сообщение: "{message}"
Классификация: НЕ РЕЛЕВАНТНЫЙ
Объяснение: Пользователи отметили это как не полезный лид""")
            
            # Приоритизируем админские решения (берём больше админских примеров)
            selected_examples = []
            
            # Берём до 5 админских примеров
            selected_examples.extend(admin_examples[:5])
            
            # Дополняем пользовательскими до максимума
            remaining_slots = self.max_examples - len(selected_examples)
            selected_examples.extend(user_examples[:remaining_slots])
            
            if selected_examples:
                return f"""
ПРИМЕРЫ ИЗ ОБРАТНОЙ СВЯЗИ (администратор + пользователи):
{''.join(selected_examples)}

Используй эти примеры для улучшения точности классификации.
ВАЖНО: Решения администратора имеют наивысший приоритет.
""" 
            return ""
            
        except Exception as e:
            logger.error(f"Error getting feedback examples: {e}")
            return ""
    
    async def get_enhanced_system_prompt(self, base_prompt: str) -> str:
        """Enhance system prompt with feedback examples"""
        try:
            feedback_examples = await self.get_feedback_examples()
            
            if feedback_examples:
                enhanced_prompt = f"""{base_prompt}

{feedback_examples}

ВАЖНО: Учитывай примеры обратной связи от администраторов и пользователей для более точной классификации.
Решения администратора являются наиболее авторитетными."""
                return enhanced_prompt
            else:
                return base_prompt
                
        except Exception as e:
            logger.error(f"Error enhancing prompt: {e}")
            return base_prompt
    
    async def get_training_stats(self) -> Dict:
        """Get training and feedback statistics"""
        try:
            stats = await feedback_manager.get_feedback_stats()
            
            # Дополнительная статистика по админским решениям
            admin_stats = await self._get_admin_feedback_stats()
            
            # Calculate training effectiveness
            total_feedback = stats.get('total_feedback', 0)
            useful_rate = 0
            if total_feedback > 0:
                useful_count = stats.get('useful_count', 0)
                useful_rate = round((useful_count / total_feedback) * 100, 1)
            
            return {
                'total_feedback': total_feedback,
                'useful_leads_rate': useful_rate,
                'recent_feedback_7d': stats.get('recent_feedback_7d', 0),
                'training_examples': min(total_feedback, self.max_examples),
                'ai_improvement_enabled': total_feedback >= 5,
                'admin_decisions': admin_stats.get('admin_total', 0),
                'admin_approved': admin_stats.get('admin_approved', 0),
                'admin_rejected': admin_stats.get('admin_rejected', 0)
            }
            
        except Exception as e:
            logger.error(f"Error getting training stats: {e}")
            return {}
    
    async def _get_admin_feedback_stats(self) -> Dict:
        """Получает статистику по админским решениям"""
        try:
            import aiosqlite
            async with aiosqlite.connect(feedback_manager.db_path) as db:
                # Общее количество админских решений
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM feedback 
                    WHERE user_id = 'admin' AND user_feedback IS NOT NULL
                """)
                result = await cursor.fetchone()
                admin_total = result[0] if result else 0
                
                # Одобренные админом
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM feedback 
                    WHERE user_id = 'admin' AND user_feedback = 'useful'
                """)
                result = await cursor.fetchone()
                admin_approved = result[0] if result else 0
                
                # Отклонённые админом
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM feedback 
                    WHERE user_id = 'admin' AND user_feedback = 'not_useful'
                """)
                result = await cursor.fetchone()
                admin_rejected = result[0] if result else 0
                
                return {
                    'admin_total': admin_total,
                    'admin_approved': admin_approved,
                    'admin_rejected': admin_rejected
                }
        except Exception as e:
            logger.error(f"Error getting admin feedback stats: {e}")
            return {}
    
    async def export_training_data(self) -> int:
        """Export feedback data for fine-tuning"""
        try:
            count = await feedback_manager.export_training_data("ai_training_data.jsonl")
            logger.info(f"Exported {count} training examples")
            return count
        except Exception as e:
            logger.error(f"Error exporting training data: {e}")
            return 0
    
    async def should_retrain_model(self) -> bool:
        """Determine if model should be retrained based on feedback"""
        try:
            stats = await self.get_training_stats()
            total_feedback = stats.get('total_feedback', 0)
            useful_rate = stats.get('useful_leads_rate', 0)
            
            # Retrain if we have enough data and low accuracy
            return total_feedback >= 100 and useful_rate < 70
            
        except Exception as e:
            logger.error(f"Error checking retrain status: {e}")
            return False

# Global AI trainer instance
ai_trainer = AITrainer()