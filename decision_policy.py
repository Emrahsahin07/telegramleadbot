from dataclasses import dataclass


SHADOW_BORDERLINE_THRESHOLD = 0.70
AUTO_SEND_THRESHOLD = 0.79
POLICY_VERSION = "automatic-v1"

AUTO_SEND = "auto_send"
REJECT = "reject"
SHADOW_BORDERLINE = "shadow_borderline"


@dataclass(frozen=True)
class LeadDecision:
    decision: str
    reason: str


def decide_lead(relevant: bool, calibrated_confidence: float) -> LeadDecision:
    """Apply the automatic policy after a valid AI business result."""
    confidence = float(calibrated_confidence)
    if relevant is not True:
        return LeadDecision(REJECT, "ai_not_relevant")
    if confidence < SHADOW_BORDERLINE_THRESHOLD:
        return LeadDecision(REJECT, "confidence_below_shadow_threshold")
    if confidence < AUTO_SEND_THRESHOLD:
        return LeadDecision(
            SHADOW_BORDERLINE,
            "confidence_below_auto_send_threshold",
        )
    return LeadDecision(AUTO_SEND, "confidence_at_or_above_auto_send_threshold")
