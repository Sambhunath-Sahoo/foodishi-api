# Foodishi AI — Phase 1 Test Plan

Executed by hand, not a test suite. No pytest, no coverage gate — the goal is to
prove the system behaves, and to say plainly where it does not.

## How to run this anywhere

Every step reads its target from the environment, so the same plan runs against a
laptop or a deployment.

```bash
export FOODISHI_API_URL=http://localhost:8000        # or https://api.foodishi.example
export FOODISHI_OPERATOR_URL=http://localhost:3000
export FOODISHI_PARTNER_URL=http://localhost:3001
export FOODISHI_CUSTOMER_URL=http://localhost:3002
# from foodishi-api/.env
export SUPABASE_URL= SUPABASE_PUBLISHABLE_KEY= SUPABASE_SECRET_KEY=
```

Steps are tagged:

- **[any]** — runs identically local or hosted
- **[local]** — needs the working tree (build output, server logs, seeding)

Supabase is remote in both cases, so every auth and database assertion is the same
either way.

### Differences that only appear when hosted

| Concern | Local | Hosted |
|---|---|---|
| CORS | `localhost:3000-3002` | the real frontend origins |
| `AUTH_ENABLED` | `false`, dev header fallback | **`true`**, fallback must be dead |
| Database host | direct connection works | direct is **IPv6-only** — most hosts need the Supavisor pooler on 6543 |
| `--reset` seeding | expected | **destructive**, must be blocked |

---

## Preconditions

| # | Check | Expected |
|---|---|---|
| P1 | `uv run python -m app.seed.run --reset` completes **[local]** | 25 restaurants · ~550 menu items · 150 users · ~800 orders |
| P2 | `GET /health` | `200 {"status":"ok","database":"reachable"}` |
| P3 | `GET /openapi.json` | 200, path count matches the router set |
| P4 | All tables RLS-enabled | 20/20 |
| P5 | Model ↔ database column parity | no missing columns |

## Test identities

Created through the Supabase Admin API with `email_confirm: true`, so **no inbox is
needed** — already proven working, including JWKS signature verification and
rejection of a tampered token.

| Handle | Role | Purpose |
|---|---|---|
| `cust-a` | customer | places, tracks, cancels |
| `cust-b` | customer | must never see cust-a's data |
| `owner-r1` | owner @ restaurant 1 | staff management, menu, orders |
| `mgr-r1` | manager @ restaurant 1 | orders + menu, **not** staff |
| `owner-r2` | owner @ restaurant 2 | the attacker in the scoping suite |

---

## Suite A — Discovery **[any]**

| # | Step | Expected |
|---|---|---|
| A1 | `GET /restaurants` | 200, 25 total, page of 20 |
| A2 | `GET /restaurants?limit=500` | **422** — the cap is enforced, not silently clamped |
| A3 | `GET /restaurants?city=Bengaluru` | only Bengaluru rows |
| A4 | `GET /restaurants?cuisine=biryani` | every row tagged biryani |
| A5 | `GET /restaurants?open_now=true` | consistent with `opens_at`/`closes_at` |
| A6 | `GET /restaurants/{id}` | includes cuisines **and** policy |
| A7 | `GET /restaurants/999999` | 404 |
| A8 | `GET /restaurants/{id}/menu` | categories in `sort_order`, items nested |
| A9 | A8 with SQL echo **[local]** | **one** query, not one per category |
| A10 | `GET /restaurants/{id}/menu/search?q=biryani&is_veg=true` | only veg matches |
| A11 | `GET /restaurants/{id}/policy` | cancellation window, refund SLA, fees |

## Suite B — Pricing and quote **[any]**

The keystone. If this is wrong, everything downstream charges wrong.

| # | Step | Expected |
|---|---|---|
| B1 | `POST /orders/quote` with a valid cart | 200 with the full breakdown |
| B2 | Arithmetic check | `total == subtotal + packaging + delivery + tax − discount`, exactly |
| B3 | Call B1 twice | byte-identical result, **zero** rows written |
| B4 | Cart below `min_order_value` | 422 naming the minimum |
| B5 | Address beyond `max_delivery_distance_km` | 422 stating both distances |
| B6 | Cart containing an unavailable item | 422 naming the item |
| B7 | Subtotal above `free_delivery_above` | `delivery_fee == 0` |
| B8 | Quote returns `promised_at` and `cancellable_until` | both present and future |

## Suite C — Order lifecycle **[any]**

| # | Step | Expected |
|---|---|---|
| C1 | `POST /orders` | 201; total matches the quote exactly |
| C2 | Same `Idempotency-Key` again | **same order id**, no second row |
| C3 | Order rows | exactly one `order_status_events` row (`→ pending`) |
| C4 | Change a menu price, re-read the order | historic `item_name`/`unit_price` unchanged |
| C5 | `PATCH /orders/{id}/status` pending→confirmed as restaurant | 200, event written |
| C6 | delivered→preparing | **409** |
| C7 | out_for_delivery→delivered as `user` | **409** — wrong actor |
| C8 | `GET /orders/{id}/status` | live `minutes_remaining`, `is_late`, `is_cancellable` |
| C9 | `GET /orders/{id}/events` | full timeline, chronological |

## Suite D — Cancellation and refunds **[any]**

Uses the deliberately seeded edge cases.

| # | Step | Expected |
|---|---|---|
| D1 | Cancel the **inside-window** seeded order | 200, `fee == 0`, full refund |
| D2 | Cancel the **past-window** seeded order | 200, fee applied, refund is the remainder |
| D3 | Cancel a delivered order | 409 |
| D4 | Cancel the same order twice | 409 |
| D5 | Rows after D1/D2 | status, event **and** refund all written |
| D6 | `sla_due_at` | `initiated_at + policy.refund_sla_hours` |
| D7 | Edit the restaurant's policy, re-read D1/D2 | **outcomes unchanged** — policy was frozen |
| D8 | `GET /refunds/{id}` on the seeded breached refund | `sla_breached: true` |
| D9 | Refund exceeding captured | 409 |

## Suite E — Coupons **[any]**

| # | Step | Expected |
|---|---|---|
| E1 | `POST /coupons/validate` FOODISHI20 on a qualifying cart | applicable, discount capped at `max_discount_amount` |
| E2 | `EXPIRED25` | refused — "expired" |
| E3 | `SOLDOUT` | refused — usage limit |
| E4 | Below `min_order_value` | refused, naming the minimum |
| E5 | Restaurant-scoped coupon at the wrong restaurant | refused |
| E6 | Same user past `usage_limit_per_user` | refused |
| E7 | Order placed with a coupon | `coupon_redemptions` row written, `times_used` incremented |
| E8 | Percent coupon on a very large cart | capped, not unbounded |

## Suite F — Payments, delivery, metrics **[any]**

| # | Step | Expected |
|---|---|---|
| F1 | `POST /orders/{id}/payments` | 201 authorized |
| F2 | Callback → captured | `captured_at` set |
| F3 | Pay an already-captured order | 409 |
| F4 | `GET /orders/{id}/payments` | includes failed attempts |
| F5 | Assign delivery twice | 409 (`order_id` is UNIQUE) |
| F6 | `GET /orders/{id}/delivery` | ETA recomputed live from `promised_at` |
| F7 | `GET /admin/metrics/summary` | counts reconcile with direct SQL |
| F8 | `GET /admin/metrics/funnel` | cancellation split inside vs outside window |

## Suite G — Authentication **[any]**

| # | Step | Expected |
|---|---|---|
| G1 | Admin-create a user with `email_confirm: true` | 200, confirmed, no inbox needed |
| G2 | Password sign-in | 200, ES256 access token |
| G3 | Verify the token against the live JWKS | valid; `kid` matches |
| G4 | Tamper one character | `InvalidSignatureError` |
| G5 | Protected endpoint, no token | 401 + `WWW-Authenticate` |
| G6 | Expired token | 401, message does **not** reveal why |
| G7 | `POST /auth/link` | profile created, or an unlinked one matched by email |
| G8 | `POST /auth/link` twice | idempotent — same profile, 200 both times, no duplicate |
| G9 | `GET /me` before linking | 404 pointing at `/auth/link` |
| G10 | `POST /auth/link` for an email already linked to another identity | 409, not a second profile |
| G11 | `POST /auth/link` with a token whose auth user was deleted | 422 naming the missing identity, not a 409 about email |
| G12 | `GET /me/restaurants` as a manager | only their restaurants, only active memberships |
| G13 | `DELETE /staff/{id}` for the last active owner | 409, restaurant keeps an owner |

> Paths, settled 2026-08-20 by reading the code rather than the plan: the
> constant is `PROFILE_LINK_PATH = "/auth/link"` in
> `app/dependencies/identity.py`, and the profile route is `GET /me`. An earlier
> revision of this suite "corrected" these to `/auth/profile` and `/auth/me` on
> the strength of a draft of `API_PLAN.md`; the draft was wrong and the routes
> were right. Assert against the constant, never against a literal — that is the
> whole reason it exists. See `API_PLAN.md` §2.11.

## Suite H — Authorization **[any]** ← the one that matters

A UI filter is bypassed by editing a URL. These prove the server refuses.

| # | Attack | Expected |
|---|---|---|
| H1 | `owner-r2` requests `GET /orders?restaurant_id=1` | **403**, not a filtered empty list |
| H2 | `owner-r2` PATCHes an order belonging to restaurant 1 | 403 |
| H3 | `owner-r2` PATCHes a menu item owned by restaurant 1 | 403 (restaurant resolved via the row, not the path) |
| H4 | `mgr-r1` adds staff | 403 — owner only |
| H5 | `mgr-r1` edits restaurant 1's menu | 200 — manager is allowed |
| H6 | `cust-a` reads `GET /orders?user_id={cust-b}` | must not return cust-b's orders |
| H7 | `cust-a` cancels cust-b's order | 403 |
| H8 | `GET /me/restaurants` as `owner-r1` | only restaurant 1 |
| H9 | Demote the last active owner | 409 |
| H10 | Deactivated staff member acts | 403 |

## Suite I — UI, driven with Playwright **[local]**

Real browser, real clicks, screenshots reviewed by eye — not just HTTP 200.

**Operator :3000**

| # | Step | Expected |
|---|---|---|
| I1 | Overview loads | KPI tiles show real seeded numbers, not zeros |
| I2 | Live board | orders grouped by status; late rows carry the crit stripe **and** chip |
| I3 | SLA watch | the seeded breached refund appears |
| I4 | Order drawer | items, timeline, payments, refunds |
| I5 | Dark mode toggle | tokens switch; no unreadable text |
| I6 | Narrow viewport | table scrolls in its own container; body does not scroll sideways |

**Partner :3001**

| # | Step | Expected |
|---|---|---|
| I7 | Order queue for restaurant 1 | only restaurant 1's orders |
| I8 | Tap Accept | status advances; card moves; event written |
| I9 | Menu availability toggle | persists after reload |
| I10 | Dev-mode ribbon | visible and honest about not being authorization |

**Customer :3002 at 390px**

| # | Step | Expected |
|---|---|---|
| I11 | Discovery + filters | veg-only and search change the results |
| I12 | Add to cart | quote recalculates on every change |
| I13 | Enter `SOLDOUT` | the **server's own** message appears, not "Invalid coupon" |
| I14 | Enter `FOODISHI20` | discount line appears; total drops by exactly that |
| I15 | Place order | 201; lands on tracking |
| I16 | Tracking | stepper, countdown, Cancel enabled only while cancellable |
| I17 | Cancel inside window | button reads "free for N more min"; result matches |
| I18 | Cancel past window | button states the **actual fee** before the tap |

## Suite J — Cross-cutting **[any]**

| # | Step | Expected |
|---|---|---|
| J1 | Every list endpoint with `limit=101` | 422 |
| J2 | Validation errors | `detail` array with `type`/`loc`/`msg` |
| J3 | Handled errors | `detail` string, human-readable |
| J4 | No 500s anywhere in the run | zero |
| J5 | `/health` with the database unreachable **[local]** | 503, not 200 |
| J6 | CORS preflight from a frontend origin | allowed |

## Cleanup

- Delete every test auth user via the Admin API
- Delete orders, refunds, payments and staff rows created during the run
- Confirm the seeded baseline is intact

## Results

Filled in on execution: **expected beside actual**, with failures stated plainly
rather than summarised away.

| Suite | Cases | Pass | Fail | Notes |
|---|---|---|---|---|
| A–J | | | | |
