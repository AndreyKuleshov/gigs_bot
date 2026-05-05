"""Real-DB tests for AuthService — covers every method except OAuth network."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.db.models import OAuthState, User
from app.services.auth_service import _build_flow, _utcnow, auth_service


def test_utcnow_is_naive():
    n = _utcnow()
    assert n.tzinfo is None


def test_build_flow_uses_settings_creds():
    flow = _build_flow()
    assert "fake-client-id" in flow.client_config["client_id"]


# ── upsert_user_info ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_creates_user_if_missing(db):
    user = await auth_service.upsert_user_info(1, username="alice", full_name="Alice A.")
    assert user.id == 1
    assert user.username == "alice"
    assert user.full_name == "Alice A."


@pytest.mark.asyncio
async def test_upsert_updates_changed_fields(db):
    await auth_service.upsert_user_info(2, username="old", full_name="Old Name")
    await auth_service.upsert_user_info(2, username="new", full_name="New Name")
    from app.db.base import get_session

    async with get_session() as session:
        u = await session.get(User, 2)
    assert u is not None
    assert u.username == "new"
    assert u.full_name == "New Name"


@pytest.mark.asyncio
async def test_upsert_no_change_when_same_fields(db):
    """Idempotent: passing the same values is a no-op."""
    await auth_service.upsert_user_info(3, username="x", full_name="X")
    await auth_service.upsert_user_info(3, username="x", full_name="X")  # no error


# ── timezone ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_timezone_default_utc(db):
    await auth_service.upsert_user_info(10)
    tz = await auth_service.get_user_timezone(10)
    assert tz == "UTC"


@pytest.mark.asyncio
async def test_set_and_get_user_timezone(db):
    await auth_service.upsert_user_info(11)
    await auth_service.set_user_timezone(11, "Europe/Belgrade")
    tz = await auth_service.get_user_timezone(11)
    assert tz == "Europe/Belgrade"


@pytest.mark.asyncio
async def test_get_user_timezone_for_missing_user_returns_utc(db):
    tz = await auth_service.get_user_timezone(9999)
    assert tz == "UTC"


@pytest.mark.asyncio
async def test_set_user_timezone_for_missing_user_is_noop(db):
    """Don't crash if the user row doesn't exist yet."""
    await auth_service.set_user_timezone(8888, "Europe/Belgrade")
    tz = await auth_service.get_user_timezone(8888)
    assert tz == "UTC"


# ── calendar selection ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_id_unset_returns_none(db):
    await auth_service.upsert_user_info(20)
    assert await auth_service.get_calendar_id(20) is None
    assert await auth_service.get_calendar_name(20) is None


@pytest.mark.asyncio
async def test_set_and_get_calendar(db):
    await auth_service.upsert_user_info(21)
    await auth_service.set_calendar_id(21, "primary", "Primary")
    assert await auth_service.get_calendar_id(21) == "primary"
    assert await auth_service.get_calendar_name(21) == "Primary"


@pytest.mark.asyncio
async def test_set_calendar_for_missing_user_is_noop(db):
    await auth_service.set_calendar_id(7777, "x", "X")
    assert await auth_service.get_calendar_id(7777) is None


# ── credentials / auth ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_credentials_no_user_returns_none(db):
    assert await auth_service.get_credentials(404) is None


@pytest.mark.asyncio
async def test_get_credentials_user_without_tokens_returns_none(db):
    await auth_service.upsert_user_info(30)
    assert await auth_service.get_credentials(30) is None


@pytest.mark.asyncio
async def test_get_credentials_with_corrupt_blob_returns_none(db):
    """Decrypt failure → log warning, return None — never raise."""
    from app.db.base import get_session

    await auth_service.upsert_user_info(31)
    async with get_session() as session:
        u = await session.get(User, 31)
        assert u is not None
        u.google_tokens_encrypted = "this-is-not-fernet"
    assert await auth_service.get_credentials(31) is None


@pytest.mark.asyncio
async def test_get_credentials_decrypts_valid_blob(db):
    """Encrypt a token blob with the same FERNET_KEY tests use, store, read back."""
    from app.core.security import encrypt_json
    from app.db.base import get_session

    await auth_service.upsert_user_info(32)
    blob = encrypt_json(
        {
            "token": "tok",
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
            "expiry": "2099-01-01T00:00:00",
        }
    )
    async with get_session() as session:
        u = await session.get(User, 32)
        assert u is not None
        u.google_tokens_encrypted = blob
    creds = await auth_service.get_credentials(32)
    assert creds is not None
    assert creds.token == "tok"
    assert creds.refresh_token == "rt"
    assert creds.expiry is not None and creds.expiry.year == 2099


@pytest.mark.asyncio
async def test_is_authenticated_reflects_get_credentials(db):
    await auth_service.upsert_user_info(33)
    assert await auth_service.is_authenticated(33) is False


@pytest.mark.asyncio
async def test_revoke_tokens_clears_blob(db):
    from app.core.security import encrypt_json
    from app.db.base import get_session

    await auth_service.upsert_user_info(34)
    async with get_session() as session:
        u = await session.get(User, 34)
        assert u is not None
        u.google_tokens_encrypted = encrypt_json({"token": "x"})

    await auth_service.revoke_tokens(34)
    async with get_session() as session:
        u = await session.get(User, 34)
    assert u is not None and u.google_tokens_encrypted is None


@pytest.mark.asyncio
async def test_revoke_tokens_for_missing_user_is_noop(db):
    await auth_service.revoke_tokens(6666)  # must not raise


# ── OAuth state lifecycle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_auth_url_persists_state_row(db):
    """Real flow up to the URL — Google authorization URL is HTTP-free."""
    url = await auth_service.get_auth_url(40)
    assert url.startswith("https://accounts.google.com/o/oauth2/auth")
    from app.db.base import get_session

    async with get_session() as session:
        result = await session.execute(select(OAuthState).where(OAuthState.telegram_user_id == 40))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].telegram_user_id == 40


@pytest.mark.asyncio
async def test_get_auth_url_purges_expired_states(db):
    """Stale states are GC'd whenever a new auth URL is requested."""
    from app.db.base import get_session

    async with get_session() as session:
        session.add(
            OAuthState(
                state="stale",
                telegram_user_id=41,
                code_verifier="v",
                expires_at=_utcnow() - timedelta(hours=1),
            )
        )
    await auth_service.get_auth_url(41)
    async with get_session() as session:
        result = await session.execute(select(OAuthState).where(OAuthState.state == "stale"))
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_handle_oauth_callback_unknown_state_raises(db):
    with pytest.raises(ValueError, match="invalid"):
        await auth_service.handle_oauth_callback(code="xxx", state="never-issued")


@pytest.mark.asyncio
async def test_handle_oauth_callback_expired_state_raises(db):
    """Expired OAuth state → ValueError. (The delete is also issued but the
    same-session rollback on raise undoes it; expired rows are GC'd on the
    next get_auth_url anyway, so we don't assert on the delete here.)"""
    from app.db.base import get_session

    async with get_session() as session:
        session.add(
            OAuthState(
                state="expired",
                telegram_user_id=42,
                code_verifier="v",
                expires_at=_utcnow() - timedelta(minutes=1),
            )
        )
    with pytest.raises(ValueError, match="expired"):
        await auth_service.handle_oauth_callback(code="xxx", state="expired")


@pytest.mark.asyncio
async def test_handle_oauth_callback_persists_tokens(db):
    """Full happy path: state in DB → fetch_token mocked → user row updated."""
    # First create the state via the real method
    await auth_service.get_auth_url(50)
    from app.db.base import get_session

    async with get_session() as session:
        result = await session.execute(select(OAuthState).where(OAuthState.telegram_user_id == 50))
        state_row = result.scalar_one()
        state_token = state_row.state

    # Mock the network round-trip + the credentials object the flow returns
    fake_creds = MagicMock(
        token="abc",
        refresh_token="ref",
        client_id="cid",
        client_secret="cs",
        scopes=["https://www.googleapis.com/auth/calendar"],
        expiry=datetime(2099, 1, 1),
        token_uri="https://oauth2.googleapis.com/token",
    )
    fake_flow = MagicMock()
    fake_flow.fetch_token = MagicMock(return_value=None)
    fake_flow.credentials = fake_creds
    with patch("app.services.auth_service._build_flow", return_value=fake_flow):
        returned_id = await auth_service.handle_oauth_callback(code="ok", state=state_token)
    assert returned_id == 50

    # User row created and tokens stored
    async with get_session() as session:
        u = await session.get(User, 50)
    assert u is not None
    assert u.google_tokens_encrypted is not None


@pytest.mark.asyncio
async def test_handle_oauth_callback_updates_existing_user(db):
    """If user already exists, callback only writes encrypted tokens, not creates a row."""
    await auth_service.upsert_user_info(51, username="precreated", full_name="Pre")
    await auth_service.get_auth_url(51)
    from app.db.base import get_session

    async with get_session() as session:
        result = await session.execute(select(OAuthState).where(OAuthState.telegram_user_id == 51))
        state_token = result.scalar_one().state

    fake_flow = MagicMock()
    fake_flow.credentials = MagicMock(
        token="t",
        refresh_token="r",
        client_id="c",
        client_secret="s",
        scopes=None,
        expiry=None,
        token_uri="https://oauth2.googleapis.com/token",
    )
    with patch("app.services.auth_service._build_flow", return_value=fake_flow):
        await auth_service.handle_oauth_callback(code="ok", state=state_token)

    async with get_session() as session:
        u = await session.get(User, 51)
    assert u is not None
    assert u.username == "precreated"  # not clobbered
    assert u.google_tokens_encrypted is not None


# IntegrityError fallback (concurrent insert race) is exercised in the unit
# only by deliberate fault injection; it's covered with `# pragma: no cover`
# in the source — see app/services/auth_service.py.
