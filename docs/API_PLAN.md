# Foodishi AI — Phase 1 API Plan

**Scope:** REST APIs only. No Redis, no caching layer, no AI/agent work, no
background workers. Those are phase 2+.

**Built on:** the schema in `DATABASE_DESIGN.md`, already live — 21 tables in
`public`, of which `restaurant_staff` is the one this document depends on.

**Status today (2026-08-20):** `app.openapi()` reports **69 operations**, up
from 60 earlier the same day. Every domain below is built, §2.11 and §2.12
included — the nine identity and staff endpoints D2's reversal added landed
after this document first predicted six of them. The counts below are read off
`app.openapi()`, not off this plan.

| | Count |
|---|---|
| Phase-1 domain endpoints (§2.1–§2.10) | 50 |
| Identity (§2.11) | 4 |
| Restaurant staff (§2.12) | 5 |
| `/users` endpoints predating this plan | 4 |
| `/admin/metrics/*` (`DASHBOARD_PLAN.md`) | 4 |
| `GET /` and `GET /health` | 2 |
| **Live today** | **69** |

**What is outstanding is no longer endpoints — it is the authorization retrofit
on the endpoints that already exist.** Catalog admin, `GET /orders`,
`PATCH /orders/{id}/status` and `PATCH /deliveries/{id}` are scoped. The rest of
orders, plus users, addresses, payments, refunds, coupons and delivery partners,
are still open to anyone who can reach the port. See §5.1.

**Why nine, not the six this plan predicted.** Three paths moved and one was
added while the endpoints were written, and the code is the authority:

| Predicted here | Shipped as | Why |
|---|---|---|
| `POST /auth/profile` | **`POST /auth/link`** | `PROFILE_LINK_PATH` in `app/dependencies/identity.py` is the single definition, and it says `/auth/link`. The 404 an unlinked token receives names whatever that constant holds |
| `GET /auth/me` | **`GET /me`** + **`GET /me/restaurants`** | Profile and restaurant list are different shapes with different cache lifetimes; the partner app wants the second on load and the first rarely |
| — | **`PATCH /me`**, **`GET /me/orders`** | The token-subject twins of `PATCH /users/{id}` and `GET /orders?user_id=`. Adding them is what lets the retrofit eventually *refuse* the id-taking versions instead of merely reading the id from the token |
| `PATCH`/`DELETE /restaurants/{rid}/staff/{sid}` | **`PATCH`/`DELETE /staff/{sid}`** | A membership id is already unique; nesting it invited a mismatched pair of ids that then has to be validated. The owner check re-derives the restaurant from the row instead |

---

## 0. Decide these before writing endpoint #1

Three conventions that are cheap now and expensive to retrofit across 50
endpoints. All three were settled and applied. A fourth — authentication — was
deferred, and is the counter-example: it is being retrofitted across the 60
endpoints that existed before it landed, which is exactly what §0 exists to
prevent. See §5.1.

### 0.1 Response shape

Your standing rules call for an envelope (`{success, data, error, meta}`).
The four endpoints you have return bare objects with meaning carried by the HTTP
status code. **Pick one now.**

Recommendation: **stay bare.** FastAPI's `response_model`, the generated OpenAPI
schema, and client codegen all assume it, and an envelope duplicates what the
status line already says. Errors already have a consistent shape — a `detail`
string for handled cases, a `detail` array for validation.

If you want the envelope instead, it costs one `APIRoute` subclass and a change
to the four existing endpoints — but do it before there are fifty.

### 0.2 Pagination

Every list endpoint, same contract from day one:

```
GET /restaurants?limit=20&offset=0

{ "items": [...], "total": 137, "limit": 20, "offset": 0 }
```

Cap `limit` at 100. An unbounded list endpoint is the single easiest way to
take down a database.

### 0.3 Idempotency on money paths

`POST /orders` and `POST /orders/{id}/payments` must accept an
`Idempotency-Key` header. A double-tap on a slow network must not create two
orders or charge twice. Store the key with the order; a repeat returns the
original result rather than creating a second row.

---

## 1. Service layer — build before the endpoints

Four pieces of logic that more than one endpoint needs. Written once, in
`app/services/`, they stay consistent. Written inline in routers, they drift.

| Service | Responsibility | Used by |
|---|---|---|
| `pricing` | items + address + coupon -> subtotal, packaging, delivery, tax, discount, total | `POST /orders/quote`, `POST /orders`, the seeder |
| `policy` | can this order be cancelled? what fee? what refund SLA? | cancel, refund |
| `eta` | prep time + distance -> `promised_at` | quote, place, delivery |
| `order_state` | which status transitions are legal, and who may make them | every status change |

**The pricing service is the important one.** If the API computes a total one
way and the seeder another, your test data lies to you. One function, both
callers.

---

## 2. Endpoints by domain

Legend: **P0** = required for a working order flow · **P1** = needed to operate
the product · **P2** = nice to have in phase 1

### 2.1 Users — 1 to add

| Method | Path | Pri | Notes |
|---|---|---|---|
| POST | `/users` | done | |
| GET | `/users/{id}` | done | |
| PATCH | `/users/{id}` | done | |
| DELETE | `/users/{id}` | done | 409 when the user has orders |
| GET | `/users` | P1 | paginated; filter `city`, `is_active`, `q` |

### 2.2 Addresses — 6

An order cannot exist without a delivery address, so this blocks ordering.

| Method | Path | Pri | Notes |
|---|---|---|---|
| POST | `/users/{user_id}/addresses` | P0 | first address becomes default automatically |
| GET | `/users/{user_id}/addresses` | P0 | |
| GET | `/addresses/{id}` | P1 | |
| PATCH | `/addresses/{id}` | P1 | |
| DELETE | `/addresses/{id}` | P1 | 409 if referenced by a live order |
| PUT | `/addresses/{id}/default` | P0 | must clear the old default in the same transaction — the partial unique index will reject it otherwise |

### 2.3 Discovery and catalog — 7 read endpoints

| Method | Path | Pri | Notes |
|---|---|---|---|
| GET | `/cuisines` | P0 | |
| GET | `/restaurants` | P0 | filters: `city`, `cuisine`, `q`, `open_now`, `min_rating`, `max_price_for_two`; sort: `rating`, `price_for_two`, `name`; paginated |
| GET | `/restaurants/{id}` | P0 | includes cuisines + policy |
| GET | `/restaurants/{id}/menu` | P0 | categories with items nested, `sort_order` respected |
| GET | `/restaurants/{id}/menu/search` | P1 | `q` over item name + description, `is_veg`, `max_price`, `spice_level`. **This is the menu-Q&A groundwork** — structured filters now, semantic search much later |
| GET | `/restaurants/{id}/policy` | P0 | cancellation window, refund SLA, delivery charges — the answers support needs |
| GET | `/menu-items/{id}` | P1 | |

### 2.4 Catalog administration — 10, all P2

Seed data covers phase 1 content, so these are only needed if you want to edit
the catalog through the API rather than SQL.

`POST` / `PATCH` / `DELETE` on `/restaurants`, `/restaurants/{id}/menu-categories`,
`/menu-items`, plus `PUT /restaurants/{id}/policy`.

### 2.5 Pricing — 1, and it gates everything

| Method | Path | Pri | Notes |
|---|---|---|---|
| POST | `/orders/quote` | P0 | body: `restaurant_id`, `items[]`, `address_id`, `coupon_code?` |

Returns the full breakdown plus `promised_at` and `cancellable_until` — with no
side effects. The cart screen calls it on every change, and `POST /orders`
calls the same service so the quoted price and the charged price cannot diverge.

Validates: restaurant open, address within `max_delivery_distance_km`, subtotal
over `min_order_value`, every item available.

### 2.6 Orders — 7

| Method | Path | Pri | Notes |
|---|---|---|---|
| POST | `/orders` | P0 | idempotent; writes order + items + first status event in one transaction |
| GET | `/orders` | P0 | user's orders; filter `status`, `from`, `to`; paginated |
| GET | `/orders/{id}` | P0 | full detail with items |
| GET | `/orders/{id}/status` | P0 | lightweight: status, `promised_at`, ETA remaining, `cancellable_until` |
| GET | `/orders/{id}/events` | P1 | the status timeline |
| POST | `/orders/{id}/cancel` | P0 | policy-checked — see below |
| PATCH | `/orders/{id}/status` | P0 | restaurant/system transitions, validated against the state machine |

**Cancellation is the most interesting endpoint in phase 1.** It must:

1. Reject if status is already `delivered` or `cancelled` -> 409
2. Compare `now()` against the order's frozen `cancellable_until`
3. Inside the window -> full refund. Outside -> apply `cancellation_fee_percent`
4. Write the status change **and** the `order_status_events` row **and** the
   `refunds` row in one transaction
5. Set `sla_due_at` = now + the policy's `refund_sla_hours`

Every status change writes an `order_status_events` row. No exceptions — that
table is the only answer to "when did this happen and who did it."

### 2.7 Payments — 4

Simulated provider. No real gateway in phase 1.

| Method | Path | Pri | Notes |
|---|---|---|---|
| POST | `/orders/{id}/payments` | P0 | idempotent; a failed attempt is a **row**, not an error to discard |
| GET | `/orders/{id}/payments` | P1 | all attempts, including failures |
| GET | `/payments/{id}` | P2 | |
| POST | `/payments/{id}/callback` | P1 | simulates the provider confirming — moves `authorized` -> `captured` |

### 2.8 Refunds — 4

| Method | Path | Pri | Notes |
|---|---|---|---|
| POST | `/orders/{id}/refunds` | P1 | manual/goodwill; cancellation creates one automatically |
| GET | `/orders/{id}/refunds` | P0 | |
| GET | `/refunds/{id}` | P1 | include `sla_due_at` and whether it is breached |
| POST | `/refunds/{id}/complete` | P1 | simulates settlement |

Guard: total refunded must never exceed captured. Enforce in the service —
the schema does not check this across rows.

### 2.9 Coupons — 5

| Method | Path | Pri | Notes |
|---|---|---|---|
| GET | `/coupons` | P1 | active and currently valid |
| POST | `/coupons/validate` | P0 | code + cart -> applicable?, discount, or the reason it fails |
| GET | `/coupons/{code}` | P2 | |
| POST | `/coupons` | P2 | admin |
| PATCH | `/coupons/{id}` | P2 | admin |

Validation must check **all** of: active, inside `valid_from`/`valid_until`,
subtotal over `min_order_value`, scope matches the restaurant or cuisine,
`times_used` under `usage_limit_total`, and this user's redemption count under
`usage_limit_per_user`. Missing any one of these is how coupons get abused.

### 2.10 Delivery and ETA — 5

| Method | Path | Pri | Notes |
|---|---|---|---|
| POST | `/orders/{id}/delivery/assign` | P0 | picks an available partner |
| GET | `/orders/{id}/delivery` | P0 | partner, status, live ETA |
| PATCH | `/deliveries/{id}` | P0 | partner updates: picked up, delivered |
| GET | `/delivery-partners` | P2 | admin |
| POST | `/delivery-partners` | P2 | admin |

### 2.11 Identity — 4, built

Added by D2's reversal on 2026-08-20, and built the same day. Sign-in itself is
not an endpoint here: the client talks to Supabase Auth directly and arrives
with an access token. These four turn that token into something the rest of the
schema can use. Implemented in `app/routers/me.py`.

| Method | Path | Pri | Notes |
|---|---|---|---|
| POST | `/auth/link` | P0 | create or claim the `public.users` row for the token's `sub`. **The path is not a choice** — `PROFILE_LINK_PATH` in `app/dependencies/identity.py` defines it, and the 404 an unlinked token receives names it. Always 200, never 201 |
| GET | `/me` | P0 | the caller's own profile. `CurrentUser`, so an unlinked token gets the 404 that points at `POST /auth/link` |
| PATCH | `/me` | P1 | same `UserUpdate` schema as `PATCH /users/{id}` — same empty-body and explicit-null rejection. The only difference is that the subject comes from the token, so there is no id to aim elsewhere |
| GET | `/me/orders` | P0 | the caller's own orders, paginated, with `restaurant_id`, `status` and `live` filters |

`POST /auth/link` is the one endpoint that may not require a linked profile — it
is what creates the link. It takes `CurrentClaims`, not `CurrentUser`;
`CurrentUser` would 404 pointing at the endpoint you are already calling.

Three cases it handles, all reachable, tried in this order:

1. **Token already linked** — return that profile, 200. Idempotent by
   construction: `auth_user_id` is UNIQUE, and a frontend that calls this on
   every sign-in must not accumulate profiles
2. **Token whose e-mail matches an existing unlinked profile** — claim that row
   rather than creating a duplicate. This is how the 150 seeded users ever
   acquire identities, and it is the case worth the code: those rows already own
   orders, addresses and coupon redemptions keyed to their integer id. Matching
   on e-mail is safe because Supabase verified it and `users.email` is unique.
   If the row turns out to belong to somebody else's identity, that is a 409
3. **Nothing matches** — create a fresh profile

Two failure modes that are easy to miss and are handled explicitly:

- **A token can outlive the account it names.** Delete the Supabase user and its
  unexpired tokens still pass signature checks; the insert then fails on the
  `auth.users` foreign key. That is a 422 about a missing identity, not a 409
  about a duplicate e-mail — different SQLSTATEs, deliberately told apart
- **Claims are external data.** The e-mail on the token is validated before it
  becomes a row; a token carrying no e-mail is 422, because there is nothing to
  match or store

### 2.12 Restaurant staff — 5, built

The authorization boundary for the partner app, exposed as CRUD. Implemented in
`app/routers/staff.py`.

| Method | Path | Pri | Min role | Notes |
|---|---|---|---|---|
| GET | `/restaurants/{restaurant_id}/staff` | P1 | manager | paginated; `require_staff` reads the id straight from the path |
| POST | `/restaurants/{restaurant_id}/staff` | P1 | owner | by `user_id`; 409 on the `uq_staff_user_restaurant` unique constraint |
| PATCH | `/staff/{staff_id}` | P1 | owner | `role` and `is_active`. Prefer deactivating to deleting |
| DELETE | `/staff/{staff_id}` | P2 | owner | for rows added by mistake |
| GET | `/me/restaurants` | P0 | — any signed-in caller | the restaurants this caller may act for, with their role in each. Unpaginated by design: one person works at a handful of restaurants, and a picker that arrives in pages is worse than one that arrives whole |

**Why the membership routes are flat.** `PATCH /restaurants/{rid}/staff/{sid}`
would carry two ids that can disagree, and validating that they agree is code
that exists only because of the shape of the URL. A membership id is already
unique, so the flat path re-derives the restaurant from the row and checks
ownership of *that*. The cost is that `require_staff()` cannot read a
`{restaurant_id}` off the path — these two routes call the owner check by hand
in the handler, which is why they take `Request`.

Three rules that are easy to get wrong, all implemented:

- **Wrong-restaurant is 403, not 404.** A 404 leaks whether a restaurant exists;
  more importantly, "you may not" and "it isn't there" are different answers and
  support needs to tell them apart. A row that genuinely does not exist is still
  404
- **The last active owner cannot be demoted, deactivated or deleted.** No CHECK
  can count rows, so it lives in the handler. An owner *may* step down — just
  not while they are the only one left, which would strand the restaurant
- **`POST /restaurants` makes its creator an owner** in the same transaction.
  Not a convenience: every other catalog write and `POST /.../staff` demand an
  existing membership, so a restaurant created without one could never be edited
  or staffed by anyone

**`restaurant_staff` is seeded** — every restaurant has at least one active
owner, alongside `manager` and `staff` rows and a few inactive ones. Row totals
drift as the seeder is re-run, so rely on the invariant rather than a count.
The blocking fact this section
used to carry ("zero rows, so everything 403s") is resolved. Note that
`AUTH_ENABLED=false` still fakes identity only, never permissions: the dev
header picks a `users.id`, and that user's real memberships decide the rest.

**No seeded user has an `auth_user_id`** — all 150 are null, which is correct.
They acquire one the first time the real person signs in and calls
`POST /auth/link` with a matching e-mail.

---

## 3. Totals

Read off `app.openapi()` on 2026-08-20, not off the tick-boxes below it.

| Domain | Endpoints | P0 | Built | Authorized |
|---|---|---|---|---|
| Users (`/users/*`) | 1 new (4 done) | 0 | yes | **no** |
| Addresses | 6 | 3 | yes | **no** |
| Discovery | 7 | 5 | yes | n/a — public by design |
| Catalog admin | 10 | 0 | yes | yes |
| Pricing | 1 | 1 | yes | **no** |
| Orders | 7 | 6 | yes | partly — 2 of 7 |
| Payments | 4 | 1 | yes | **no** |
| Refunds | 4 | 1 | yes | **no** |
| Coupons | 5 | 1 | yes | **no** |
| Delivery | 5 | 3 | yes | partly — 1 of 5 |
| **Subtotal — the original 50** | **50** | **21** | | |
| Identity (§2.11) | 4 | 3 | yes | yes — by construction |
| Restaurant staff (§2.12) | 5 | 1 | yes | yes |
| **Total, phase 1** | **59** | **25** | | |

Outside this table but inside the OpenAPI document: the 4 `/users` endpoints
that predate the plan, 4 `/admin/metrics/*` from `DASHBOARD_PLAN.md` (which
still says six — four were built), `GET /` and `GET /health`. That is the
difference between the 59 here and the **69 operations live today**.

The plan originally said 56 and then 66. Both numbers are superseded: the
identity and staff work shipped as 9 endpoints rather than 6, for the reasons in
the table at the top of this document.

**25 endpoints gets you a working order flow** — sign in, link, browse, quote,
place, track, cancel, refund. The other 34 are administration and convenience.

**Counting endpoints is not counting the work left, and now it is actively
misleading.** Every endpoint in this plan exists. What does not exist is a
consistent answer to *who may call it*: the `Authorized` column above has more
"no" than "yes" in it, and closing that column adds no rows to this table while
being most of the remaining effort. See §5.1.

---

## 4. Build order

Each step is testable before the next begins.

| # | Step | Why here |
|---|---|---|
| 1 | **Seed data** | Catalog endpoints cannot be tested against an empty database. 25 restaurants, ~550 items, coupons, partners. |
| 2 | Conventions: pagination helper, error shape, `app/services/` skeleton | Retrofitting these across 50 endpoints is the expensive path |
| 3 | Discovery reads (2.3) | No writes, no state — the cheapest way to prove the schema is right |
| 4 | Addresses (2.2) | Blocks ordering |
| 5 | **Pricing service + `/orders/quote`** | The keystone. Everything downstream depends on one correct total |
| 6 | `POST /orders`, `GET /orders`, `GET /orders/{id}` | |
| 7 | State machine + `PATCH /orders/{id}/status` + events | |
| 8 | Cancellation + automatic refund | First place policy actually bites |
| 9 | Payments | |
| 10 | Refunds (manual paths) | |
| 11 | Coupons | Feeds back into pricing — revisit step 5's service |
| 12 | Delivery + live ETA | |
| 13 | Catalog admin (2.4) | Last; seed data covers phase 1 |
| 14 | **Identity + staff (2.11, 2.12)** — done | Should have been step 2. It is here because D2 was reversed after the endpoints were written |
| 15 | **The authorization retrofit** — in progress | The price of step 14 landing fourteenth instead of second, paid once. Catalog admin, `GET /orders`, `PATCH /orders/{id}/status` and `PATCH /deliveries/{id}` are done; the rest are not |

---

## 5. Two things to settle — one settled, one still open

### 5.1 Authentication — settled 2026-08-20: Supabase Auth, phase 1

This section asked for a decision rather than an oversight. It got both, on the
same day: deferred (D2), then reversed within hours when `MONOREPO_PLAN.md`
committed to a partner app and restaurant scoping stopped being optional.

**Chosen:** JWT verification against Supabase Auth. Not an API key — an API key
identifies the *client*, and every question here is about the *user*.

Built and verified against the live project:

| Module | What it gives you |
|---|---|
| `app/services/auth.py` | `verify_token(token) -> claims`, `TokenError`. JWKS fetched once and cached; **ES256 / RS256 only**, so an HS256 forgery re-signed with the published key is rejected; 10 s leeway; every failure returns the same `"Invalid or expired token"` and logs the real reason |
| `app/dependencies/identity.py` | `CurrentUser`, `OptionalUser`, `CurrentClaims`, `CurrentStaff`, `require_staff(restaurant_id?, minimum_role?)`, `unauthenticated()`, `forbidden()`, and `UNAUTHENTICATED` / `FORBIDDEN` / `NO_PROFILE` for `responses=` |
| `app/config.py` | `require_env`, `env_flag`, `is_auth_enabled`. Reads `os.environ` after `app.db`'s `load_dotenv` — no pydantic-settings, which was removed deliberately |

Conventions these add to §0:

- **Every 401 carries `WWW-Authenticate: Bearer`** and one generic body. Error
  text never distinguishes expired from forged from malformed — that distinction
  is a probing oracle, and it goes to the log instead
- **404 for an unlinked token**, with a body naming `POST /auth/link` — the path
  comes from `PROFILE_LINK_PATH`, so the message cannot drift from the route. A
  valid token belonging to nobody is not an authentication failure
- **403 for a deactivated user and for wrong-restaurant staff.** `OptionalUser`
  returns `None` in all of these cases rather than raising, so public endpoints
  can personalize without gaining a failure mode
- **`AUTH_ENABLED=false`** substitutes an `X-Dev-User-Id` header for a token and
  logs `DEV AUTH:` at WARNING on every request. With auth on, that header alone
  is a 401, never a bypass. It must be true anywhere deployed

A third module joined them once the retrofit started:

| Module | What it gives you |
|---|---|
| `app/dependencies/scope.py` | `staff_of_order`, `staff_of_delivery`, `manager_of_restaurant`, `manager_of_menu_category`, `manager_of_menu_item`, `OrderListRestaurants`, `RestaurantScopeDep`. Endpoints rarely name a restaurant — they name a menu item, an order, a delivery — so the governing restaurant is looked up from the row server-side and never taken from the client. Every check funnels into `identity.require_staff`, so the membership rule, the 403 wording and the audit line have one definition |

**What is still open is the retrofit, and it is the larger half.** Progress as
of 2026-08-20:

| | Status |
|---|---|
| All 10 catalog-admin writes | scoped — manager of the restaurant that owns the row |
| `GET /orders` | scoped — narrowed to the caller's restaurants |
| `PATCH /orders/{id}/status` | staff of the order's restaurant |
| `PATCH /deliveries/{id}` | staff of the delivery's restaurant |
| `GET /orders/{id}`, `/status`, `/events`, `POST /orders`, `/cancel`, `/quote` | **open** |
| `/users/*`, `/addresses/*`, `/payments/*`, `/refunds/*`, `/coupons/*`, `/delivery-partners` | **open** |

So `GET /orders/7` still answers to anyone who asks, and `PATCH /users/{id}`
still takes any integer. The dependency that fixes each of these exists and is
one line per route — but *which* line, and what a partner versus a customer
versus an operator may reach, is a decision per endpoint. `GET /me/orders` and
`PATCH /me` exist precisely so the id-taking versions can eventually refuse a
caller who names somebody else rather than quietly reinterpreting the id. See
`IMPLEMENTATION_PLAN.md` Step 15.

### 5.2 Tests

There is still no test suite. The 50 endpoints above include a pricing engine,
a refund calculator, and a coupon validator — pure functions with exact
expected outputs, which is the easiest and highest-value code to test that
exists in this project.

Cost is roughly 40 lines of pytest + httpx fixtures once, then a few
assertions per endpoint.
