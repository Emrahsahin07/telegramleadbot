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
from tests.helpers import (
    RecordingBot,
    active_prefs,
    configure_temp_queue,
    create_queue_event,
    expired_trial_prefs,
    lead_kwargs,
    make_outbox_retries_due,
)


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

    result = await delivery.send_lead_to_users(**lead_kwargs())

    assert result.delivered_uids == [202]
    assert result.failed_uids == [101]
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

    result = await delivery.send_lead_to_users(**lead_kwargs())

    assert result.delivered_uids == [202]
    assert result.failed_uids == []
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


@pytest.mark.correct
async def test_pass_successful_recipient_is_not_duplicated_on_partial_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """PASS: durable recipient state prevents duplicate delivery after retry."""

    bot = RecordingBot({101: [TimeoutError("temporary Telegram failure")]})
    configure_delivery(
        monkeypatch,
        bot,
        {"101": active_prefs(), "202": active_prefs()},
    )
    await configure_temp_queue(monkeypatch, tmp_path)
    event_id = await create_queue_event()
    monkeypatch.setenv("WRITE_OUTBOX", "1")
    monkeypatch.setenv("DELIVERY_OUTBOX_WORKER", "1")
    kwargs = lead_kwargs()
    kwargs["event_id"] = event_id

    routed = await delivery.send_lead_to_users(**kwargs)
    assert routed.queued_uids == [101, 202]
    assert bot.calls == []

    assert await delivery.deliver_next_outbox(event_id) == ("retry", 101)
    assert await delivery.deliver_next_outbox(event_id) == ("delivered", 202)
    await make_outbox_retries_due(event_id)
    rerouted = await delivery.send_lead_to_users(**kwargs)
    assert rerouted.queued_uids == []
    assert await delivery.deliver_next_outbox(event_id) == ("delivered", 101)
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
