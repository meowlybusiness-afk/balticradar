-- BalticRadar: backfill engine_l from engine_cc.
--
-- autoplius (LT) only ever gave us cubic centimetres ("Dzinējs: 1998 cm3"), so cars.engine_l was
-- NULL for every Lithuanian listing. The site's "Tilpums no / līdz" filter matches on engine_l, so
-- those cars were silently dropped from ANY search that set an engine volume: SUV + automatic +
-- 2.0 L returned 0 results even though 2 891 such cars exist.
--
-- run_all.py now derives the litres at write time; this fixes the rows already in the table.
-- Run once in: Supabase dashboard -> SQL Editor -> paste -> Run.

UPDATE cars
   SET engine_l = to_char(round(engine_cc / 1000.0, 1), 'FM990.0')
 WHERE engine_l IS NULL
   AND engine_cc BETWEEN 200 AND 12000;

-- how many are left without a volume (expect: only rows with no engine_cc either)
SELECT count(*) FILTER (WHERE engine_l IS NULL) AS still_null,
       count(*)                                 AS total
  FROM cars
 WHERE active;
