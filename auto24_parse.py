-- BalticRadar - Supabase / Postgres schema
-- Implements the ad_id / car_id split: an "ad" is a posting, a "car" is the
-- physical vehicle. Reposts reuse car_id so price/mileage history stays as one
-- chain. Safe to run on an existing project: only CREATEs, no DROP.
-- Run in: Supabase dashboard -> SQL Editor -> paste -> Run.

-- ============ CARS: one row per physical vehicle ============
create table if not exists cars (
  car_id        text primary key,                 -- stable internal id
  fingerprint   text,                              -- identity hash (no price/mileage)
  source        text,                              -- autoplius | auto24 | ss.lv
  country       text check (country in ('LV','LT','EE')),
  make          text,
  model         text,
  year          int,
  engine_cc     int,
  engine_l      text,
  power_kw      int,
  fuel          text,
  gearbox       text,
  body          text,
  drivetrain    text,
  color         text,
  owner_code    text,                              -- strong signal (autoplius private)
  vin_prefix    text,                              -- strong signal (auto24/ss.lv)
  location      text,
  photos        jsonb default '[]'::jsonb,
  last_price    int,
  last_mileage  int,
  active        boolean default true,              -- false when its current ad disappears
  first_seen    timestamptz default now(),
  last_seen     timestamptz default now()
);

create index if not exists idx_cars_country     on cars(country);
create index if not exists idx_cars_make_model  on cars(make, model);
create index if not exists idx_cars_year        on cars(year);
create index if not exists idx_cars_price       on cars(last_price);
create index if not exists idx_cars_active_seen on cars(active, first_seen desc);
create index if not exists idx_cars_fingerprint on cars(fingerprint);

-- ============ ADS: one row per posting (the changing id) ============
create table if not exists ads (
  ad_id       text primary key,                    -- e.g. A31042610 / EE4324117 / LV57557782
  car_id      text references cars(car_id) on delete cascade,
  source      text,
  source_url  text,
  active      boolean default true,
  first_seen  timestamptz default now(),
  last_seen   timestamptz default now()
);
create index if not exists idx_ads_car on ads(car_id);

-- ============ PRICE / MILEAGE HISTORY: attached to car_id, never ad_id ============
create table if not exists price_history (
  id        bigserial primary key,
  car_id    text references cars(car_id) on delete cascade,
  ts        timestamptz default now(),
  price     int,
  mileage   int
);
create index if not exists idx_ph_car_ts on price_history(car_id, ts);

-- ============ REVIEW QUEUE: uncertain matches a human confirms ============
create table if not exists review_queue (
  ad_id       text primary key,
  fingerprint text,
  reason      text,
  payload     jsonb,
  created     timestamptz default now()
);

-- ============ VIEW the website reads (newest active cars) ============
create or replace view v_listings as
  select car_id, source, country, make, model, year, engine_l, fuel, gearbox,
         body, drivetrain, location, photos, last_price, last_mileage,
         first_seen, source as src
  from cars
  where active = true
  order by first_seen desc;

-- NOTE: your existing `listings` table is left untouched. Once you trust this
-- pipeline, point the website at v_listings (or rename), so nothing breaks now.
