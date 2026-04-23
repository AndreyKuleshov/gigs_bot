"""SQLAlchemy ORM models."""

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class User(Base):
    """One row per Telegram user.  Google tokens are stored encrypted."""

    __tablename__ = "users"

    # Primary key is the Telegram user_id (already globally unique)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Fernet-encrypted JSON blob of Google OAuth2 tokens
    google_tokens_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # IANA timezone, e.g. "Europe/Moscow"
    timezone: Mapped[str] = mapped_column(String(50), default="UTC", nullable=False)

    # Selected Google Calendar ID (empty = not yet selected)
    selected_calendar_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    selected_calendar_name: Mapped[str | None] = mapped_column(
        String(255), default=None, nullable=True
    )

    # Last date (in user's local tz) the daily morning digest was sent.
    # Guards against double-sending from the ~60s scheduler tick.
    last_daily_sent_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<User id={self.id}>"


class OAuthState(Base):
    """Short-lived PKCE OAuth state stored in DB (replaces Redis for stateless deploys)."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(256), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
