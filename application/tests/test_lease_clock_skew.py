"""Fixed small issuance skew only; no system-clock changes or live requests."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_offline_security import ENDPOINT, PASSWORD, USER, lease

from ollama_chat_app.services import offline_access
from ollama_chat_app.services.cloud_client import CloudAPIClient, CloudAPIError
from ollama_chat_app.services.offline_access import OfflineAccessError, OfflineAccessStore

NOW = datetime(2026, 9, 9, 7, 50, 56, 806812, tzinfo=UTC)


@pytest.mark.parametrize(
    "offset", [timedelta(0), timedelta(microseconds=61332), timedelta(seconds=5)]
)
def test_issuance_skew_up_to_five_seconds_returns_unchanged_lease(offset):
    original = lease(NOW + offset)
    accepted = offline_access.validate_lease(original, actor_id=1, now=NOW)
    assert accepted == original and accepted is not original
    assert offline_access.ISSUED_AT_CLOCK_SKEW_SECONDS == 5


@pytest.mark.parametrize(
    "offset", [timedelta(seconds=5, microseconds=1), timedelta(seconds=6), timedelta(minutes=1)]
)
def test_issuance_beyond_fixed_bound_fails(offset):
    with pytest.raises(OfflineAccessError):
        offline_access.validate_lease(lease(NOW + offset), actor_id=1, now=NOW)


@pytest.mark.parametrize("expiry_delta", [timedelta(0), timedelta(microseconds=-1)])
def test_expiry_equality_or_past_is_never_given_skew_grace(expiry_delta):
    original = lease(NOW - timedelta(hours=1), expires_at=(NOW + expiry_delta).isoformat())
    with pytest.raises(OfflineAccessError):
        offline_access.validate_lease(original, actor_id=1, now=NOW)


def test_expiry_one_microsecond_ahead_is_not_rewritten():
    original = lease(
        NOW - timedelta(hours=1), expires_at=(NOW + timedelta(microseconds=1)).isoformat()
    )
    assert offline_access.validate_lease(original, now=NOW) == original


@pytest.mark.parametrize("issued_offset", [timedelta(0), timedelta(seconds=5)])
def test_server_issuance_interval_cannot_exceed_twelve_hours(issued_offset):
    issued = NOW + issued_offset
    original = lease(issued, expires_at=(issued + timedelta(hours=12)).isoformat())
    assert offline_access.validate_lease(original, now=NOW) == original
    original["expires_at"] = (issued + timedelta(hours=12, microseconds=1)).isoformat()
    with pytest.raises(OfflineAccessError):
        offline_access.validate_lease(original, now=NOW)


@pytest.mark.parametrize("duration", [timedelta(0), timedelta(microseconds=-1)])
def test_expiry_must_still_follow_issuance_even_inside_skew_window(duration):
    issued = NOW + timedelta(seconds=5)
    original = lease(issued, expires_at=(issued + duration).isoformat())
    with pytest.raises(OfflineAccessError):
        offline_access.validate_lease(original, now=NOW)


@pytest.fixture
def fixed_validation_clock(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(offline_access, "datetime", FixedDateTime)
    monkeypatch.setattr(CloudAPIClient, "_on_gui_thread", staticmethod(lambda: False))


@pytest.mark.parametrize("remember", [False, True])
@pytest.mark.parametrize("offset", [timedelta(microseconds=61332), timedelta(seconds=5)])
def test_real_client_login_accepts_small_skew_without_changing_expiry(
    fixed_validation_clock, tmp_path, remember, offset
):
    original = lease(NOW + offset)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == "/aliyun/v1/auth/login"
        return httpx.Response(
            200,
            json={
                "user": USER,
                "token_type": "bearer",
                "access_token": "synthetic-only-token-12345",
                "offline_lease": original,
                "sync_protocol": 2,
            },
        )

    store = OfflineAccessStore(tmp_path / "private", clock=lambda: NOW)
    with CloudAPIClient(
        ENDPOINT, offline_store=store, transport=httpx.MockTransport(handler)
    ) as client:
        assert client.login(USER["username"], PASSWORD, remember_offline=remember) == USER
        assert client.is_online_authenticated and client.sync_protocol == 2
        assert client.offline_lease == original and client.offline_opt_in is remember
        assert bool(list(store.root.glob("*.offline"))) is remember
        if remember:
            assert store.authenticate(ENDPOINT, USER["username"], PASSWORD)[1] == original
    assert calls == ["/aliyun/v1/auth/login"]


@pytest.mark.parametrize("invalid", ["future", "expired", "overlong"])
def test_real_client_rejects_invalid_lease_without_retaining_session_or_verifier(
    fixed_validation_clock, tmp_path, invalid
):
    issued = NOW + timedelta(seconds=5, microseconds=1)
    original = lease(issued)
    if invalid == "expired":
        original = lease(NOW - timedelta(hours=1), expires_at=NOW.isoformat())
    elif invalid == "overlong":
        original = lease(NOW, expires_at=(NOW + timedelta(hours=12, microseconds=1)).isoformat())

    def handler(request):
        return httpx.Response(
            200,
            json={
                "user": USER,
                "access_token": "synthetic-only-token-12345",
                "offline_lease": original,
            },
        )

    store = OfflineAccessStore(tmp_path / "private", clock=lambda: NOW)
    with CloudAPIClient(
        ENDPOINT, offline_store=store, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(CloudAPIError) as error:
            client.login(USER["username"], PASSWORD, remember_offline=True)
        assert error.value.code == "protocol"
        assert not client.is_authenticated and client.offline_lease is None
        assert not store.root.exists()


def test_future_issuance_never_relaxes_persistent_last_clock_or_revocation(tmp_path):
    clock = [NOW]
    store = OfflineAccessStore(tmp_path, clock=lambda: clock[0])
    store.enroll(ENDPOINT, USER, PASSWORD, lease(NOW + timedelta(seconds=5)))
    clock[0] += timedelta(microseconds=1)
    store.authenticate(ENDPOINT, USER["username"], PASSWORD)
    clock[0] = NOW  # Only one microsecond, still forbidden after persisted progress.
    reopened = OfflineAccessStore(tmp_path, clock=lambda: clock[0])
    with pytest.raises(OfflineAccessError, match="回退"):
        reopened.authenticate(ENDPOINT, USER["username"], PASSWORD)
    clock[0] = NOW + timedelta(seconds=1)
    reopened.revoke(ENDPOINT, USER["username"])
    with pytest.raises(OfflineAccessError):
        reopened.authenticate(ENDPOINT, USER["username"], PASSWORD)


@pytest.mark.parametrize("patch", [{"actor_id": 2}, {"role_code": "advisor"}])
def test_future_issuance_does_not_relax_account_and_role_enrollment_gates(tmp_path, patch):
    store = OfflineAccessStore(tmp_path / "private", clock=lambda: NOW)
    with pytest.raises(OfflineAccessError):
        store.enroll(ENDPOINT, USER, PASSWORD, lease(NOW + timedelta(seconds=5), **patch))
    assert not store.root.exists()
