from __future__ import annotations

import pytest

import delivery
from tests.helpers import lead_kwargs


@pytest.mark.correct
@pytest.mark.parametrize(
    ("write_outbox", "outbox_worker", "expected"),
    [
        ("0", "0", "legacy"),
        ("1", "1", "outbox"),
        ("0", "1", None),
        ("1", "0", None),
    ],
)
def test_pass_startup_feature_flag_combinations(
    monkeypatch: pytest.MonkeyPatch,
    write_outbox: str,
    outbox_worker: str,
    expected: str | None,
) -> None:
    monkeypatch.setenv("WRITE_OUTBOX", write_outbox)
    monkeypatch.setenv("DELIVERY_OUTBOX_WORKER", outbox_worker)

    if expected is None:
        with pytest.raises(delivery.StartupConfigurationError):
            delivery.validate_delivery_mode()
    else:
        assert delivery.validate_delivery_mode() == expected


@pytest.mark.correct
async def test_pass_outbox_mode_requires_event_id_before_any_telegram_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WRITE_OUTBOX", "1")
    monkeypatch.setenv("DELIVERY_OUTBOX_WORKER", "1")

    with pytest.raises(ValueError, match="event_id is required"):
        await delivery.send_lead_to_users(**lead_kwargs())


@pytest.mark.correct
def test_pass_invalid_lease_timeout_relationship_is_startup_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WRITE_OUTBOX", "1")
    monkeypatch.setenv("DELIVERY_OUTBOX_WORKER", "1")
    monkeypatch.setenv("DELIVERY_OUTBOX_LEASE_SECONDS", "10")
    monkeypatch.setenv("DELIVERY_SEND_TIMEOUT_SECONDS", "10")

    with pytest.raises(delivery.StartupConfigurationError):
        delivery.validate_delivery_mode()


@pytest.mark.correct
def test_pass_outbox_runtime_disables_telethon_flood_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        flood_sleep_threshold = 60

    client = Client()
    monkeypatch.setattr(delivery, "bot_client", client)

    delivery.configure_delivery_runtime("outbox")

    assert client.flood_sleep_threshold == 0
