"""Verification of Supabase Auth access tokens.

This module answers exactly one question: was this JWT really issued by our
Supabase project, and is it still valid? It does no database work and knows
nothing about public.users — app/dependencies/identity.py turns claims into a
profile. Keeping the split means the crypto can be tested without a database.

Read app/dependencies/identity.py's docstring before deploying: the AUTH_ENABLED
escape hatch can bypass everything in this file.
"""

import logging
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from starlette.concurrency import run_in_threadpool

from app.config import require_env

logger = logging.getLogger(__name__)

# Supabase signs project JWTs with ES256 (current, asymmetric keys) or RS256.
# An explicit list is what stops the "alg": "none" and HS256-with-the-public-key
# confusion attacks — never pass the algorithm from the token's own header.
ALGORITHMS = ["ES256", "RS256"]

# Every Supabase user token carries aud "authenticated". Anon/service keys do
# not, so checking it keeps a leaked publishable key from being replayed here.
AUDIENCE = "authenticated"

# Claims we refuse to run without. Absent any of these the token is malformed
# for our purposes even if the signature checks out.
REQUIRED_CLAIMS = ["exp", "iat", "sub", "aud", "iss"]

# Small clock skew tolerance between Supabase's signer and this host.
LEEWAY_SECONDS = 10

# What the caller is told, always, whatever actually went wrong. A client that
# can tell "expired" from "wrong signature" from "wrong issuer" learns nothing
# it can act on and gains an oracle for probing tokens. The real reason goes to
# the log, where an operator can see it.
GENERIC_FAILURE = "Invalid or expired token"


class TokenError(Exception):
    """Token failed verification.

    The message is written for the server log, never for an HTTP response body.
    """


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    """The shared JWKS client.

    cache_keys=True is the reason this is a singleton: Supabase rotates signing
    keys, and a cached client picks the new key up on the first token that
    references an unknown kid. A fresh client per request would refetch the key
    set on every call instead, and a client built at import time would make the
    whole app fail to start when SUPABASE_JWKS_URL is unset.
    """
    return PyJWKClient(require_env("SUPABASE_JWKS_URL"), cache_keys=True)


def _issuer() -> str:
    # Supabase mints tokens with iss "<project-url>/auth/v1". rstrip guards the
    # common .env slip of a trailing slash on SUPABASE_URL, which would produce
    # a double slash and reject every real token.
    return f"{require_env('SUPABASE_URL').rstrip('/')}/auth/v1"


async def verify_token(token: str) -> dict[str, Any]:
    """Verify a Supabase access token and return its claims.

    Raises TokenError on any failure, with a log-only reason.

    Both steps are synchronous and blocking: get_signing_key_from_jwt performs
    an HTTPS fetch when the kid is not cached, and jwt.decode is CPU-bound
    signature verification. Run on the event loop they would stall every other
    request on this worker, so both go through the threadpool.
    """
    if not token or not token.strip():
        raise TokenError("empty bearer token")

    try:
        signing_key = await run_in_threadpool(
            _jwks_client().get_signing_key_from_jwt, token
        )
        claims = await run_in_threadpool(
            jwt.decode,
            token,
            signing_key.key,
            algorithms=ALGORITHMS,
            audience=AUDIENCE,
            issuer=_issuer(),
            leeway=LEEWAY_SECONDS,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        # Covers both families: PyJWKClientError (key set unreachable, unknown
        # kid) and InvalidTokenError (expired, bad signature, wrong audience).
        raise TokenError(f"{type(exc).__name__}: {exc}") from exc
    except RuntimeError as exc:
        # require_env: auth is switched on but SUPABASE_URL / SUPABASE_JWKS_URL
        # are missing. A misconfigured server, not a bad token — but the caller
        # still gets the generic message, and this is what tells us apart.
        logger.error("Auth is enabled but not configured: %s", exc)
        raise TokenError(f"configuration: {exc}") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise TokenError("token has no usable sub claim")
    return claims
