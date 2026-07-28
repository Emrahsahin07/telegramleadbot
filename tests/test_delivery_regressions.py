from __future__ import annotations

from collections import Counter
from unittest.mock import AsyncMock

import pytest
from telethon.errors import (
    InputUserDeactivatedError,
    PeerIdInvalidError,
    UserIsBlockedError,
)

import delivery
from tests.helpers import RecordingBot, active_prefs, expired_trial_prefs, lead_kwargs


def configure_delivery(monkeypatch: pytest.MonkeyPatch, bot: RecordingBot, users: dict) -> None:
    monkeypatch.setattr(delivery, "bot_client", bot)
    monkeypatch.setattr(delivery, "subscriptions", users)
    monkeypatch.setattr(
        delivery,
        "categories",
        {"трансфер": {"keywords": ["трансфер"], "subcategories": {}}},
    )
    monkeypatch.setattr(delivery, "metrics", Counter())
    monkeypatch.setattr(delivery, "save_subscriptions", lambda: None)
    monkeypatch.setattr(
        delivery.feedback_manager,
        "store_lead_sent",
        AsyncMock(return_value=None),
    )


@pytest.mark.correct
@pytest.mark.parametrize(
    "recipient_error",
    [
        pytest.param(UserIsBlockedError(request=None), id="user-blocked"),
        pytest.param(InputUserDeactivatedError(request=None), id="user-deactivated"),
        pytest.param(PeerIdInvalidError(request=None), id="peer-invalid"),
    ],
)
async def test_pass_recipient_error_does_not_stop_remaining_delivery(
    monkeypatch: pytest.MonkeyPatch,
    recipient_error: Exception,
) -> None:
    """PASS: a failure for recipient 101 does not prevent recipient 202."""

    bot = RecordingBot({101: [recipient_error]})
    configure_delivery(
        monkeypatch,
        bot,
        {"101": active_prefs(), "202": active_prefs()},
    )

    sent, failed = await delivery.send_lead_to_users(**lead_kwargs())

    assert sent == [202]
    assert failed == [101]
    assert bot.calls == [101, 202]


@pytest.mark.correct
async def test_pass_blocked_user_during_trial_notice_does_not_stop_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PASS: UserIsBlockedError in the trial-expiry path is isolated."""

    bot = RecordingBot({101: [UserIsBlockedError(request=None)]})
    configure_delivery(
        monkeypatch,
        bot,
        {"101": expired_trial_prefs(), "202": active_prefs()},
    )

    sent, failed = await delivery.send_lead_to_users(**lead_kwargs())

    assert sent == [202]
    assert failed == []
    assert bot.calls == [101, 202]


@pytest.mark.known_bug
@pytest.mark.parametrize(
    "recipient_error",
    [
        pytest.param(InputUserDeactivatedError(request=None), id="user-deactivated"),
        pytest.param(PeerIdInvalidError(request=None), id="peer-invalid"),
    ],
)
@pytest.mark.xfail(
    strict=True,
    raises=(InputUserDeactivatedError, PeerIdInvalidError),
    reason=(
        "Known bug: the trial-expiry notification path handles UserIsBlockedError "
        "but lets deactivated/invalid-peer errors abort delivery to later recipients"
    ),
)
async def test_xfail_trial_notice_error_does_not_stop_remaining_delivery(
    monkeypatch: pytest.MonkeyPatch,
    recipient_error: Exception,
) -> None:
    """XFAIL: permanent errors in trial notices must be recipient-local."""

    bot = RecordingBot({101: [recipient_error]})
    configure_delivery(
        monkeypatch,
        bot,
        {"101": expired_trial_prefs(), "202": active_prefs()},
    )

    await delivery.send_lead_to_users(**lead_kwargs())

    assert 202 in bot.calls


@pytest.mark.known_bug
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Known bug: retrying a partially delivered lead resends it to recipients "
        "that already succeeded because there is no per-recipient idempotency state"
    ),
)
async def test_xfail_successful_recipient_is_not_duplicated_on_partial_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XFAIL: recipient 202 must not receive the same event twice."""

    bot = RecordingBot({101: [TimeoutError("temporary Telegram failure")]})
    configure_delivery(
        monkeypatch,
        bot,
        {"101": active_prefs(), "202": active_prefs()},
    )

    first_sent, first_failed = await delivery.send_lead_to_users(**lead_kwargs())
    second_sent, second_failed = await delivery.send_lead_to_users(**lead_kwargs())

    assert first_sent == [202]
    assert first_failed == [101]
    assert second_sent == [101]
    assert second_failed == []
    assert bot.calls.count(202) == 1


@pytest.mark.known_bug
@pytest.mark.parametrize(
    ("bad_field", "bad_value"),
    [
        pytest.param("subscription_end", "not-a-date", id="subscription-end"),
        pytest.param("trial_start", "not-a-date", id="trial-start"),
    ],
)
@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason=(
        "Known bug: malformed subscription timestamps raise before the delivery "
        "loop can continue to healthy recipients"
    ),
)
async def test_xfail_malformed_subscription_does_not_block_other_recipients(
    monkeypatch: pytest.MonkeyPatch,
    bad_field: str,
    bad_value: str,
) -> None:
    """XFAIL: invalid state for recipient 101 must be isolated from recipient 202."""

    malformed = {
        "categories": ["трансфер"],
        "locations": ["Анталия"],
        "subcats": {},
        bad_field: bad_value,
    }
    bot = RecordingBot()
    configure_delivery(
        monkeypatch,
        bot,
        {"101": malformed, "202": active_prefs()},
    )

    await delivery.send_lead_to_users(**lead_kwargs())

    assert 202 in bot.calls
