#!/usr/bin/env python3
"""Deterministic lead-quality baseline using the real production pipeline.

The AI call is replaced by the corpus label so the report measures deterministic
filters, overrides, location/category matching, decision policy, and routing.
"""

from __future__ import annotations

import atexit
import asyncio
from collections import Counter
import json
import logging
import logging.handlers
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Callable

import telethon


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class NoopTelegramClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._connected = False

    def on(self, *_args: Any, **_kwargs: Any) -> Callable:
        return lambda func: func

    def is_connected(self) -> bool:
        return self._connected


class EvaluationBot:
    flood_sleep_threshold = 0

    async def get_me(self):
        return SimpleNamespace(id=999001)


class EvaluationEvent:
    def __init__(self, case: dict[str, Any], message_id: int) -> None:
        self.id = message_id
        # Each corpus item is an independent synthetic chat, preventing region
        # cache state from leaking between evaluation examples.
        self.chat_id = -1000000000000 - message_id
        self.sender_id = 777
        self.raw_text = case["text"]
        self.is_group = True
        self.is_channel = False
        self._case = case

    async def get_chat(self):
        return SimpleNamespace(
            title=self._case.get("title", "Общий чат"),
            username="synthetic_group",
        )

    async def get_sender(self):
        return SimpleNamespace(
            id=self.sender_id,
            first_name="Synthetic sender",
            username="synthetic_sender",
            bot=False,
        )


TERMINAL_METRICS = (
    "negative_ctx_filtered",
    "dedup_text",
    "pre_ad_filtered",
    "no_region",
    "no_regional_keyword_match",
    "no_category_match",
    "pre_offer_filtered",
    "pre_review_filtered",
    "no_global_keyword_match",
    "pre_no_trigger_filtered",
    "ai_dropped",
    "discarded_low_confidence",
    "ai_no_category",
    "ai_cat_no_kw_match",
    "no_subscribers_for_region",
    "pref_region_skipped",
    "pref_ai_category_skipped",
    "pref_ai_subcategory_skipped",
    "pref_category_skipped",
)


def _safe_imports():
    telethon.TelegramClient = NoopTelegramClient
    logging.handlers.RotatingFileHandler = (
        lambda *_args, **_kwargs: logging.NullHandler()
    )
    os.environ.setdefault("API_ID", "12345")
    os.environ.setdefault("API_HASH", "evaluation-api-hash")
    os.environ.setdefault("LEADBOT_TOKEN", "12345:evaluation-token")
    os.environ.setdefault("OPENAI_API_KEY", "evaluation-openai-key")
    os.environ["TARGET_BOT_ID"] = "999001"
    os.environ["CLEAR_QUEUE_ON_START"] = "0"
    os.environ["SEND_NOTIFICATIONS"] = "1"
    os.environ["NOTIFY_SEND_ERRORS"] = "0"
    os.environ["WRITE_OUTBOX"] = "1"
    os.environ["DELIVERY_OUTBOX_WORKER"] = "1"

    import Botparsing
    import delivery
    import message_queue
    from db_lock_resolver import SafeDatabaseManager

    atexit.unregister(Botparsing.dump_metrics)
    return Botparsing, delivery, message_queue, SafeDatabaseManager


async def evaluate() -> dict[str, Any]:
    Botparsing, delivery, message_queue, SafeDatabaseManager = _safe_imports()
    corpus = json.loads(
        (ROOT / "evaluation" / "lead_quality_corpus.json").read_text(
            encoding="utf-8"
        )
    )
    categories = json.loads((ROOT / "categories.json").read_text(encoding="utf-8"))
    all_locations = list(Botparsing.CANONICAL_LOCATIONS)
    subscriptions = {
        "101": {
            "subscription_end": "2099-01-01T00:00:00+00:00",
            "categories": list(categories),
            "locations": all_locations,
            "subcats": {},
        }
    }

    with tempfile.TemporaryDirectory(prefix="lead-quality-baseline-") as temp_dir:
        os.chdir(temp_dir)
        db_path = str(Path(temp_dir) / "queue.db")
        message_queue.db_manager = SafeDatabaseManager(db_path)
        message_queue.DB_PATH = db_path
        await message_queue.init_db()

        shared_metrics = Counter()
        Botparsing.metrics = shared_metrics
        delivery.metrics = shared_metrics
        Botparsing.categories = categories
        delivery.categories = categories
        Botparsing.subscriptions = subscriptions
        delivery.subscriptions = subscriptions
        delivery.bot_client = EvaluationBot()
        delivery.save_subscriptions = lambda: None
        Botparsing.REGION_CACHE.clear()
        Botparsing.TOP_KEYWORD_STEMS = {
            Botparsing._stem(token)
            for entry in categories.values()
            for keyword in entry.get("keywords", [])
            for token in Botparsing.WORD_RE.findall(str(keyword).lower())
        }

        results = []
        for index, case in enumerate(corpus, start=1):
            ai_called = False
            oracle_payload = None

            async def oracle(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                nonlocal ai_called, oracle_payload
                ai_called = True
                confidence = float(case["confidence"])
                oracle_payload = {
                    "relevant": bool(case["relevant"]),
                    "category": case.get("category"),
                    "subcategory": case.get("subcategory"),
                    "region": (case.get("location") or [None])[0],
                    "explanation": "synthetic oracle label",
                    "raw_confidence": confidence,
                    "confidence": confidence,
                }
                return oracle_payload

            Botparsing._classify_message_with_ai = oracle
            before = Counter(shared_metrics)
            event = EvaluationEvent(case, index)
            await Botparsing.process_message(event)
            event_id = message_queue.build_delivery_event_id(event.chat_id, event.id)
            outbox_rows = await message_queue.get_delivery_outbox_rows(event_id)
            shadow = await message_queue.get_shadow_decision(event_id)
            delta = shared_metrics - before

            if outbox_rows:
                actual_decision = "auto_send"
                stage = "delivery_outbox"
            elif shadow:
                actual_decision = "shadow_borderline"
                stage = f"shadow:{shadow['reason']}"
            else:
                actual_decision = "reject"
                stage = next(
                    (name for name in TERMINAL_METRICS if delta.get(name, 0)),
                    "reject_without_terminal_metric",
                )
            if oracle_payload is not None and (
                bool(oracle_payload.get("relevant")) != bool(case["relevant"])
            ):
                stage = (
                    "post_ai_override:"
                    + str(oracle_payload.get("explanation", "relevance_changed"))
                )

            expected = case["expected_decision"]
            rank = {"reject": 0, "shadow_borderline": 1, "auto_send": 2}
            if actual_decision == expected:
                classification = "correct"
            elif rank[actual_decision] > rank[expected]:
                classification = "false_positive"
            else:
                classification = "false_negative"

            results.append(
                {
                    "id": case["id"],
                    "expected_decision": expected,
                    "actual_decision": actual_decision,
                    "classification": classification,
                    "expected_ai": bool(case["should_reach_ai"]),
                    "actual_ai": ai_called,
                    "stage": stage,
                }
            )

    counts = Counter(row["classification"] for row in results)
    fn_stages = Counter(
        row["stage"] for row in results if row["classification"] == "false_negative"
    )
    fp_stages = Counter(
        row["stage"] for row in results if row["classification"] == "false_positive"
    )
    ai_reach_mismatches = [
        row["id"] for row in results if row["expected_ai"] != row["actual_ai"]
    ]
    return {
        "corpus_size": len(corpus),
        "counts": dict(counts),
        "false_negative_stages": dict(fn_stages.most_common()),
        "false_positive_stages": dict(fp_stages.most_common()),
        "ai_reach_mismatches": ai_reach_mismatches,
        "results": results,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(evaluate()), ensure_ascii=False, indent=2))
