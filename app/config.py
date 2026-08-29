"""Environment access for the auth stack.

pydantic-settings was deliberately removed from this project, so this is the
whole configuration layer: named accessors over os.environ. The point of the
accessors rather than bare os.environ["..."] reads is the failure mode — a
missing variable raises with its own name in the message instead of surfacing
as a KeyError from somewhere three frames down.

Importing app.db is what makes os.environ populated: that module calls
load_dotenv() on the repo-root .env at import time.
"""

import os

# Imported for the side effect: app.db's module body calls load_dotenv().
from app.db import ROOT  # noqa: F401

# Accepted spellings for boolean env vars. Anything OUTSIDE both sets is an
# error, not a false: a typo used to read as false, which for AUTH_ENABLED meant
# `AUTH_ENABLED=Y` silently disabled authentication. See env_flag.
TRUTHY = frozenset({"1", "true", "yes", "on"})


def require_env(name: str) -> str:
    """Read a variable that the process cannot function without.

    Called lazily (at first use, not at import) so the app still boots and
    serves /health in an environment where auth is not configured yet.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to .env (see the Supabase dashboard, "
            f"Connect -> Framework)."
        )
    return value


FALSEY = {"0", "false", "no", "off"}


def env_flag(name: str, *, default: bool = False) -> bool:
    """Parse a boolean environment variable, refusing anything ambiguous.

    An unrecognised value used to read as false, which for a security flag is
    the wrong way to be wrong: AUTH_ENABLED=Y, =t, =enabled or =True_ all
    silently disabled authentication. A value that is neither truthy nor falsey
    is now a startup error naming the variable, because the alternative is a
    deployment that looks configured and is not.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUTHY:
        return True
    if value in FALSEY:
        return False
    raise RuntimeError(
        f"{name}={raw!r} is not a recognised boolean. Use one of "
        f"{sorted(TRUTHY)} or {sorted(FALSEY)}."
    )


def is_auth_enabled() -> bool:
    """Whether real Supabase tokens are required.

    Read per request rather than cached at import so a test can flip it with
    monkeypatch.setenv and so a deployment cannot end up running with a value
    that was captured before the environment was fully assembled.

    Defaults to TRUE. It used to default to false, which meant authentication
    was off unless somebody remembered to turn it on — and .env is gitignored,
    so .env is not what a deployment gets. One missing variable re-armed the
    X-Dev-User-Id header into a total bypass: `curl -H 'X-Dev-User-Id: 1' /me`
    returned any customer, and walking the integer reached restaurant admins.
    Failing closed means the escape hatch now costs an explicit
    AUTH_ENABLED=false, which is a thing somebody has to write down.
    """
    return env_flag("AUTH_ENABLED", default=True)
