# Foodishi AI — Frontend Monorepo Plan (high level)

Three Next.js apps sharing one component library and one API client.

---

## Tooling

| Concern | Choice | Version checked | Why |
|---|---|---|---|
| Monorepo | **Turborepo** | 2.10.11 | From Vercel, who make Next.js. Its default template *is* "several Next.js apps + a shared ui package". Task caching out of the box |
| Package manager | **pnpm** | not installed yet | Turborepo's default. Workspace linking without duplicated `node_modules` |
| Components | **shadcn/ui** | 4.18.0 | Has first-class monorepo support — components generate straight into `packages/ui`. You own the source, so no versioning dance |
| Framework | Next.js | 16.3.1 | |
| Charts | Tremor | 3.18.7 | Operator dashboard only |
| API types | `openapi-typescript` | — | Generated from FastAPI's `/openapi.json`, shared by all three apps |

**Rejected:** Nx (23.1.1) — more powerful, but its generators and plugin model
are overhead for three apps. Plain pnpm workspaces — works, but loses task
caching, which is the main reason to bother with a monorepo tool at all.

**Setup snag to clear first:** only npm is installed here. `corepack enable pnpm`
created the shim but it fails to run. Either fix corepack, `npm i -g pnpm`, or
fall back to npm workspaces — Turborepo supports those too, just less neatly.

---

## Structure

```
foodishi/
├── foodishi-api/                  FastAPI + Postgres (exists)
└── foodishi-web/                  Turborepo — self-contained, no Python inside
    ├── apps/
    │   ├── operator/           internal ops dashboard
    │   ├── partner/            restaurant-facing
    │   └── customer/           end-customer ordering
    ├── packages/
    │   ├── ui/                 shadcn components + shared primitives
    │   ├── api-client/         generated types + typed fetch wrapper
    │   ├── eslint-config/
    │   └── typescript-config/
    └── turbo.json
```

Keeping `foodishi-web/` separate from `foodishi-api/` means the Python and JS
toolchains never argue about lockfiles, CI steps, or what the repo root means.

---

## What each app is

| App | Audience | Sees | Notes |
|---|---|---|---|
| **operator** | You / Foodishi ops | Everything, all restaurants | The 5 monitoring views in `DASHBOARD_PLAN.md`. Pairs with sqladmin for raw CRUD |
| **partner** | Restaurant staff | **Only their own restaurant** | Incoming orders, accept/reject, mark ready, menu, payouts |
| **customer** | End customer | Their own orders | Browse, cart, place, track, cancel |

## What is shared, and what deliberately is not

**Shared** — `packages/ui` (buttons, tables, forms, layout, theme) and
`packages/api-client` (one generated client, so a backend change surfaces as a
TypeScript error in all three apps rather than a runtime bug in one).

**Not shared** — layouts and navigation. An ops console, a kitchen screen and a
consumer storefront have genuinely different information density. Forcing one
shell on all three is the usual way monorepo UI packages turn into a mess.

---

## The consequence, now paid rather than weighed

Three apps means the **partner** app exists, and that was never just more
screens. This section used to argue for deferring all of it. Writing it is what
reversed D2 the same day, so two of the three lines are now done:

- [x] **Schema change** — `restaurant_staff` (user, restaurant, role,
      `is_active`) plus `users.auth_user_id`. Applied 2026-08-20; the table
      count went 19 → 20, and to 21 with `menu_item_images` from concurrent
      work. See `DATABASE_DESIGN.md` §5.8
- [x] **Decision D2 (auth) stopped being deferrable.** Supabase Auth is in phase
      1. JWT verification, `CurrentUser`, `require_staff` and the nine identity
      and staff endpoints are built and verified against the live project —
      `IMPLEMENTATION_PLAN.md` D2 and Step 15
- [~] **Authorization on every catalog and order endpoint** — a partner must
      only ever reach their own restaurant's data. That is a server-side scope
      check, not a UI filter; a filter is bypassed by editing an id in the URL.
      **Half done.** All 10 catalog-admin writes, `GET /orders`,
      `PATCH /orders/{id}/status` and `PATCH /deliveries/{id}` are scoped
      through `app/dependencies/scope.py`. The remaining order routes, plus
      users, addresses, payments, refunds and coupons, are still open

Both things that used to gate the partner app are now cleared:

1. `restaurant_staff` is **seeded**, covering every restaurant with
   an owner, and mostly a manager and staff besides. Scoped routes now answer
   for the right people instead of 403ing for everyone
2. The identity endpoints exist: `POST /auth/link` (the path is
   `PROFILE_LINK_PATH`, not the `/auth/profile` earlier drafts named), `GET /me`,
   `PATCH /me`, `GET /me/orders`, and `GET /me/restaurants` — which is precisely
   the restaurant picker this app opens with

The old advice — *build operator only, it needs no schema change and no
authorization model* — is no longer the only fast route. What remains is not a
missing capability but an inconsistent one: some endpoints check the caller and
some do not.

---

## Steps

### M1 — Foundation
- [ ] Resolve the pnpm situation (corepack, global install, or npm workspaces)
- [ ] `create-turbo` scaffold at `foodishi/foodishi-web/`
- [ ] Rename the template apps to `operator`, add `partner` and `customer`
- [ ] Shared `eslint-config` and `typescript-config` packages
- [ ] Confirm `turbo dev` runs all three on different ports

### M2 — Shared packages
- [ ] `shadcn init` targeting `packages/ui`
- [ ] Base components: button, table, card, badge, dialog, form, toast
- [ ] Tailwind preset + design tokens shared across apps
- [ ] `packages/api-client`: `openapi-typescript` generation script, typed fetch
      wrapper, TanStack Query hooks
- [ ] CORS on the FastAPI side for all three dev origins

### M3 — operator
- [ ] App shell, then the 5 views from `DASHBOARD_PLAN.md`

### M4 — partner *(UNBLOCKED 2026-08-20 — D2 reversed, and the API side shipped)*
- [x] `restaurant_staff` table + `require_staff()` dependency in the API
- [x] `restaurant_staff` seeded — every restaurant has an active owner
- [x] `POST /auth/link` and the staff CRUD endpoints (API Step 15)
- [x] Scope checks on the catalog endpoints, `GET /orders`,
      `PATCH /orders/{id}/status` and `PATCH /deliveries/{id}`
- [ ] Scope checks on the rest of the order routes — see API Step 15
- [ ] Supabase client + session handling in the app; send the access token as
      `Authorization: Bearer`
- [ ] Call `GET /me/restaurants` on load for the restaurant picker — it returns
      only memberships that are active, so the picker cannot offer a restaurant
      the API will then refuse
- [ ] Live order queue, accept/reject, menu availability toggles

### M5 — customer *(UNBLOCKED 2026-08-20 — identity exists and is built)*
- [x] `POST /auth/link` exists — a Supabase user with no `public.users` row
      cannot place an order, and this is the only endpoint that creates one
- [ ] Call it on first sign-in. It is idempotent (always 200), so calling it on
      *every* sign-in is also correct and simpler than tracking whether you have
- [ ] Sign-in / sign-up screens against Supabase Auth
- [ ] Use `GET /me` and `GET /me/orders` rather than `/users/{id}` and
      `/orders?user_id=` — the `/me` pair takes its subject from the token and
      will survive the retrofit that closes the id-taking versions
- [ ] Browse, restaurant page, cart with live quote, place, track, cancel

**Unblocked is not finished.** The endpoints exist and the dependencies are
verified, but the per-endpoint authorization retrofit is only half applied.
Building screens against `GET /orders/{id}` or `PATCH /users/{id}` — both still
public — reproduces exactly the hole D2 was reversed to close, and does it in
code that will need rewriting when those routes start refusing. Prefer the `/me`
and `/restaurants/{id}/...` routes, which already check.

---

## Sequencing

`foodishi-web` depends on the API existing. M1 and M2 can happen any time — they
touch no endpoints. M3 needs the metrics endpoints (Dashboard Step B). M4 and M5
need most of the 59 phase-1 endpoints — all of which now exist; `app.openapi()`
reports 69 operations in total — and both want API Step 15's retrofit finished,
not merely started.

The partner-scoping work this document introduced is no longer a warning about
a future change: it is `IMPLEMENTATION_PLAN.md` Step 15, written down
deliberately rather than discovered halfway through, which was the point.
