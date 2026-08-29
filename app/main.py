import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import Base, engine
from app.models import registry  # noqa: F401 - registers every model on Base.metadata
from app.routers import (
    addresses,
    admin_finance,
    admin_operations,
    admin_platform,
    admin_reports,
    catalog,
    catalog_admin,
    coupons,
    delivery,
    finance,
    health,
    images,
    me,
    metrics,
    modifiers,
    orders,
    payments,
    refunds,
    reports,
    reviews,
    staff,
    users,
)

logger = logging.getLogger(__name__)

#: Whether boot-time schema preparation succeeded. Read by GET /health, which
#: otherwise cannot tell the difference between "database reachable" and
#: "database reachable AND the schema this build expects is actually there".
SCHEMA_STATE: dict[str, object] = {"prepared": False, "error": None}


# Every table in this schema is expected to have row-level security ENABLED and
# NO policies. That combination is deny-all for the anon and authenticated roles
# PostgREST uses, while this service is unaffected because it connects as the
# table owner and owners bypass RLS unless it is FORCED.
#
# It has to be asserted on boot because create_all does not do it. A table added
# to a model ships with RLS off, and Supabase exposes every public table over
# PostgREST using the publishable key — which is public by design and sits in
# every frontend bundle. That is not theoretical: platform_staff, the table that
# decides who is a Foodishi admin, was created this way and was readable AND
# INSERTABLE with the publishable key until this ran. Anyone could have granted
# themselves platform admin with one request.
#
# Adding a policy is a deliberate act; leaving RLS off must not be an accident.
ENABLE_RLS = """
    select tablename from pg_tables
    where schemaname = 'public' and not rowsecurity
"""


# create_all creates missing TABLES. It does not touch a table that already
# exists, so a column added to an existing model never reaches the database and
# every query naming it fails at runtime with "column does not exist".
#
# These are the additive columns that have shipped since. ADD COLUMN IF NOT
# EXISTS is idempotent and, for a nullable column or one with a server default,
# non-blocking on Postgres — so this is safe to run on every boot. It is still a
# stand-in for migrations: anything that DROPS or RETYPES a column does not
# belong here and needs a real migration tool.
#
# Never write a destructive statement into this list. The seed cannot rebuild
# the hand-made restaurant_staff rows or the Supabase auth linkage on
# users.auth_user_id, so losing either is unrecoverable.
ADDITIVE_COLUMNS = (
    # The platform's cut, per restaurant. Defaulted so existing rows are valid.
    'alter table public.restaurants add column if not exists '
    'commission_percent numeric(5, 2) not null default 18.00',
    # "Leave it at the gate" — previously kept in the customer's browser, so the
    # kitchen never saw it.
    "alter table public.orders add column if not exists delivery_note text",
    # Per-person grants on top of a role.
    "alter table public.restaurant_staff add column if not exists "
    "permissions varchar(40)[] not null default '{}'::varchar[]",
    "alter table public.restaurant_staff add column if not exists "
    "access_reset_at timestamptz",
    # The three below were applied to the working database out of band and were
    # in NO reproducible record: not in create_all (which never touches a table
    # that already exists) and not in this list. Verified present in the live
    # schema, so these are no-ops there — they are here so the NEXT environment
    # gets them. Without them a fresh deployment against an existing database
    # fails on every query naming the column, because SQLAlchemy puts every
    # mapped column in every SELECT: orders.idempotency_key takes down the whole
    # order surface, and users.avatar_url takes down every authenticated request.
    "alter table public.orders add column if not exists "
    "idempotency_key varchar(80)",
    # unique=True on the model is a table-level constraint, which ADD COLUMN does
    # not carry. This index is what actually enforces "a double-tap must not buy
    # dinner twice".
    "create unique index if not exists uq_orders_idempotency_key "
    "on public.orders (idempotency_key)",
    "alter table public.users add column if not exists avatar_url text",
    # At most one live payment per order. See models/payment.py for why partial.
    "create unique index if not exists uq_payments_one_live_per_order "
    "on public.payments (order_id) where status in "
    "('authorized', 'captured', 'partially_refunded')",
)


# ALTER TYPE cannot live in ADDITIVE_COLUMNS: a value added to an enum is not
# usable by later statements in the SAME transaction, and everything above runs
# inside one engine.begin(). So these get a transaction each.
#
# StaffRole collapsed `owner` and `manager` into `admin`, and dropping an enum
# value is not something Postgres offers — so the live type carries all four and
# the Python enum carries two. Verified: the live type already has `admin` and no
# restaurant_staff row still holds a legacy value. Recorded here so a fresh
# database is not left with a type the code cannot write.
ADDITIVE_ENUM_VALUES = (
    "alter type staff_role add value if not exists 'admin'",
)


async def _apply_additive_columns(connection) -> None:
    for statement in ADDITIVE_COLUMNS:
        await connection.execute(text(statement))


#: One key, hashed once, so every schema-preparation transaction contends on the
#: same lock rather than only the first one.
SCHEMA_LOCK = "select pg_advisory_xact_lock(hashtext('foodishi_schema_prep'))"

#: ADD COLUMN and ALTER TABLE take ACCESS EXCLUSIVE, and while one waits every
#: query on that table queues behind it. Bounded so a boot during service fails
#: fast and visibly instead of stalling the API.
LOCK_TIMEOUT = "set local lock_timeout = '5s'"


@asynccontextmanager
async def _schema_step():
    """One transaction, holding the schema lock, with a bounded lock wait.

    pg_advisory_xact_lock is released at COMMIT, so taking it once in the first
    transaction serialised only that transaction -- the CREATE TYPE and
    CREATE UNIQUE INDEX races it was added for were still open across workers,
    and now cost a permanently-503 worker rather than a swallowed warning,
    because a failure here sets SCHEMA_STATE["prepared"] = False. Every step
    takes it.
    """
    async with engine.begin() as connection:
        await connection.execute(text(SCHEMA_LOCK))
        await connection.execute(text(LOCK_TIMEOUT))
        yield connection


async def _apply_additive_enum_values() -> None:
    for statement in ADDITIVE_ENUM_VALUES:
        # A transaction each: a value added to an enum is not usable by a later
        # statement in the same one.
        async with _schema_step() as connection:
            await connection.execute(text(statement))


async def _assert_row_level_security(connection) -> None:
    unprotected = (await connection.execute(text(ENABLE_RLS))).scalars().all()
    for table in unprotected:
        # The comment here used to claim quote_ident was being used; it was not,
        # and no quote_ident call existed. The identifier is trusted because it
        # comes from pg_tables and never from a request, so this is not
        # exploitable -- but a comment asserting a safety measure that is absent
        # is how the next reader stops checking. Asserted rather than described:
        # an identifier that is not a plain one is skipped and logged, so a
        # future change that widens the source of `table` fails loudly.
        if not table.isidentifier():
            logger.error(
                "Skipping RLS on %r: not a plain identifier. Enable it by hand.",
                table,
            )
            continue
        await connection.execute(
            text(f'alter table public."{table}" enable row level security')
        )
    if unprotected:
        logger.warning(
            "Enabled row-level security on %d table(s) that had none: %s. "
            "A table reaches this state by being created through create_all; "
            "it was open to anyone holding the publishable key until now.",
            len(unprotected),
            ", ".join(unprotected),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Stand-in for migrations while the schema is one table. Startup carries on
    # when the database is unreachable so the process stays inspectable, and
    # GET /health reports the failure rather than letting it pass unnoticed.
    #
    # ONE TRANSACTION PER STEP, deliberately. All three used to share an
    # engine.begin(), so a failure in the LAST step rolled back the first two --
    # a single table in `public` this role does not own made the RLS sweep raise
    # `must be owner of table`, which silently reverted create_all and every
    # additive column. Separate transactions mean a late failure cannot undo
    # earlier work.
    #
    # EVERY step holds the advisory lock, not just the first -- see _schema_step.
    # With `--workers 4`, four processes ran create_all and CREATE TYPE
    # concurrently, and `checkfirst` is a read followed by a write with no lock,
    # so the losers raised a duplicate-key error on pg_type that the bare
    # `except` swallowed.
    try:
        async with _schema_step() as connection:
            await connection.run_sync(Base.metadata.create_all)

        await _apply_additive_enum_values()

        async with _schema_step() as connection:
            await _apply_additive_columns(connection)

        # Last, so a table created a moment ago is protected before anything can
        # read it -- and in its own transaction so an un-owned table cannot
        # revert the columns above.
        async with _schema_step() as connection:
            await _assert_row_level_security(connection)

        SCHEMA_STATE["prepared"] = True
        SCHEMA_STATE["error"] = None
    except Exception as exc:
        # Recorded, not just logged. /health used to answer `ok` for every
        # failure mode except an unreachable database, so a lock timeout or an
        # un-owned table left the pods passing their readiness probe while every
        # query naming a new column returned 500. The comment promised /health
        # would report it; now it can.
        SCHEMA_STATE["prepared"] = False
        SCHEMA_STATE["error"] = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "Could not prepare schema — API will start, /health will report 503"
        )
    yield
    await engine.dispose()


app = FastAPI(title="Foodishi AI API", lifespan=lifespan)


#: Exception types anywhere in a DBAPIError's cause chain that mean "the CALLER
#: sent a value this column cannot hold", as opposed to "the database is having a
#: problem". Matched by dotted name so this module does not import asyncpg.
#:
#: Registering the handler below on `sqlalchemy.exc.DataError` did NOT work, and
#: the reason is worth writing down: the asyncpg dialect wraps every driver error
#: in its own `AsyncAdapt_asyncpg_dbapi.Error`, which SQLAlchemy classifies as a
#: generic `DBAPIError` — never as the more specific `DataError`. So the handler
#: was registered for a class that is never raised, and
#: `GET /menu-items/1099511627776` kept returning 500. The app built and imported
#: perfectly the whole time; only running it showed this.
DATA_ERROR_TYPES = frozenset(
    {
        "asyncpg.exceptions.DataError",
        "asyncpg.exceptions.NumericValueOutOfRangeError",
        "sqlalchemy.exc.DataError",
        "builtins.OverflowError",
    }
)


def _is_caller_data_error(exc: BaseException) -> bool:
    """Walk the cause chain for a "this value is not storable" error."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        kind = type(current)
        if f"{kind.__module__}.{kind.__qualname__}" in DATA_ERROR_TYPES:
            return True
        current = current.__cause__ or current.__context__
    return False


# An integer outside PostgreSQL's `integer` range is not a missing row, it is a
# value the column cannot hold: asyncpg raises on the bind, which without this
# handler is a bare 500. app/core/ids.py bounds every id field a request body
# declares and app/dependencies/scope.py's path_int bounds path parameters, but
# neither can cover a field added later, and GET /menu-items/{huge} reaches
# session.get with no dependency in between.
#
# This is the backstop, not the fix: a bounded field gives the caller a message
# naming what was wrong, where this can only say that something was out of range.
#
# Registered on DBAPIError because that is what actually arrives, then NARROWED:
# a connection drop or a deadlock is also a DBAPIError and is emphatically not
# the caller's fault, so anything that is not a data error is re-raised and gets
# the 500 it deserves.
@app.exception_handler(DBAPIError)
async def handle_dbapi_error(request: Request, exc: DBAPIError) -> JSONResponse:
    if not _is_caller_data_error(exc):
        raise exc
    logger.warning(
        "Out-of-range value on %s %s: %s", request.method, request.url.path, exc
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "A value in this request is out of range for the field it "
            "addresses — check that every id is a positive integer below 2147483647."
        },
    )

# The three consoles (operator :3000, partner :3001, customer :3002) are served
# from their own origins by Next, so every call they make is cross-origin. With
# no CORSMiddleware a preflight answers 405 and a plain GET carries no
# Access-Control-Allow-Origin, which makes the API unreachable from a browser
# even though curl is perfectly happy. Origins come from CORS_ORIGINS
# (comma-separated) and fall back to the local dev trio.
#
# allow_headers is named rather than "*" because the dev identity header is a
# custom one: naming it documents the contract and keeps working unchanged if
# credentialed requests are ever switched on, where "*" is not honoured.
DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000,http://localhost:3001,http://localhost:3002,"
    "http://127.0.0.1:3000,http://127.0.0.1:3001,http://127.0.0.1:3002"
)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "Idempotency-Key",
        "X-Dev-User-Id",
    ],
    max_age=600,
)

# Grouped by audience rather than alphabetically, because that is the question
# somebody reading this list actually has: which console does this serve.
#
# Route ORDER matters in one place. FastAPI matches in declaration order, so a
# literal path must be declared before a parameterised one that would also
# match it — `/restaurants/{restaurant_id}/reports` and
# `/restaurants/{restaurant_id}/modifier-groups` are distinct literals under the
# same prefix and do not collide, but anything added later of the shape
# `/restaurants/{id}/{something}` would swallow both. Keep the specific ones here.
for module in (
    # Public and shared
    health, catalog, coupons, images,
    # Customer
    users, addresses, orders, payments, refunds, reviews, me,
    # Restaurant (the partner console)
    catalog_admin, modifiers, staff, reports, finance, delivery,
    # Foodishi operations (the operator console)
    metrics, admin_platform, admin_operations, admin_finance, admin_reports,
):
    app.include_router(module.router)
