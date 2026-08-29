from uuid import UUID

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.mixins import TimestampMixin


class User(Base, TimestampMixin):
    # Unrelated to Supabase's built-in auth.users table.
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(20))
    city: Mapped[str] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    avatar_url: Mapped[str | None] = mapped_column(Text)

    # Links this profile to Supabase Auth. Nullable because seeded users and
    # anyone created before auth existed have no identity yet. The profile stays
    # here rather than in auth.users: that table is Supabase-managed, and orders,
    # addresses and redemptions all reference this integer id.
    auth_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), unique=True)
