from dataclasses import dataclass
from typing import Annotated, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    """Bounded paging for every list endpoint.

    le=MAX_LIMIT is the point: an unbounded list endpoint is the easiest way to
    take a database down, and FastAPI rejects the oversized request before it
    ever reaches a query.
    """
    return PageParams(limit=limit, offset=offset)


PageDep = Annotated[PageParams, Depends(page_params)]

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


async def paginate(
    session: AsyncSession, statement: Select, params: PageParams
) -> tuple[list, int]:
    """Run a query windowed, plus a COUNT over the same filters.

    The count reuses the statement's own WHERE clause via a subquery, so a
    filter can never be applied to the page but forgotten in the total.
    """
    total = await session.scalar(
        select(func.count()).select_from(statement.subquery())
    )
    rows = await session.execute(statement.limit(params.limit).offset(params.offset))
    return list(rows.scalars().all()), int(total or 0)
