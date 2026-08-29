import enum

from sqlalchemy import Enum


def pg_enum(python_enum: type[enum.Enum], name: str) -> Enum:
    """A native Postgres enum storing member *values*, not member names.

    SQLAlchemy defaults to persisting `OUT_FOR_DELIVERY`; values_callable makes
    it persist `out_for_delivery`, which is what the API and the seed data use.
    """
    return Enum(
        python_enum,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )
