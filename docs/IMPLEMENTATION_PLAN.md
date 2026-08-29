# Foodishi AI — Phase 1 Implementation Plan

Seed data + 59 REST endpoints, Supabase Auth included. No Redis, no caching,
no AI, no background workers — those are phase 2.

**Counting convention.** "59" is what phase 1 *adds*: 50 domain endpoints, plus
the 9 identity and staff endpoints D2's reversal brought in. The four `/users`
endpoints that predate this plan, the 4 `/admin/metrics` endpoints, `GET /` and
`GET /health` are on top. `app.openapi()` is the arbiter — it reports **69
operations today**.

The reversal was first sized at 6 endpoints; it shipped as 9. `POST
/auth/profile` became `POST /auth/link`, `GET /auth/me` split into `GET /me` and
`GET /me/restaurants`, and `PATCH /me` and `GET /me/orders` were added as the
token-subject twins of the two endpoints that take an identity from the request.
`API_PLAN.md` has the reasoning per path. Every one of the nine is built.

**How to use this:** tick boxes as you go. Every step ends with an
**Acceptance** block — do not move on until those pass. Steps are ordered so
each one is testable before the next begins.

---

## Decisions — settled 2026-08-20

- [x] **D1 — Response shape: bare objects.** Not explicitly answered; going with
      the recommendation, which is also the status quo. `response_model` returns
      the object, meaning comes from the HTTP status, errors keep the existing
      `detail` shape. No work, no retrofit.
- [x] **D2 — Auth: Supabase Auth, in phase 1.** ~~Skipped for phase 1.~~
      **Reversed on 2026-08-20, the same day it was deferred.** See below for
      what changed and what the reversal cost.
- [x] **D3 — Tests: skipped for phase 1.** See mitigation below.

### D2, reversed — the honest version

Deferring auth was recorded on 2026-08-20 as a deliberate choice, with the
consequences written down rather than discovered later. Those consequences
arrived within hours of being written down, so the decision was reinstated the
same day. Both halves are kept here because the reasoning is the useful part.

**Why it was deferred.** Everything ran on one laptop against seed data. Auth
buys nothing when the only caller is you, and it touches every endpoint, so
doing it early looked like paying a large cost for a benefit that starts at
zero.

**Why that did not survive the day.** The deferral was written with three
triggers attached — *the moment any of these is true, this decision is wrong*:

- The API is reachable from anywhere but localhost — every order, address and
  phone number is readable by incrementing an integer, and anyone can cancel
  anyone's food
- You build the **partner** app — restaurant scoping *is* authorization. A UI
  filter is bypassed by editing an id in the URL, so restaurant A would read
  restaurant B's revenue
- You build the **customer** app — "my orders" has no meaning without identity

`MONOREPO_PLAN.md` was written the same day and commits to three apps, `partner`
among them. That fired the second trigger immediately. Restaurant scoping is not
a screen you add later; it is a server-side check on the endpoints that were
about to be written, and retrofitting it across sixty of them is exactly the
expensive path Step 0 exists to avoid.

**What the reversal actually cost.** Less than the deferral implied, because the
groundwork was already in place:

- `auth_user_id uuid UNIQUE REFERENCES auth.users(id) ON DELETE SET NULL` on
  `public.users`, plus the `restaurant_staff` table — one migration, already
  applied. See `DATABASE_DESIGN.md` §5.8
- `app/services/auth.py` — JWT verification against the project JWKS. ES256 and
  RS256 only, so an HS256 alg-confusion forgery signed with a published key is
  rejected. No database access, no framework coupling
- `app/dependencies/identity.py` — `CurrentUser`, `OptionalUser`,
  `CurrentStaff`, `require_staff(restaurant_id?, minimum_role?)`
- `app/config.py` — `require_env`, `env_flag`, `is_auth_enabled`. No
  pydantic-settings; it was removed deliberately and stays removed

**What it cost that was avoidable, and was not avoided.** The endpoints written
before the reversal take `user_id` from the path or body. That was the hedge:
"when auth lands, that becomes read-it-from-the-token instead of a rewrite." The
hedge held for identity but **not** for authorization — `GET /orders?user_id=7`
still answers for user 7 to anyone who asks. Reading the id from the token is a
one-line change; *refusing* the request when the token names someone else is a
per-endpoint decision that has to be made sixty times. Step 15 is that work.

**Escape hatch.** `AUTH_ENABLED=false` in `.env` swaps token verification for an
`X-Dev-User-Id` header and logs `DEV AUTH:` at WARNING on every request. It
fakes identity, never permissions — `require_staff` still consults
`restaurant_staff` in dev mode, so the header picks a `users.id` and that user's
real memberships decide everything else.

### What skipping tests costs, and the mitigation

The risk concentrates in four pure functions — `pricing`, `policy`, `coupons`,
`order_state` — where a wrong number is silent rather than loud.

Precedent from this project: `PATCH /users/{id}` was verified by running ten
hand-picked cases and reported working. It returned a 500 on `{"city": null}`,
found only when someone asked a question the ten cases did not cover.

Mitigation, since there is no suite:

- [ ] `ck_orders_total_reconciles` already makes a pricing bug an insert failure
      rather than a wrong charge — the database is doing the work a test would
- [ ] Every step's **Acceptance** block gets actually executed and its output
      shown, not asserted from reading the code
- [ ] The seeder runs the same `pricing.quote` the API does, so 800 seeded
      orders are themselves a consistency check — any disagreement shows up as a
      CHECK violation during seeding

## Progress

| Step | Area | Endpoints | Done |
|---|---|---|---|
| 0 | Foundations | — | [ ] |
| 1 | Seed data | — | [ ] |
| 2 | Discovery & catalog reads | 7 | [ ] |
| 3 | Addresses | 6 | [ ] |
| 4 | Pricing / quote | 1 | [ ] |
| 5 | Orders core | 3 | [ ] |
| 6 | Status & state machine | 3 | [ ] |
| 7 | Cancellation + auto refund | 1 | [ ] |
| 8 | Payments | 4 | [ ] |
| 9 | Refunds | 4 | [ ] |
| 10 | Coupons | 5 | [ ] |
| 11 | Delivery & ETA | 5 | [ ] |
| 12 | Users list | 1 | [ ] |
| 13 | Catalog admin | 10 | [ ] |
| 14 | Hardening | — | [ ] |
| 15 | **Auth, identity & staff scoping** | 9 | [~] endpoints done, retrofit part-done |
| A | Admin UI (sqladmin) — see `DASHBOARD_PLAN.md` | — | [ ] |
| B–E | Monitoring dashboard (Next.js) | +4 metrics | [ ] |
| | **Total** | **59 (+4 metrics)** | |

`DASHBOARD_PLAN.md` still says six metrics endpoints; four were built
(`summary`, `orders-over-time`, `funnel`, `restaurants`). The table above counts
what exists.

---

## Step 0 — Foundations

Retrofitting these across 50 endpoints is the expensive path. Do them first.

**Create**

- [ ] `app/core/pagination.py` — `LimitOffset` dependency (`limit` default 20,
      **max 100**; `offset` default 0) and a generic `Page[T]` response model
      returning `{items, total, limit, offset}`
- [ ] `app/core/errors.py` — one place that builds 404 / 409 / 422 responses,
      so messages stay uniform
- [ ] `app/services/__init__.py` — empty package; logic lands here, not in routers
- [ ] `app/repositories/__init__.py` — empty package for query helpers
- [ ] Move the existing `_not_found` out of `app/routers/users.py` into
      `app/core/errors.py`

**Acceptance**

- [ ] `limit=500` is rejected with 422, not silently served
- [ ] Existing 4 user endpoints still pass their checks
- [ ] `uv run fastapi dev` starts clean

---

## Step 1 — Seed data

Catalog endpoints cannot be tested against an empty database. This comes first.

**Create** `app/seed/` — one module per domain, `run.py` as the entry point.

- [ ] `app/seed/run.py` — `uv run python -m app.seed.run [--reset]`
- [ ] **Fixed RNG seed (42)** — identical dataset every run, or tests drift
- [ ] Idempotent: re-running does not duplicate. `--reset` truncates first

**Data**

- [ ] 8 cuisines — North Indian, South Indian, Chinese, Biryani, Pizza,
      Desserts, Beverages, Street Food
- [ ] 25 restaurants across 3 cities, realistic `avg_prep_minutes`,
      opening hours, lat/lng
- [ ] 1 `restaurant_policies` row each — **vary them**: cancellation windows
      3–15 min, refund SLA 24–72 h, some with `free_delivery_above`, some without
- [ ] 4–6 menu categories per restaurant
- [ ] 18–28 menu items per restaurant (~550 total), realistic prices, veg flags,
      spice levels
- [ ] 150 users + ~280 addresses (exactly one default each)
- [ ] 15 delivery partners
- [ ] 10 coupons — flat, percent-with-cap, restaurant-scoped, cuisine-scoped,
      one expired, one at its usage limit
- [ ] 800 orders over 90 days, **prices computed by the pricing service**, not
      hand-rolled (Step 4 may need to come partly first — that is fine)
- [ ] Order items 1–5 per order (~2,400)
- [ ] Status mix ≈ 70% delivered · 12% cancelled · 18% live
- [ ] Payments for every order; ~4% failed then retried successfully
- [ ] Refunds on ~8% of orders, mixed statuses
- [ ] Deliveries for every order past `preparing`
- [ ] `order_status_events` for every transition — no order with an empty timeline

**Edge cases to seed deliberately** (these are what you will demo and test against)

- [ ] An order still inside its cancellation window
- [ ] An order just past it (cancelling now incurs a fee)
- [ ] A refund past its `sla_due_at` — breached
- [ ] A coupon at `usage_limit_total`
- [ ] A payment that failed and was retried
- [ ] A restaurant currently closed by its own hours
- [ ] A menu item with `is_available = false`

**Acceptance**

- [ ] Two consecutive `--reset` runs produce identical row counts and identical totals
- [ ] Zero rows violate `ck_orders_total_reconciles`
- [ ] `select count(*) from orders where id not in (select order_id from order_status_events)` returns 0

---

## Step 2 — Discovery & catalog reads (7 endpoints)

No writes, no state. The cheapest way to prove the schema is right.

**Create** `app/routers/catalog.py`, `app/schemas/catalog.py`,
`app/repositories/catalog.py`

- [ ] `GET /cuisines`
- [ ] `GET /restaurants` — filters `city`, `cuisine`, `q`, `open_now`,
      `min_rating`, `max_price_for_two`; sort `rating` / `price_for_two` / `name`; paginated
- [ ] `GET /restaurants/{id}` — includes cuisines and policy
- [ ] `GET /restaurants/{id}/menu` — categories with items nested, `sort_order` respected
- [ ] `GET /restaurants/{id}/menu/search` — `q` over name + description,
      filters `is_veg`, `max_price`, `spice_level`
- [ ] `GET /restaurants/{id}/policy`
- [ ] `GET /menu-items/{id}`

**Acceptance**

- [ ] `GET /restaurants` with no filters returns 25, correctly paginated
- [ ] `open_now=true` respects `opens_at` / `closes_at`
- [ ] Menu endpoint issues **one** query, not one per category (check the SQL log)
- [ ] Unknown restaurant id -> 404 everywhere

---

## Step 3 — Addresses (6 endpoints)

Blocks ordering: an order cannot exist without a delivery address.

**Create** `app/routers/addresses.py`, `app/schemas/address.py`

- [ ] `POST /users/{user_id}/addresses` — first address becomes default automatically
- [ ] `GET /users/{user_id}/addresses`
- [ ] `GET /addresses/{id}`
- [ ] `PATCH /addresses/{id}`
- [ ] `DELETE /addresses/{id}` — 409 if a live order references it
- [ ] `PUT /addresses/{id}/default`

**Acceptance**

- [ ] Setting a new default clears the old one **in the same transaction** —
      otherwise `ix_addresses_one_default_per_user` rejects the write
- [ ] A user's second address does not become default
- [ ] Deleting the default promotes another address, or 409s if it is the last one

---

## Step 4 — Pricing service + quote (1 endpoint)

**The keystone.** Everything downstream depends on one correct total.

**Create** `app/services/pricing.py`, `app/services/eta.py`

- [ ] `pricing.quote(restaurant, items, address, coupon?) -> QuoteResult` —
      pure function, no database writes
- [ ] `eta.promised_at(restaurant, distance_km, placed_at)`
- [ ] Distance: haversine from address lat/lng to restaurant lat/lng
- [ ] Delivery fee: `base + per_km × distance`, waived above `free_delivery_above`
- [ ] Tax on `subtotal` (pick a rate, put it in one named constant)
- [ ] `POST /orders/quote`

**Validations the quote must perform**

- [ ] Restaurant exists, is active, and is open now
- [ ] Distance within `max_delivery_distance_km` -> else 422
- [ ] Subtotal at or above `min_order_value` -> else 422
- [ ] Every item exists, belongs to that restaurant, and `is_available`
- [ ] Returns `promised_at` and `cancellable_until` alongside the breakdown

**Acceptance**

- [ ] Quote total satisfies `ck_orders_total_reconciles` arithmetic exactly
- [ ] Called twice with the same input -> identical output, zero rows written
- [ ] **The seeder uses this same function.** One pricing implementation, not two

---

## Step 5 — Orders core (3 endpoints)

**Create** `app/routers/orders.py`, `app/schemas/order.py`,
`app/repositories/orders.py`

- [ ] `POST /orders` — accepts `Idempotency-Key`; writes order + items +
      first `order_status_events` row in **one** transaction
- [ ] `GET /orders` — filter `user_id`, `status`, `from`, `to`; paginated
- [ ] `GET /orders/{id}` — full detail with items

**Rules**

- [ ] Re-uses `pricing.quote` — never recomputes totals inline
- [ ] Snapshots `item_name` and `unit_price` onto `order_items`
- [ ] Freezes `cancellable_until` and `promised_at` at placement
- [ ] Repeat `Idempotency-Key` returns the original order, does not create a second

**Acceptance**

- [ ] Placing an order writes exactly one `order_status_events` row (`-> pending`)
- [ ] Same key twice -> one order, two identical 201s
- [ ] Changing a menu price afterwards does not change the historic order

---

## Step 6 — Status & state machine (3 endpoints)

**Create** `app/services/order_state.py`

- [ ] Legal transition map matching the diagram in `DATABASE_DESIGN.md`
- [ ] Each transition records who is allowed to make it (`user` / `restaurant` /
      `system`)
- [ ] `GET /orders/{id}/status` — status, `promised_at`, ETA remaining,
      `cancellable_until`
- [ ] `GET /orders/{id}/events` — the timeline
- [ ] `PATCH /orders/{id}/status`

**Acceptance**

- [ ] Illegal transition (`delivered -> preparing`) -> 409
- [ ] Every accepted transition writes an event row — no silent updates
- [ ] `delivered` sets `delivered_at`

---

## Step 7 — Cancellation + automatic refund (1 endpoint)

The most interesting endpoint in phase 1, and the reason the schema freezes policy.

- [ ] `POST /orders/{id}/cancel`
- [ ] `app/services/policy.py` — `can_cancel(order) -> (allowed, fee, reason)`

**It must, in one transaction**

- [ ] Reject `delivered` / already `cancelled` -> 409
- [ ] Compare `now()` against the order's own `cancellable_until`
- [ ] Inside the window -> full refund. Outside -> apply `cancellation_fee_percent`
- [ ] Set status `cancelled`, set `cancelled_at` and `cancellation_reason`
- [ ] Write the `order_status_events` row
- [ ] Create the `refunds` row with `sla_due_at = now() + refund_sla_hours`

**Acceptance**

- [ ] Seeded inside-window order -> full refund, zero fee
- [ ] Seeded past-window order -> fee applied, refund is the remainder
- [ ] Refund amount never exceeds what was captured
- [ ] Editing the restaurant's policy afterwards does **not** change either result

---

## Step 8 — Payments (4 endpoints)

Simulated provider. No real gateway.

**Create** `app/routers/payments.py`, `app/services/payment_sim.py`

- [ ] `POST /orders/{id}/payments` — idempotent
- [ ] `GET /orders/{id}/payments` — all attempts, **including failures**
- [ ] `GET /payments/{id}`
- [ ] `POST /payments/{id}/callback` — `authorized` -> `captured`

**Acceptance**

- [ ] A failed attempt persists as a row; the retry is a second row
- [ ] Capture sets `captured_at`
- [ ] Paying an already-captured order -> 409

---

## Step 9 — Refunds (4 endpoints)

- [ ] `POST /orders/{id}/refunds` — manual / goodwill
- [ ] `GET /orders/{id}/refunds`
- [ ] `GET /refunds/{id}` — include whether `sla_due_at` is breached
- [ ] `POST /refunds/{id}/complete`

**Acceptance**

- [ ] Sum of refunds never exceeds captured — enforced in the service, since the
      schema cannot check across rows
- [ ] The seeded breached refund reports `sla_breached: true`

---

## Step 10 — Coupons (5 endpoints)

**Create** `app/routers/coupons.py`, `app/services/coupons.py`

- [ ] `GET /coupons` — active and currently valid
- [ ] `POST /coupons/validate` — code + cart -> applicable?, discount, or reason
- [ ] `GET /coupons/{code}`
- [ ] `POST /coupons` (admin)
- [ ] `PATCH /coupons/{id}` (admin)

**Validation must check all seven** — missing one is how coupons get abused

- [ ] `is_active`
- [ ] Inside `valid_from` / `valid_until`
- [ ] Subtotal at or above `min_order_value`
- [ ] Scope matches restaurant or cuisine
- [ ] `times_used < usage_limit_total`
- [ ] This user's redemptions `< usage_limit_per_user`
- [ ] Percent discounts capped by `max_discount_amount`

**Then**

- [ ] Wire into `pricing.quote` — revisit Step 4
- [ ] `POST /orders` writes the `coupon_redemptions` row and increments `times_used`
      in the same transaction

**Acceptance**

- [ ] Seeded exhausted coupon -> rejected with a clear reason
- [ ] Seeded expired coupon -> rejected
- [ ] Percent coupon on a large cart -> capped, not unbounded
- [ ] Same user cannot exceed their per-user limit

---

## Step 11 — Delivery & ETA (5 endpoints)

**Create** `app/routers/delivery.py`

- [ ] `POST /orders/{id}/delivery/assign` — picks an available partner
- [ ] `GET /orders/{id}/delivery` — partner, status, live ETA
- [ ] `PATCH /deliveries/{id}` — picked up / delivered
- [ ] `GET /delivery-partners` (admin)
- [ ] `POST /delivery-partners` (admin)

**Acceptance**

- [ ] Assigning twice -> 409 (`deliveries.order_id` is unique)
- [ ] Marking delivered also advances the order to `delivered` and writes its event
- [ ] Live ETA recomputes from `promised_at`, never a stored countdown

---

## Step 12 — Users list (1 endpoint)

- [ ] `GET /users` — filter `city`, `is_active`, `q`; paginated

---

## Step 13 — Catalog admin (10 endpoints)

Last, because seed data covers phase 1 content.

- [ ] `POST /restaurants`
- [ ] `PATCH /restaurants/{id}`
- [ ] `DELETE /restaurants/{id}` — 409 if orders reference it
- [ ] `PUT /restaurants/{id}/policy`
- [ ] `POST /restaurants/{id}/menu-categories`
- [ ] `PATCH /menu-categories/{id}`
- [ ] `DELETE /menu-categories/{id}`
- [ ] `POST /menu-items`
- [ ] `PATCH /menu-items/{id}`
- [ ] `DELETE /menu-items/{id}` — 409 if `order_items` reference it

---

## Step 14 — Hardening

- [x] **D2** — reversed, not deferred. The endpoints are done; the
      authorization retrofit is not. Step 15, below
- [x] **D3** — deferred, decision recorded above
- [ ] Basic auth on `/admin` (sqladmin) — the one exception, since it exposes
      write forms over all 20 tables
- [ ] `/health` stops returning `str(exc)` to callers — it currently leaks
      database internals
- [ ] Structured logging with a request id
- [ ] CORS configured for the frontend origin
- [x] Regenerate `docs/schema.mmd` if any model changed — done for
      `restaurant_staff` and `users.auth_user_id`
- [ ] Re-run `get_advisors` — confirm no new security findings

---

## Step 15 — Auth, identity & staff scoping (9 endpoints)

D2's reversal. **The endpoints are built. The retrofit is half done**, and the
half that is left is the part that matters.

**Built already** — do not rewrite these, import them

| Module | Provides |
|---|---|
| `app/config.py` | `require_env`, `env_flag`, `is_auth_enabled` |
| `app/services/auth.py` | `verify_token(token) -> claims`, `TokenError`. JWKS-cached, ES256/RS256 only |
| `app/dependencies/identity.py` | `CurrentUser`, `OptionalUser`, `CurrentClaims`, `CurrentStaff`, `require_staff()`, `unauthenticated()`, `forbidden()`, and `UNAUTHENTICATED` / `FORBIDDEN` / `NO_PROFILE` for `responses=` |
| `app/dependencies/scope.py` | `staff_of_order`, `staff_of_delivery`, `manager_of_restaurant`, `manager_of_menu_category`, `manager_of_menu_item`, `OrderListRestaurants`, `RestaurantScopeDep`. Resolves *which* restaurant governs a row the caller named indirectly, then defers to `require_staff` |

**Endpoints — all nine shipped** (`app/routers/me.py`, `app/routers/staff.py`)

- [x] `POST /auth/link` — create or claim the `public.users` row for the bearer
      token's `sub`. **The path is not negotiable**: `PROFILE_LINK_PATH` in
      `identity.py` defines it and the 404 for an unlinked token names it. This
      is `/auth/link`, not the `/auth/profile` earlier drafts predicted — the
      constant is the authority, not this document
- [x] `GET /me` — the caller's own profile
- [x] `PATCH /me` — same `UserUpdate` schema as `PATCH /users/{id}`, subject
      taken from the token
- [x] `GET /me/orders` — the caller's own orders, paginated
- [x] `GET /me/restaurants` — the restaurants the caller may act for, with their
      role in each. What the partner app calls on load
- [x] `GET /restaurants/{restaurant_id}/staff` — manager and above
- [x] `POST /restaurants/{restaurant_id}/staff` — owner only
- [x] `PATCH /staff/{staff_id}` — role and `is_active`; owner only. Flat, not
      nested: a membership id is already unique, and two ids in one path can
      disagree
- [x] `DELETE /staff/{staff_id}` — owner only

**Then the retrofit, which is the larger half**

- [x] Seed `restaurant_staff` — every restaurant carries at least one active
      owner, plus `manager`/`staff` rows and a few inactive ones. Row totals
      drift as the seeder re-runs, so assert the invariant. The "every scoped route 403s for
      everyone" blocker is gone
- [x] Partner-facing catalog writes get scope checks — all 10 catalog-admin
      routes go through `manager_of_*`
- [x] `GET /orders` narrows to the caller's restaurants;
      `PATCH /orders/{id}/status` and `PATCH /deliveries/{id}` require staff of
      the governing restaurant
- [ ] The rest of orders — `GET /orders/{id}`, `/status`, `/events`,
      `POST /orders`, `/cancel`, `/quote` — still answer to anyone
- [ ] `/users/*`, `/addresses/*`, `/payments/*`, `/refunds/*`, `/coupons/*` and
      `/delivery-partners` are untouched. `PATCH /users/{id}` still takes any
      integer
- [ ] `POST /restaurants` requires only an identity, because nobody can be staff
      of a restaurant that does not exist yet. There is no platform-admin role
      in the schema to check instead — that is a real gap, not an oversight
- [ ] `AUTH_ENABLED` must be **true** in any deployed environment. It is
      currently **false** in `.env`. Assert it loudly at startup rather than
      trusting the file that ships

**Acceptance**

- [x] A real token minted by the project verifies; a tampered one, an HS256
      re-signing with the same `kid`, and garbage all 401 with the single body
      `{"detail": "Invalid or expired token"}` — the real reason goes to the log
- [x] An unlinked token gets 404 naming `POST /auth/link`; after calling it,
      the same token gets 200
- [x] A `manager` passes a `>= staff` route and 403s on an `owner` route
- [x] Staff of restaurant A get 403, not 404, on restaurant B's staff list
- [x] `AUTH_ENABLED=false` logs `DEV AUTH:` on every request, and
      `X-Dev-User-Id` alone with auth **on** is 401, never a bypass
- [ ] The last active owner of a restaurant cannot be demoted, deactivated or
      deleted — implemented in `staff.py`; not yet exercised end to end here
- [ ] No endpoint outside the list above still trusts a client-supplied identity

> Test note: `fastapi.testclient.TestClient` gives each request a fresh event
> loop, which breaks the asyncpg pool and produces spurious 500s. Drive the app
> with `httpx.ASGITransport` inside a single `asyncio.run`.

---

## Notes carried in from earlier work

- **`create_all` is still in `main.py`.** It creates but never alters. When a
  model changes, apply the change through an MCP migration and keep the model in
  sync by hand — `create_all` will not do it and will not warn you.
- **RLS is on for every `public` table with no policies** — re-verified
  2026-08-20 across all **21** (the count rose from 20 when `menu_item_images`
  arrived from concurrent work; it is not yet described in `DATABASE_DESIGN.md`).
  `restaurant_staff` included. Correct while the API connects as `postgres` and
  bypasses RLS. It stays on because the publishable key is a live exposure
  vector the moment anything talks to PostgREST directly — see
  `DATABASE_DESIGN.md` §9.
- **`SUPABASE_SECRET_KEY` still needs rotating** — it appeared in a chat
  transcript and is sitting in `.env`. Still unrotated as of 2026-08-20, and now
  more urgent than when it was noted: the same `.env` holds the auth
  configuration.
