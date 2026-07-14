-- Enables the "Mazākais nobraukums" (lowest mileage) sort on the main catalogue.
--
-- WHY: ordering the whole ~94k catalogue by last_mileage currently exceeds the Postgres
-- statement timeout (SQLSTATE 57014). index.html's sbPage() safety net then silently falls
-- back to first_seen.desc, so the sort option looks broken. Price and year already have
-- covering indexes, which is why those sorts return in <200ms.
--
-- Run this in the Supabase SQL editor, then re-enable the <option value="ma"> in index.html.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cars_active_last_mileage
  ON cars (last_mileage ASC NULLS LAST)
  WHERE active;

ANALYZE cars;

-- Verify (should be well under 1s, and NOT a Seq Scan):
-- EXPLAIN ANALYZE
--   SELECT car_id, last_mileage FROM v_listings
--   ORDER BY last_mileage ASC NULLS LAST LIMIT 30;
