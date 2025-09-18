#!/usr/bin/env python3
"""
Simple Fine-tuning Manager for OpenAI Models
"""

import os
import json
from typing import Dict, Optional
from openai import OpenAI
from config import logger

class FineTuningManager:
    """Manages OpenAI model fine-tuning process"""
    
    def __init__(self):
        try:
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            self.min_examples_for_tuning = 100
        except Exception as e:
            logger.error(f"Error initializing OpenAI client: {e}")
            self.client = None
        
    async def can_start_fine_tuning(self) -> Dict:
        """Check if we have enough data for fine-tuning"""
        try:
            from feedback_manager import feedback_manager
            stats = await feedback_manager.get_feedback_stats()
            total_feedback = stats.get('total_feedback', 0)
            useful_count = stats.get('useful_count', 0)
            not_useful_count = stats.get('not_useful_count', 0)
            
            ready = (total_feedback >= self.min_examples_for_tuning and 
                    useful_count >= 30 and not_useful_count >= 20)
            
            return {
                'ready': ready,
                'total_examples': total_feedback,
                'positive_examples': useful_count,
                'negative_examples': not_useful_count,
                'min_required': self.min_examples_for_tuning,
                'recommendation': self._get_recommendation(total_feedback, useful_count, not_useful_count)
            }
            
        except Exception as e:
            logger.error(f"Error checking fine-tuning readiness: {e}")
            return {'ready': False, 'error': str(e)}
    
    def _get_recommendation(self, total: int, positive: int, negative: int) -> str:
        """Get recommendation based on current data"""
        if total < 50:
            return "Продолжайте собирать feedback. Нужно минимум 100 примеров."
        elif total < 100:
            return f"Почти готово! Собрано {total}/100 примеров."
        elif positive < 30:
            return f"Нужно больше положительных примеров: {positive}/30"
        elif negative < 20:
            return f"Нужно больше отрицательных примеров: {negative}/20"
        else:
            return "✅ Готово к fine-tuning!"

    def check_fine_tuning_status(self, job_id: str) -> Dict:
        """Check status of fine-tuning job"""
        try:
            if not self.client:
                return {'error': 'OpenAI client not initialized'}
                
            job = self.client.fine_tuning.jobs.retrieve(job_id)
            return {
                'id': job.id,
                'status': job.status,
                'model': job.fine_tuned_model,
                'created_at': job.created_at,
                'finished_at': job.finished_at,
                'training_file': job.training_file,
                'result_files': job.result_files
            }
            
        except Exception as e:
            logger.error(f"Error checking fine-tuning status: {e}")
            return {'error': str(e)}

    async def full_fine_tuning_process(self) -> Dict:
        """Placeholder for full fine-tuning process"""
        try:
            readiness = await self.can_start_fine_tuning()
            if not readiness['ready']:
                return {
                    'success': False,
                    'message': readiness.get('recommendation', 'Not ready for fine-tuning'),
                    'data': readiness
                }
            
            return {
                'success': False,
                'message': 'Fine-tuning implementation in progress - use external tools for now'
            }
            
        except Exception as e:
            logger.error(f"Error in fine-tuning process: {e}")
            return {'success': False, 'message': f'Error: {e}'}

# Global instance
fine_tuning_manager = FineTuningManager()