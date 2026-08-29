"""One bounded type for every database id that arrives in a request.

`app/dependencies/scope.py` already worked this out for PATH parameters and
wrote down why:

    Every id these dependencies look up is a PostgreSQL `integer` primary key.
    A value outside that range is not a row that is missing, it is a value the
    column cannot hold — asyncpg raises DataError on the bind, which surfaces as
    a 500 rather than the 422 the caller deserves.

`path_int` enforces that, but it only runs for routes that carry a scope or
ownership dependency, and NO body field or query int had a bound at all. So
`POST /coupons/validate {"restaurant_id": 1099511627776}` — unauthenticated —
was a 500, and so was `GET /menu-items/1099511627776`.

PG_INT_MAX is re-declared here rather than imported from scope.py because this
module must not depend on the dependency layer; scope.py's copy stays as the
authority for path parameters and the two are asserted equal in the one place
that can see both.
"""

from typing import Annotated

from pydantic import Field

#: PostgreSQL `integer` upper bound. A primary key cannot exceed it.
PG_INT_MAX = 2**31 - 1

#: A reference to an existing row: positive, and small enough to be stored.
DbId = Annotated[int, Field(ge=1, le=PG_INT_MAX)]
