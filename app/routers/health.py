import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db import engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/")
async def root():
    return {"message": "Foodishi AI is running"}


@router.get("/health")
async def health(response: Response):
    """Liveness plus a real database round-trip.

    The process can serve traffic without Postgres, so a reachable API says
    nothing about whether requests will succeed. This actually issues a query
    and returns 503 when it fails, so orchestrators and humans see the outage.
    """
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        # The reason goes to the log, not to the caller. /health has no auth
        # dependency, and an asyncpg connection failure stringifies to include
        # the host, the port and sometimes the user it tried -- i.e. the shape of
        # DATABASE_URL, handed to anyone who asks. app/services/auth.py keeps one
        # GENERIC_FAILURE string for exactly this reason; so does this now.
        logger.exception("Health check could not reach the database")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": "unreachable"}

    # Reachable is not the same as ready. Boot-time schema preparation can fail
    # while the database stays perfectly reachable -- a lock timeout, an un-owned
    # table in the RLS sweep, two workers racing on CREATE TYPE -- and this used
    # to answer "ok" for all of them, so orchestrators passed the readiness probe
    # while every query naming a new column returned 500. The comment above
    # promised /health would report the failure; this is that.
    #
    # Imported here rather than at module scope: app.main imports this router, so
    # a top-level import would be circular.
    from app.main import SCHEMA_STATE

    if not SCHEMA_STATE["prepared"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        # The reason is NOT returned. It was, on the reasoning that a DDL failure
        # is "our own exception class, not a connection string" -- but the block
        # that records it is `except Exception` around engine.begin(), so a
        # boot-time connection or TLS failure is captured verbatim, host, port,
        # username and cert subject included. It would then be served to any
        # anonymous caller the moment the database came back. That is the same
        # leak the branch above was changed to close.
        #
        # SCHEMA_STATE["error"] is logged by the lifespan handler that sets it.
        return {
            "status": "degraded",
            "database": "reachable",
            "schema": "not prepared",
        }

    return {"status": "ok", "database": "reachable", "schema": "prepared"}
