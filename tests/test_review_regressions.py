from __future__ import annotations

import review_handler
from decision_policy import SHADOW_BORDERLINE, decide_lead


def test_pass_borderline_no_longer_requires_manual_approval() -> None:
    """Manual callback code remains available but is outside automatic policy."""
    decision = decide_lead(True, 0.75)

    assert decision.decision == SHADOW_BORDERLINE
    assert callable(review_handler.handle_review_callback)
