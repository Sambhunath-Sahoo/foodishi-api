"""Supabase auth accounts for seeded staff, so a seeded login can actually sign in.

Extracted from app/seed/platform.py rather than copied into the new restaurant
staff phase. Both need the same three things — find an account, create one, link
the id onto public.users.auth_user_id — and this codebase already carries four
byte-identical copies of a LIKE escaper in its routers, which is the pattern this
avoids repeating.

WHAT THIS TOUCHES OUTSIDE POSTGRES. Every other seed phase writes rows and
nothing else. This one calls Supabase's admin API and creates real auth accounts,
which is the only reason a seeded operator or manager can reach a console at all:
`users.auth_user_id` is the join between an identity and a profile, and nothing
else in the repository populates it.

It fails SOFT, deliberately. Supabase unreachable, or the keys unset, means the
profiles are still written and simply cannot be signed into yet — which is a
recoverable state a later run repairs, where refusing to seed at all would leave
a half-built database.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

#: DEVELOPMENT ONLY, and shared by every seeded staff login.
#:
#: Fine here for the reason it has always been fine: these accounts exist on a
#: laptop's database against a development Supabase project. A real deployment
#: provisions people through Supabase and never reads this constant.
#:
#: If this value changes, every already-created auth account keeps its OLD
#: password — Supabase stores the hash, not this string. Re-running the seeder
#: does not reset it, because the create call is skipped for an account that
#: already exists. Change it in the Supabase dashboard too, or delete the
#: accounts first.
DEV_PASSWORD = "foodishidev2026"

#: The domain every seeded staff address sits on.
#:
#: NEVER example.com: the seeded customers occupy that, and a collision there
#: would hand a restaurant or platform grant to a customer.
STAFF_DOMAIN = "foodishi.internal"


class AdminApi:
    """The slice of Supabase's admin API the seeder needs."""

    def __init__(self, base_url: str, secret_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}

    async def find(self, client: httpx.AsyncClient, email: str) -> str | None:
        # The admin list endpoint has no exact-match filter worth trusting, so
        # this pages and compares. A handful of staff against a few hundred
        # accounts makes that cheaper than being clever.
        response = await client.get(
            f"{self._base}/auth/v1/admin/users",
            headers=self._headers,
            params={"page": 1, "per_page": 200},
        )
        response.raise_for_status()
        for account in response.json().get("users", []):
            if (account.get("email") or "").lower() == email.lower():
                return account["id"]
        return None

    async def create(self, client: httpx.AsyncClient, email: str) -> str:
        response = await client.post(
            f"{self._base}/auth/v1/admin/users",
            headers=self._headers,
            # email_confirm skips the verification mail: there is no inbox behind
            # the staff domain, and an unconfirmed account cannot sign in.
            json={"email": email, "password": DEV_PASSWORD, "email_confirm": True},
        )
        if response.status_code == 422:
            # Already registered. Fall back to the lookup rather than failing —
            # the seeder has to survive a second run.
            existing = await self.find(client, email)
            if existing is not None:
                return existing
        response.raise_for_status()
        return response.json()["id"]


    async def rename(
        self, client: httpx.AsyncClient, auth_user_id: str, new_email: str
    ) -> None:
        """Change an existing account's address, keeping the account itself.

        The auth id is unchanged, which is the whole point: `users.auth_user_id`
        keeps pointing at the same account, so the linkage survives a rebrand of
        the address. email_confirm re-confirms the new address, because an
        unconfirmed one cannot sign in and there is no inbox behind the staff
        domain to click a link in.
        """
        response = await client.put(
            f"{self._base}/auth/v1/admin/users/{auth_user_id}",
            headers=self._headers,
            json={"email": new_email, "email_confirm": True},
        )
        response.raise_for_status()


    async def set_password(
        self, client: httpx.AsyncClient, auth_user_id: str, password: str
    ) -> None:
        """Force an existing account's password to `password`.

        Needed because renaming an address does NOT change the credential behind
        it: Supabase stores a hash, and the create call that would have used the
        new DEV_PASSWORD is skipped for an account that already exists. So after
        the rebrand the seeded operator still answered to `tadkadev2026` while
        every comment in this repository said `foodishidev2026` -- a documented
        credential that does not work is worse than an undocumented one, because
        somebody trusts it and then doubts the account instead of the docs.

        Development only, and only ever called with DEV_PASSWORD.
        """
        response = await client.put(
            f"{self._base}/auth/v1/admin/users/{auth_user_id}",
            headers=self._headers,
            json={"password": password},
        )
        response.raise_for_status()


def admin_api() -> AdminApi | None:
    """The admin client, or None when the environment cannot support one."""
    base_url = os.environ.get("SUPABASE_URL", "").strip()
    secret_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not base_url or not secret_key:
        return None
    return AdminApi(base_url, secret_key)


async def resolve_auth_ids(emails: tuple[str, ...]) -> dict[str, str]:
    """Supabase auth ids for each address, creating the account if absent.

    Returns an EMPTY mapping rather than raising when Supabase is unreachable or
    unconfigured — see the module docstring. The caller is expected to say so in
    its notes, so a seeded login that cannot sign in is visible rather than
    discovered later at a sign-in screen.

    One HTTP client for the whole batch: a client per address would open a fresh
    TLS connection each time for no benefit.
    """
    if not emails:
        return {}

    api = admin_api()
    if api is None:
        logger.warning(
            "SUPABASE_URL/SUPABASE_SECRET_KEY not set — seeding staff without "
            "auth accounts. The profiles will exist but cannot be signed into."
        )
        return {}

    resolved: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for email in emails:
                resolved[email] = await api.find(client, email) or await api.create(
                    client, email
                )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning(
            "Supabase admin API failed (%s) — seeding the rest without the "
            "accounts that are missing. Rows already linked are unaffected.",
            exc,
        )
    return resolved
