-- 001_missing_indexes.sql
--
-- Sixteen missing indexes, two unique constraints and one redundant index,
-- all verified against the live schema on 2026-08-21 by reading pg_indexes --
-- none of the CREATEs exists yet.
--
-- NOTHING HERE LOSES DATA. Every statement is CREATE INDEX IF NOT EXISTS except
-- the last, which drops an index made redundant by a composite that already
-- covers it. Read the note above that DROP before running it.
--
-- HOW TO RUN. CREATE INDEX CONCURRENTLY cannot run inside a transaction block,
-- so run this file WITHOUT wrapping it in BEGIN/COMMIT:
--
--     psql "$DATABASE_URL" -f migrations/001_missing_indexes.sql
--
-- (Use the plain postgresql:// form of the URL, not the postgresql+asyncpg://
-- scheme app/db.py needs.) CONCURRENTLY keeps writes flowing while each index
-- builds, at the cost of a second table pass. If a build is interrupted it
-- leaves an INVALID index behind; find them with
--
--     select indexrelid::regclass from pg_index where not indisvalid;
--
-- and DROP INDEX those before re-running.
--
-- The matching Index(...) declarations are in app/models/*.py so a freshly
-- created database gets them from create_all. This file is only for the database
-- that already has the tables.


-- ---------------------------------------------------------------------------
-- orders.placed_at -- the single highest-value index here
-- ---------------------------------------------------------------------------
-- Every /admin/reports/*, every /admin/metrics/*, admin_insights.workload (read
-- on EVERY operator page load), settlements.compute_period and the partner's own
-- reports filter `placed_at >= :start AND placed_at < :end`. The existing
-- ix_orders_user_placed leads with user_id, so it cannot serve that predicate --
-- all of them were Seq Scan on orders.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_placed_at
    ON public.orders (placed_at);

-- Serves admin_insights' orders_late count and AdminOrderSort.OLDEST_PROMISE.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_promised_at
    ON public.orders (promised_at);


-- ---------------------------------------------------------------------------
-- Unindexed foreign keys. Postgres does not index an FK for you, so both the
-- referential-integrity check and every join over the column pay a scan.
-- ---------------------------------------------------------------------------

-- DELETE /addresses/{id} scanned `orders` TWICE: once for the handler's own
-- exists() guard, once for the RESTRICT trigger.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_address_id
    ON public.orders (address_id);

-- Partial: most orders carry no coupon, so this stays small.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_coupon_id
    ON public.orders (coupon_id) WHERE coupon_id IS NOT NULL;

-- Every menu-item delete scans order_items; reports._items_statement groups by it.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_order_items_menu_item_id
    ON public.order_items (menu_item_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_refunds_payment_id
    ON public.refunds (payment_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deliveries_partner_id
    ON public.deliveries (partner_id);

-- coupons.list_coupons filters restaurant_id IN (...); a restaurant delete scans.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_coupons_restaurant_id
    ON public.coupons (restaurant_id) WHERE restaurant_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_coupons_cuisine_id
    ON public.coupons (cuisine_id) WHERE cuisine_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_reviews_user_id
    ON public.reviews (user_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_conversations_order_id
    ON public.conversations (order_id) WHERE order_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_order_item_modifiers_option_id
    ON public.order_item_modifiers (option_id) WHERE option_id IS NOT NULL;

-- The PK is (restaurant_id, cuisine_id), so cuisine_id alone is unindexed.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_restaurant_cuisines_cuisine_id
    ON public.restaurant_cuisines (cuisine_id);


-- ---------------------------------------------------------------------------
-- Predicate indexes for the queries that scan whole tables today
-- ---------------------------------------------------------------------------

-- GET /restaurants/{id}/reports/performance aggregates every ready_for_pickup
-- and every confirmed event for the WHOLE platform before joining to one
-- restaurant's window. order_status_events grows ~5 rows per order forever and
-- had only (order_id, created_at), which cannot serve a to_status predicate.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_order_status_events_status_order
    ON public.order_status_events (to_status, order_id) INCLUDE (created_at);

-- GET /admin/finance/refunds filters and sorts on (status, sla_due_at) with only
-- order_id indexed, so it scanned the table and sorted it whole to keep 20 rows.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_refunds_outstanding_due
    ON public.refunds (sla_due_at) WHERE status <> 'completed';

-- settlements.compute_period's completed-refund window.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_refunds_completed_at
    ON public.refunds (completed_at) WHERE status = 'completed';

-- admin_insights.workload's payments_failed count.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_payments_failed_recent
    ON public.payments (created_at) WHERE status = 'failed';

-- Keeps the "claim the next free rider" select a one-row index scan once
-- app/routers/delivery.py takes FOR UPDATE SKIP LOCKED over it.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_delivery_partners_available
    ON public.delivery_partners (id) WHERE is_available;


-- ---------------------------------------------------------------------------
-- Correctness constraints the application cannot enforce by itself
-- ---------------------------------------------------------------------------

-- At most ONE live payment per order. app/routers/payments.py checked for an
-- existing authorized-or-captured row and then inserted, which under READ
-- COMMITTED is a check-then-act: a double-tap had both requests see zero rows,
-- both insert, and both settle -- so captured_total returned twice the order
-- total and the cancellation path refunded 1000 on a 500 order. Only a unique
-- index can refuse the second insert.
--
-- Partial, so a retry after a decline still works: "a failed UPI attempt
-- followed by a successful card payment is two rows".
--
-- If this fails, an order already has two live payments. Find them with:
--   select order_id, count(*) from public.payments
--    where status in ('authorized','captured','partially_refunded')
--    group by 1 having count(*) > 1;
-- and reconcile by hand before retrying -- do NOT delete a captured payment.
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_payments_one_live_per_order
    ON public.payments (order_id)
    WHERE status IN ('authorized', 'captured', 'partially_refunded');

-- One redemption per (coupon, user) for the single-use case. app/services/
-- coupons.py enforces usage_limit_per_user with a COUNT and the table had only a
-- NON-unique index on the pair, so two concurrent checkouts both counted zero
-- and both redeemed.
--
-- COMMENTED OUT because it is only correct if usage_limit_per_user is ALWAYS 1.
-- Check first:
--   select distinct usage_limit_per_user from public.coupons;
-- If every row is 1 (or null), uncomment. If any coupon legitimately allows more
-- than one redemption per user, leave it commented -- the atomic UPDATE in
-- app/services/ordering.py already closes the global cap race, and the per-user
-- cap needs a SELECT ... FOR UPDATE on the coupon row instead.
--
-- CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_coupon_redemption_per_user
--     ON public.coupon_redemptions (coupon_id, user_id);

-- Makes users.email case-insensitively unique for real. Two call sites lowercase
-- on write and call it "so the unique index is case-insensitive", but the
-- constraint is a plain btree on the raw column -- so any write path that forgets
-- can create 'A@b.com' beside 'a@b.com', after which POST /auth/link matches one
-- arbitrarily and the other profile becomes unreachable.
--
-- Check for existing collisions first; this will fail if any exist:
--   select lower(email), count(*) from public.users group by 1 having count(*) > 1;
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_users_email_lower
    ON public.users (lower(email));


-- ---------------------------------------------------------------------------
-- Redundant index
-- ---------------------------------------------------------------------------
-- `reviews` carries TWO indexes led by restaurant_id: ix_review_restaurant, a
-- composite on (restaurant_id, created_at), and ix_reviews_restaurant_id, a
-- single column on (restaurant_id) from index=True on the column.
--
-- They are NOT identical -- but restaurant_id is the composite's LEADING column,
-- so the composite serves every query the single-column index could, and the
-- single-column one earns nothing. Two indexes to maintain on every insert and
-- update for one index's worth of reads.
--
-- index=True has been removed from the model too, so create_all does not
-- recreate it on a fresh database.
--
-- Run this LAST and only after confirming both still exist:
--   select indexname, indexdef from pg_indexes
--    where tablename = 'reviews' and indexdef like '%restaurant_id%';
DROP INDEX CONCURRENTLY IF EXISTS public.ix_reviews_restaurant_id;


-- ---------------------------------------------------------------------------
-- Verify
-- ---------------------------------------------------------------------------
-- Expect Bitmap Index Scan on ix_orders_placed_at rather than Seq Scan:
--
--   EXPLAIN ANALYZE
--   SELECT count(*), coalesce(sum(total_amount), 0) FROM public.orders
--    WHERE placed_at >= now() - interval '30 days' AND placed_at < now();
--
-- And confirm every index above is valid:
--
--   SELECT indexrelid::regclass AS invalid_index
--     FROM pg_index WHERE NOT indisvalid;
