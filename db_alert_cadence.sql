-- BalticRadar: alert cadence (anti-spam).
--
-- The alert job runs every 20 minutes and used to e-mail on EVERY new match, so one saved filter
-- could fire up to 72 times a day and a user with three filters over 200. That gets you
-- unsubscribed on day one and teaches Gmail to junk the domain.
--
-- notify.py now honours a per-filter cadence and a hard per-user daily ceiling. This migration adds
-- the columns and the ledger it needs. Safe to re-run.
-- Run once in: Supabase dashboard -> SQL Editor -> paste -> Run.

-- 1. per-filter cadence. 'daily' is the safe default and the only cadence a FREE account gets;
--    'instant' / 'few_hours' are the premium feature.
alter table saved_filters add column if not exists notify_freq text not null default 'daily';
alter table saved_filters add column if not exists last_notified_at timestamptz;

alter table saved_filters drop constraint if exists saved_filters_notify_freq_ck;
alter table saved_filters add  constraint saved_filters_notify_freq_ck
  check (notify_freq in ('instant','few_hours','daily'));

-- every filter that already exists goes onto the safe cadence
update saved_filters set notify_freq = 'daily'
 where notify_freq is null or notify_freq not in ('instant','few_hours','daily');

-- 2. the legacy alert profile had no cadence at all - give it one
alter table subscriptions add column if not exists last_notified_at timestamptz;

-- 3. ledger: one row per e-mail actually sent, so the daily ceiling is enforceable
create table if not exists alert_sends (
  id        bigserial primary key,
  user_id   uuid,                     -- NULL = a legacy `subscriptions` send (still spends budget)
  filter_id bigint,
  sent_at   timestamptz not null default now()
);
create index if not exists alert_sends_user_day on alert_sends (user_id, sent_at desc);
create index if not exists alert_sends_day      on alert_sends (sent_at desc);

-- written by the service key only; nobody else needs to see it
alter table alert_sends enable row level security;

-- 4. sanity
select notify_freq, count(*) from saved_filters group by 1 order by 1;
