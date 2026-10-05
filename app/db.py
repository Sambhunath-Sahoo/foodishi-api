import os
import ssl
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# TLS is now AUTHENTICATED, not just encrypted. asyncpg negotiates TLS on its
# own, but with no CA configured it accepts any certificate — so anyone on a hop
# between here and Supabase could present their own, complete the Postgres
# handshake, and read the username and password straight out of DATABASE_URL.
# That is not a theoretical loss: this service connects as the table owner, and
# owners bypass RLS, so the database password is the whole schema.
#
# The CA is Supabase's public root, committed alongside this file (the README
# says it is safe to commit, and it must be present for a deploy to verify
# anything). VERIFY_X509_STRICT is relaxed because the 2021 root does not satisfy
# every RFC 5280 requirement Python 3.13 enforces by default; hostname checking
# and chain verification both stay on, which is what actually stops a MITM.
#
# Missing CA is a hard failure rather than a silent downgrade to unverified TLS:
# a deployment that cannot verify its database should not start pretending it can.
CA_CERT = ROOT / "supabase-root-2021-ca.crt"
if not CA_CERT.exists():
    raise RuntimeError(
        f"{CA_CERT.name} is missing. It is Supabase's public root CA and is "
        "required to verify the database TLS connection. Restore it from the "
        "repository or the Supabase dashboard."
    )
_ssl_context = ssl.create_default_context(cafile=str(CA_CERT))
_ssl_context.check_hostname = True
_ssl_context.verify_mode = ssl.CERT_REQUIRED
_ssl_context.verify_flags &= ~ssl.VERIFY_X509_STRICT

# pool_pre_ping is deliberately OFF. A round trip to this database costs
# ~180ms, and SQLAlchemy's asyncpg ping opens and rolls back a transaction —
# two round trips on every request. Measured: 1361ms with it, 574ms without.
# pool_recycle retires connections before they can go stale instead, which
# costs nothing per request.
#
# The pool is sized for the operator dashboard, which fires ~12 requests at
# once. A NEW connection costs far more than a query, so the pool keeps them.
# Measured 2026-10-05 from Bengaluru: on the old Sydney project a connect took
# 4-7s (~450ms per round trip), and with SQLAlchemy's defaults (5 pooled, 10
# overflow closed on return, recycled every 300s) a 12-request burst took
# 25-60s with asyncpg connect timeouts. The project is now in Mumbai
# (ap-south-1): ~0.6s to connect, ~60ms per round trip, the same burst 1-3s.
# max_connections is 60 and Supabase's own services hold ~17; 15 leaves room.
POOL_SIZE = 10
POOL_MAX_OVERFLOW = 5
POOL_RECYCLE_SECONDS = 1800  # idle_session_timeout is 0: Postgres never drops idle ones

engine = create_async_engine(
    os.environ["DATABASE_URL"],
    connect_args={"ssl": _ssl_context},
    pool_pre_ping=False,
    pool_size=POOL_SIZE,
    max_overflow=POOL_MAX_OVERFLOW,
    pool_recycle=POOL_RECYCLE_SECONDS,
)
Session = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()


async def get_session():
    async with Session.begin() as session:  # commits on success, rolls back on error
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
