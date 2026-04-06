"""SQLAlchemy ORM models."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
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

    # "button" or "text"
    mode: Mapped[str] = mapped_column(String(20), default="button", nullable=False)

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
        return f"<User id={self.id} mode={self.mode}>"
