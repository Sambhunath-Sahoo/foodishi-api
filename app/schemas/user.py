from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class UserCreate(BaseModel):
    # extra="forbid" rejects unknown fields, and stops clients setting
    # server-owned columns like id, is_active or created_at.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=120)
    email: EmailStr = Field(max_length=200)
    phone: str = Field(min_length=7, max_length=20)
    city: str = Field(min_length=2, max_length=60)


class UserUpdate(BaseModel):
    # Every field optional, so a caller sends only what changes. is_active and
    # created_at stay absent for the same reason they are absent from
    # UserCreate: they are the server's to set, not the client's.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: EmailStr | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, min_length=7, max_length=20)
    city: str | None = Field(default=None, min_length=2, max_length=60)

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        # An empty body would otherwise reach the database as a no-op UPDATE
        # and report success without changing anything.
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        return self

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        # None here means "field omitted", never "set this column to null" —
        # every one of these columns is NOT NULL. Without this check an explicit
        # null passes validation and fails in the database instead.
        nulls = sorted(f for f in self.model_fields_set if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self


class UserRead(BaseModel):
    # An allowlist for output: columns added later stay private by default.
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    phone: str
    city: str
    is_active: bool
    avatar_url: str | None = None
    created_at: datetime
