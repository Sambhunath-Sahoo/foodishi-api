# Foodishi AI — Admin & Monitoring Dashboard Plan

**Question asked:** build our own in Next.js, or adopt something existing?

**Answer: both, split by what each is actually good at.** Hand-building CRUD
screens for 20 tables is a lot of low-value work. Hand-building the five
operational views that matter is unavoidable and worth doing well.

---

## What I checked

Resolved against this project's actual dependency tree — not assumed.

| Option | Version | Verdict |
|---|---|---|
| **sqladmin** | 0.31.0 | **Adopt.** SQLAlchemy admin for FastAPI. Reads the declarative models already written. Resolves cleanly: `+ sqladmin`, `+ wtforms` — 2 packages |
| starlette-admin | 1.0.0 | Also clean (2 packages), more featureful, heavier config. Fallback if sqladmin's forms prove limiting |
| fastapi-admin | 1.0.4 | **Reject.** Tortoise ORM + aioredis. Wrong ORM |
| piccolo-admin | 1.14.0 | **Reject.** Piccolo ORM |
| Supabase Studio | have it | Not a dashboard. A table browser tied to project login — no business views, no "orders running late" |
| Retool / Appsmith | — | **Reject for now.** Another service to host and secure for something in-process gives free |
| **Refine** | 5.0.12 | Considered for the custom side; heavier abstraction than needed |
| **react-admin** | 5.15.1 | Same — a full framework where a few pages will do |
| **Tremor** | 3.18.7 | **Adopt** for charts. Purpose-built for dashboards, plain React components |

Environment confirmed: Node v20.19.3 · npm 11.6.4 · starlette 1.6.0 (satisfies
sqladmin's `>=0.50,<2.0.0`) · SQLAlchemy 2.0.52.

---

## The two layers

### Layer 1 — sqladmin, mounted into the existing API

Browse and edit all 20 tables. Roughly 60 lines total, no new service, no new
deployment, reuses the models verbatim.

Gets you free: list views with search and sort, filters, detail pages, create /
edit / delete forms, foreign-key dropdowns, pagination.

### Layer 2 — Next.js monitoring dashboard

The five views a table browser structurally cannot give you, because each is a
question rather than a table:

1. **Live orders board** — everything not `delivered`/`cancelled`, grouped by
   status, flagged red when `now() > promised_at`
2. **SLA watch** — refunds past `sla_due_at`, ordered by how far past
3. **Revenue & funnel** — orders/revenue over time, placed -> delivered
   conversion, cancellation rate split by inside/outside the window
4. **Coupon usage** — redemptions per coupon, discount given, which are near
   their limit
5. **Restaurant performance** — order volume, average prep vs. promised,
   cancellation rate by restaurant

---

## Stack for Layer 2

| Concern | Choice | Why |
|---|---|---|
| Framework | Next.js 15, App Router, TypeScript | You asked for it, and server components suit read-heavy dashboards |
| Styling | Tailwind + shadcn/ui | Own the components, no framework lock-in |
| Charts | Tremor 3.18 | Built for dashboards; skips a week of chart wiring |
| Data | TanStack Query | Polling, cache, background refresh — the live board needs all three |
| **Types** | `openapi-typescript` from FastAPI's `/openapi.json` | The API already publishes a schema. Generate the client; never hand-write a type that can drift from the backend |

**Location:** `foodishi/foodishi-admin/` — sibling to `foodishi-api/`, not nested.

---

## Steps

### Step A — sqladmin (do this first, it is an afternoon)

- [ ] `uv add sqladmin`
- [ ] `app/admin/__init__.py` — `Admin(app, engine)` mounted at `/admin`
- [ ] `app/admin/views.py` — one `ModelView` per table (19), with
      `column_list`, `column_searchable_list`, `column_sortable_list`
- [ ] Sensible list columns per model — not every column on every table
- [ ] Make `refunds` and `payments` **read-only** in the UI (`can_edit = False`)
      — money rows should move through the API's services, never a form
- [ ] Basic auth on `/admin` (sqladmin's `AuthenticationBackend`)
- [ ] Verify: browse restaurants -> menu items -> orders -> order items

**Acceptance**

- [ ] All 20 tables listed and browsable
- [ ] Seeded 800 orders paginate without timing out
- [ ] `/admin` refuses access without credentials
- [ ] Mounting admin does not change any existing endpoint's behaviour

### Step B — API groundwork for Layer 2

- [ ] CORS middleware for `http://localhost:3000`
- [ ] `GET /admin/metrics/summary` — counts, revenue today, live order count,
      breached SLA count
- [ ] `GET /admin/metrics/orders-over-time?days=30`
- [ ] `GET /admin/metrics/funnel`
- [ ] `GET /admin/metrics/restaurants` — per-restaurant performance
- [ ] `GET /orders?status=live` — the board's data source (reuses Step 5's list)
- [ ] `GET /refunds?sla_breached=true`

> These metrics endpoints are **additions to the 56** in
> `IMPLEMENTATION_PLAN.md`. Aggregations, not CRUD — plain SQL with
> `GROUP BY`, no new tables.

### Step C — Next.js scaffold

- [ ] `npx create-next-app@latest foodishi-admin --typescript --tailwind --app`
- [ ] shadcn/ui init
- [ ] `npm i @tremor/react @tanstack/react-query`
- [ ] `npm i -D openapi-typescript`
- [ ] `npm run gen:api` script -> `openapi-typescript http://localhost:8000/openapi.json -o src/types/api.d.ts`
- [ ] Typed fetch wrapper reading `NEXT_PUBLIC_API_URL`
- [ ] App shell: sidebar, header, dark mode

### Step D — The five views

- [ ] **Live orders board** — columns by status, auto-refresh 15s, red past `promised_at`
- [ ] **SLA watch** — breached refunds, worst first, with age
- [ ] **Revenue & funnel** — Tremor area chart + conversion + cancellation split
- [ ] **Coupon usage** — table with a usage bar per coupon
- [ ] **Restaurant performance** — sortable, prep-time variance highlighted
- [ ] Overview page stitching the top KPI tiles together

### Step E — Polish

- [ ] Loading skeletons, empty states, error states
- [ ] Order detail drawer — items, timeline, payments, refunds in one place
- [ ] Auth on the dashboard, matching whatever **D2** decides
- [ ] Responsive down to tablet

---

## Sequencing against the API plan

| When | Do |
|---|---|
| After **Step 1** (seed data) | **Step A** — sqladmin. You get eyes on all 20 tables immediately, which makes verifying every later step far easier |
| After **Step 11** (delivery) | Steps B–E — the metrics views need orders, payments, refunds and coupons to exist first |

Step A being early is the point: a browsable admin makes the rest of phase 1
easier to build, not just nicer to look at.

---

## The honest trade-off

sqladmin is generated CRUD. It will look like generated CRUD — functional,
plain, occasionally awkward around foreign keys. That is the deal: zero design
effort for 20 tables of admin.

The Next.js dashboard is where design effort goes, on five views that answer
real operational questions. If you would rather have one polished thing than
two adequate ones, drop sqladmin and build Layer 2 only — but expect to hand-write
a lot of table screens you would otherwise get free.
