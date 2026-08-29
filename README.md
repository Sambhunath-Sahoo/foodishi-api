# foodishi-api

Backend for Foodishi AI. See [`../FOODISHI_AI.md`](../FOODISHI_AI.md) for the roadmap.

## Run

```bash
uv run fastapi dev app/main.py
```

Docs: http://127.0.0.1:8000/docs

## Endpoints

75 routes. `/docs` is the list — this table would only rot. By group:

| Group | Covers |
| --- | --- |
| `/`, `/health` | liveness, and whether the database is reachable |
| `/auth/link`, `/me`, `/me/orders`, `/me/restaurants` | the caller's own identity, profile and lists |
| `/users`, `/addresses` | accounts and delivery addresses |
| `/restaurants`, `/cuisines`, `/menu-items`, `/menu-categories` | the catalog, its menus and dish images |
| `/orders` | quote, place, status, events, cancel, reject, assign |
| `/payments`, `/refunds` | authorize, the gateway callback that captures, refunds |
| `/coupons` | issue, list, validate |
| `/delivery-partners`, `/deliveries` | dispatch |
| `/admin/metrics/*` | the operator console's figures |

Caller-scoped reads take their subject from the token, not a parameter:
`GET /me/orders` is a customer's history, and `GET /orders?user_id=` is the
staff-facing queue that ignores it.

## Delivery distance and the promised time

There is no delivery-radius rule, and `distance_km` is not a real distance.

Saved addresses carry placeholder coordinates, so the great-circle distance to a
restaurant lands hundreds of km out — which refused almost every order
("Address is 747.1 km away") and would have priced a per-km delivery fee off
that number. Until addresses are geocoded for real:

- the promise is a plausible window, **15–45 minutes**, and
- the fee is priced off a plausible city distance, **1.0–8.0 km**.

Both are in `app/services/eta.py` (`nominal_eta_minutes`, `nominal_distance_km`)
and both are *stable* for a given restaurant-and-address pair: `/orders/quote`
and `POST /orders` run the same pricing, so a wandering number would quote 20
minutes and place 40. `haversine_km` is still there and still used by the
seeder — it is only the pricing path that no longer trusts it.

`apps/customer` mirrors these rules in `lib/services/fixtures/pricing.ts` for
its offline source; the two must stay in step.

## Notes

- `DATABASE_URL` in `.env` must use the `postgresql+asyncpg://` scheme and no
  `?sslmode=` param — don't paste Supabase's dashboard string verbatim.
- `supabase-root-2021-ca.crt` is Supabase's public CA cert, required to verify
  the TLS connection. Safe to commit.
