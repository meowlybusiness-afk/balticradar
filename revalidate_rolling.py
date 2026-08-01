#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BalticRadar ROLLING REVALIDATOR  (local, home IP, DIRECT - no proxy, no ScraperAPI)
==================================================================================
WHY THIS FILE EXISTS
    Measured on 2026-07-14 by browsing the live site as a real visitor (330 listings,
    300 unique, checked one by one against the source):

        default sort page 1 .... 0 % dead        LT filter .... 3.3 % dead
        "Letakie" (cheapest) ... 16.7 % dead     LV filter .... 0 % dead
        "Dargakie" (dearest) ... 10.0 % dead     EE filter .... 0 % dead
        deep pages 100/500 ..... 10-13 % dead

    Root cause: `last_seen` was written ONCE at discovery and never updated
    (97.4 % of rows had last_seen older than 24 h; median gap last_seen - first_seen
    = 0 minutes). NOTHING in the pipeline ever re-checked a listing.

    The GitHub-Actions revalidate job could not fix it: autoplius + auto24 hard-block
    GitHub's datacenter IP, it fell back to ScraperAPI, that returned blocks, and the
    job failed hourly (13/13 runs failed) - emailing the owner every time.

    But the HOME IP works. Verified on 2026-07-14 from this machine:
        100 autoplius pages at ~3.2 s spacing -> ZERO 429s
        112 auto24 pages     at ~1.0 s        -> all answered
         88 ss.lv pages      at ~1.0 s        -> all answered
    So: run it here, direct, politely, forever.

WHAT IT DOES
    One worker thread per source (the three hosts rate-limit independently), each
    walking its own catalogue LEAST-RECENTLY-CHECKED FIRST, forever:

        EXPIRED -> active = false, last_seen = now, last_checked = now   (SOFT HIDE)
        ALIVE   -> last_seen = now, last_checked = now
        UNKNOWN -> last_checked = now, AND NOTHING ELSE   (a block is NEVER an expiry)

    Two timestamps, deliberately:
        last_seen    = last time the listing was confirmed ALIVE   (business meaning)
        last_checked = last time we LOOKED at it, whatever happened (queue cursor only)
    Ordering by last_checked is what stops the worker looping on rows that can never
    classify. See the long note above fetch_batch() for the ss.lv incident that caused it.

    There is no DELETE statement anywhere in this file.

PACE (measured-safe, and what it buys)
        autoplius  43.9k rows @ 3.2 s -> full cycle ~1.6 days   <- the rot lives here (13 % dead)
        auto24     18.8k rows @ 1.0 s -> full cycle ~5 h
        ss.lv      32.3k rows @ 1.0 s -> full cycle ~9 h        (measured 0/88 dead; cheap anyway)
    Every listing re-checked at least every ~2 days.

FAILING LOUDLY
    * Every batch prints a one-line heartbeat and appends to revalidate_rolling.log.
    * 8 consecutive TRANSPORT failures on a source -> that source is declared BLOCKED,
      it stops writing, screams in the log and in revalidate_rolling_state.json, waits
      15 min and retries. It never silently marks anything dead.
    * A crash in one source thread does not kill the others, and it is printed in full.

RUN IT
    double-click  revalidate_rolling.bat
"""

import os, sys, json, time, random, threading, traceback, re
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("installing 'requests' ..."); os.system(f'"{sys.executable}" -m pip install requests'); import requests

HERE = os.path.dirname(os.path.abspath(__file__))
# PER-SOURCE PROCESS ISOLATION (2026-07-14): each source now runs as its OWN process so a block
# on one never stalls the others. Separate processes MUST NOT share the state file or the log
# (concurrent writes corrupt them), so WORKER_SUFFIX gives each its own pair, e.g.
#   set WORKER_SUFFIX=_autoplius  ->  revalidate_rolling_autoplius.log / _state_autoplius.json
# No suffix = the old single-process behaviour, unchanged.
_SUFX = os.environ.get("WORKER_SUFFIX", "").strip()
LOGF = os.path.join(HERE, "revalidate_rolling%s.log" % _SUFX)
STATEF = os.path.join(HERE, "revalidate_rolling_state%s.json" % _SUFX)

# ---------------------------------------------------------------- credentials
# Same mechanism the collector already uses: balticradar_key.txt next to this file.
# The key is NEVER typed by anything but the owner, and never leaves this machine.
os.environ.setdefault("SUPABASE_URL", "https://wrilvoukvyubgpomuoyn.supabase.co")
_KEYFILE = os.path.join(HERE, "balticradar_key.txt")
if not os.environ.get("SUPABASE_KEY"):
    if os.path.exists(_KEYFILE):
        os.environ["SUPABASE_KEY"] = open(_KEYFILE, encoding="utf-8").read().strip()
    elif os.environ.get("SELFTEST") == "1":
        # SELFTEST runs classify() fixtures only (no network / no DB), so it must never
        # block on the interactive key prompt.
        os.environ["SUPABASE_KEY"] = "selftest-dummy-key-not-used"
    else:
        print("=" * 66)
        print(" Supabase service_role key needed (same one the collector uses).")
        print(" Supabase -> Project Settings -> API -> service_role (secret)")
        print("=" * 66)
        k = input("Paste the key and press Enter:\n> ").strip()
        os.environ["SUPABASE_KEY"] = k
        try:
            open(_KEYFILE, "w", encoding="utf-8").write(k)
            print("Saved to balticradar_key.txt - you won't be asked again.\n")
        except Exception:
            pass

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SB = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
      "Content-Type": "application/json"}

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"    # LIVE by default; the .bat can flip it
BATCH   = int(os.environ.get("BATCH", "200"))
SMOKE   = int(os.environ.get("SMOKE", "0"))        # >0: check only N per source, then exit (a test run)

# ---------------------------------------------------------------- MILEAGE BACKFILL (opt-in, autoplius)
# One-shot recovery for the autoplius rows whose mileage (and sometimes price) was LOST in the
# 2026-06-29..07-03 broken-parser backfill (~28.8k rows had NULL mileage). OFF by default, so the
# normal auto24 / ss.lv workers are completely unaffected. When BACKFILL_MILEAGE=1 (autoplius only):
#   * the batch cursor targets active NULL-mileage rows, most-recently-seen first (most likely still
#     live), instead of the normal least-recently-checked sweep;
#   * on an ALIVE page we re-parse with the now-fixed autoplius_parse.parse_listing() and FILL mileage
#     (and price) ONLY where the DB value is currently NULL - it NEVER overwrites a real value, and
#     skips the row (leaving NULL) if the parse yields nothing or an out-of-range value. No guessing.
# autoplius_parse reads mileage from the server-rendered <meta name="keywords"> tag, so it works even
# with DOC_ONLY=1. DRY_RUN=1 writes nothing (use it for the first validation pass).
BACKFILL_MILEAGE = os.environ.get("BACKFILL_MILEAGE", "0") == "1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------- detectors
# All three re-verified by hand on 2026-07-14 against listings known dead AND known live.
SOURCES = {
    # priority order matters: autoplius carries 76 % of the rot (13 % of its rows are dead)
    "autoplius": {
        "delay": 3.2,                      # the only host that has ever rate-limited us
        "dead_markers": [
            "sludinājums neeksistē",   # removed        (VERIFIED)
            "sludinajums neeksiste",
            "sludinājums nav aktīvs",  # inactive       (VERIFIED - page still renders full specs!)
            "sludinajums nav aktivs",
            "skelbimas neaktyvus",               # lt locale
            "skelbimas neegzistuoja",
        ],
        "alive_markers": [],
        "spec_table": None,
    },
    "auto24": {
        "delay": 1.0,
        # the literal string on a dead auto24 page; it also answers HTTP 410.
        # (the old markers "kuulutus on aegunud" / "advertisement has expired" appear NOWHERE - dropped)
        "dead_markers": ["ad is not active!", "ad is not active"],
        "alive_markers": [],
        "spec_table": None,
    },
    "ss.lv": {
        "delay": 1.0,
        # REMOVED-AD SIGNAL, re-verified LIVE 2026-07-22 (home IP, Chrome) against three
        # rows and live/buy controls. The OLD model below was WRONG and was the whole bug:
        #     old assumption: removed ad = spec table present but price row stripped
        # ss.lv does NOT strip the price and does NOT 404 a removed ad. Instead it RECYCLES
        # the ad's short code: the old URL 200-REDIRECTS to a DIFFERENT listing. Observed:
        #     /cars/mercedes/a35-amg/cbneod.html -> /spare-parts/honda/accord/cbneod.html
        #     /cars/chrysler/jeep-grand-cherokee/mchin.html -> /spare-parts/honda/accord/mchin.html
        #     /cars/fiat/uno/cipix.html          -> /spare-parts/honda/accord/cipix.html
        # The recycled target keeps a FULL price + spec table of its own, so a removed ad is
        # indistinguishable from a live one by CONTENT - the URL PATH CHANGE is the only
        # reliable signal. A genuinely live ad always resolves to its own stored URL (no
        # redirect). The old "spec table present -> EXPIRED" rule marked those recycled
        # PARTS pages dead by accident (they have 'izlaiduma gads' but no 'cena:'), while a
        # code recycled to another CAR (which has 'cena:') was marked ALIVE -> the 0.075%
        # miss rate. So: expire on a cross-listing redirect; a rendered listing is ALIVE.
        #
        # An unknown/blank code is answered 200 at the SAME url with a generic "Pērkam /
        # покупаем" buy-ad placeholder (no spec table) -> stays UNKNOWN, never expired.
        "expire_on_redirect": True,
        # secondary safety net: explicit removed / not-found notices, LV + RU spellings
        # (with and without diacritics). ss.lv rarely shows these, but if it ever does we
        # must not miss them.
        "dead_markers": [
            "sludinājums nav atrasts", "sludinajums nav atrasts",
            "sludinājums ir dzēsts", "sludinajums ir dzests",
            "sludinājums ir neaktīvs", "sludinajums ir neaktivs",
            "sludinājums ir deaktivizēts", "sludinajums ir deaktivizets",
            "sludinājums nav aktīvs", "sludinajums nav aktivs",
            "beidzies derīguma termiņš", "beidzies deriguma termins",
            "объявление снято", "объявление не найдено",
            "объявление удалено", "объявление деактивировано",
            "объявление не существует",
        ],
        "alive_markers": ["cena:"],
        "spec_table": "izlaiduma gads",
    },
}
# Source priority is rot-weighted (autoplius carries 76 % of the dead listings), BUT:
#
# MEASURED 2026-07-14, after ~5 h of sustained revalidation from the home IP:
#   autoplius started answering 429 + JS-captcha to THIS IP on live pages (dead ones still
#   404 instantly). Stealth flags, a warmed cookie jar and challenge re-fetches did NOT
#   move it - this is an IP rate-limit, not a fingerprint problem. Throughput fell to ~0.
#   The collector shares this IP, so continuing to push autoplius risks the collector.
#
# So autoplius revalidation is PAUSED by default. auto24 (~1 190 checks/h) and ss.lv
# (~1 355 checks/h) are unaffected and keep running - together they are 51k of the 95k rows.
# Re-enable autoplius when there is a working residential proxy (DataImpulse is out of
# traffic) or with a much slower pace, e.g.:
#     set REVAL_SOURCES=autoplius,auto24,ss.lv   and   set AP_DELAY=20
ORDER = [s.strip() for s in os.environ.get("REVAL_SOURCES", "auto24,ss.lv").split(",") if s.strip()]
SOURCES["autoplius"]["delay"] = float(os.environ.get("AP_DELAY", "20"))   # if it IS re-enabled, go slow

BLOCK_MARKERS = ["captcha", "are you a robot", "access denied", "too many requests",
                 "just a moment", "enable javascript and cookies", "rate limit"]
MAX_CONSEC_BLOCKS = 8
BLOCK_COOLDOWN = 15 * 60      # a blocked source waits this long, then tries again. It never gives up silently.
VIEWLIMIT_SLEEP = 20 * 60     # autoplius per-IP view limit: wait it out (it does not self-clear)

_print_lock = threading.Lock()
STATE = {"started": None, "sources": {}}


def log(src, *a):
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} [{src:<9}] " + " ".join(str(x) for x in a)
    with _print_lock:
        print(line, flush=True)
        try:
            with open(LOGF, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def save_state():
    with _print_lock:
        try:
            json.dump(STATE, open(STATEF, "w", encoding="utf-8"), indent=2, default=str)
        except Exception:
            pass


# ---------------------------------------------------------------- supabase
# THE POISON-BATCH BUG (found & fixed 2026-07-21)
# ------------------------------------------------
# The batch cursor used to be `order=last_seen.asc`, and only ALIVE/EXPIRED wrote
# last_seen. UNKNOWN wrote nothing. So any row that classifies UNKNOWN stayed at the
# very head of the queue FOREVER and was re-fetched on every single batch.
#
# MEASURED 2026-07-21 by fetching the actual stuck ss.lv URLs at the home IP:
#   https://www.ss.lv/msg/lv/transport/cars/bmw/x3/cegglh.html   -> 200, 17 KB,
#       title "BMW X3 Pērku покупaем visu marku Auto"  -> no price, no spec table
#   .../honda/cr-v/cfcxig.html  200  "Honda Cr-v Pērkam. покупаем."   -> same
#   .../mercedes/sprinter/dlpfb.html 200 "Куплю Sprinter"             -> same
#   .../toyota/corolla/hfejc.html    200 "Покупаю, все марки тойот"   -> same
#   .../hyundai/i30/mneec.html  200  26 KB "Cena 6 850 €"  -> price + spec table, ALIVE
#   .../volkswagen/t5/bjippf.html 200 36 KB "Cena 2 350 €" -> price + spec table, ALIVE
# i.e. the stuck rows are "Pērku / Покупаю" WANTED-TO-BUY ads. They are live, healthy
# pages that legitimately have no price row and no spec table, so they classify UNKNOWN
# and can NEVER classify as anything else. They accreted at the head of the queue until
# ~96 % of every batch was the same buy-ads, real sale ads were never reached, and the
# expired count stayed at exactly 0 for 7 days.
#
# (Note also: those pages were fetched with a plain no-JS HTTP GET and still contained
#  the full spec table. The old comment claiming ss.lv "serves a JS/cookie shell" is
#  WRONG - DOC_ONLY was never the problem and DOC_ONLY=0 correctly changed nothing.)
#
# THE FIX: a second timestamp, `last_checked`, that advances on EVERY outcome including
# UNKNOWN, and is what the batch is ordered by. `last_seen` keeps its exact old meaning
# ("last confirmed ALIVE") so nothing downstream changes, but the cursor always moves.
#
# Requires this one-time migration (also appended to balticradar_db_fix.sql):
#   alter table public.cars add column if not exists last_checked timestamptz;
#   create index if not exists cars_active_lastchecked_idx
#       on public.cars (active, last_checked asc nulls first);
# If the column is missing the worker still runs, but it logs a loud warning every batch
# and falls back to the old (looping) order - it never silently pretends to be fine.
SEL = "car_id,source,source_url,last_price,last_mileage,last_seen,body"

# --- auto24 body self-heal ---------------------------------------------------
# auto24's meta "Description" leads with the vehicle's TRUE category (its "Type" field):
#   "SUV BMW X5 3.0 190kW touring (5-doors) ..."     -> Type=SUV,  Bodytype=touring
#   "Passenger car VW Passat 1.6 77kW touring ..."   -> Type=passenger car
# The old collector read the SECONDARY "Bodytype" word and stored SUVs as "touring"
# (wagon), which corrupted the price valuation (SUVs compared against wagons). Since this
# rolling revalidator already fetches every auto24 page, it re-derives body='suv' from the
# leading Type and heals the row in place. CONSERVATIVE: it only ever PROMOTES a row to
# 'suv' when auto24 itself says SUV; it never downgrades or rewrites any other body value,
# so genuine estates/sedans/hatchbacks are left exactly as they are.
_A24_DESC_META = re.compile(
    r'<meta[^>]+name=["\']Description["\'][^>]+content=["\']([^"\']*)', re.I)
def a24_body_from_html(html):
    m = _A24_DESC_META.search(html or "")
    if not m:
        return None
    d = m.group(1).replace("\xa0", " ")
    if re.match(r"\s*SUV\b", d, re.I):
        return "suv"
    return None

HAVE_LAST_CHECKED = None      # None = not probed yet; True/False after probe


def probe_last_checked(src):
    """One-time check: does cars.last_checked exist? Never guesses, asks the DB."""
    global HAVE_LAST_CHECKED
    if HAVE_LAST_CHECKED is not None:
        return HAVE_LAST_CHECKED
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/cars?select=car_id,last_checked&limit=1",
                         headers=SB, timeout=45)
        HAVE_LAST_CHECKED = (r.status_code == 200)
        if not HAVE_LAST_CHECKED:
            log(src, "!" * 70)
            log(src, "!! cars.last_checked DOES NOT EXIST -> the batch cursor CANNOT advance")
            log(src, "!! past rows that classify UNKNOWN, and this worker WILL loop forever.")
            log(src, "!! Run this once in the Supabase SQL editor, then restart:")
            log(src, "!!   alter table public.cars add column if not exists last_checked timestamptz;")
            log(src, "!!   create index if not exists cars_active_lastchecked_idx")
            log(src, "!!     on public.cars (active, last_checked asc nulls first);")
            log(src, "!" * 70)
    except Exception as e:
        HAVE_LAST_CHECKED = False
        log(src, f"!! last_checked probe failed ({e!r}) - falling back to last_seen order")
    return HAVE_LAST_CHECKED


def fetch_batch(src, limit):
    """Least-recently-CHECKED first, for ONE source.

    Ordered by last_checked (not last_seen) so that UNKNOWN rows - which deliberately
    never update last_seen - still leave the head of the queue. Served by
    cars_active_lastchecked_idx(active, last_checked asc nulls first).
    Rows never checked (last_checked is null) sort first, so the very first pass after
    the migration walks the whole catalogue exactly once.
    """
    if probe_last_checked(src):
        sel = SEL + ",last_checked"
        order = "last_checked.asc.nullsfirst"
    else:
        sel, order = SEL, "last_seen.asc"      # degraded: this is the looping order
    if BACKFILL_MILEAGE and src == "autoplius":
        # backfill mode: restrict to the NULL-mileage cohort, but keep the SAME anti-loop cursor
        # (last_checked.asc.nullsfirst). last_checked advances on EVERY outcome - including a
        # CAPTCHA/blocked (UNKNOWN) row via touch_checked - so a blocked row never jams the head of the
        # queue; rows that get filled (mileage no longer null) or expired (active false) drop out of the
        # cohort on their own. Ordering by last_seen instead would loop forever on blocked rows.
        url = (f"{SUPABASE_URL}/rest/v1/cars?select={sel}&active=is.true&source=eq.autoplius"
               f"&last_mileage=is.null&order={order}&limit={limit}")
    else:
        url = (f"{SUPABASE_URL}/rest/v1/cars?select={sel}&active=is.true"
               f"&source=eq.{src}&order={order}&limit={limit}")
    r = requests.get(url, headers=SB, timeout=90)
    if r.status_code == 200:
        return r.json()
    log(src, f"!! batch query failed {r.status_code}: {r.text[:120]}")
    return []


def patch_car(src, car_id, payload):
    if DRY_RUN:
        return True
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/cars?car_id=eq.{car_id}",
                       headers={**SB, "Prefer": "return=minimal"},
                       data=json.dumps(payload), timeout=45)
    if r.status_code >= 300:
        log(src, f"!! PATCH FAILED {car_id}: {r.status_code} {r.text[:140]}")
        return False
    return True


def touch_checked(src, car_id):
    """Advance ONLY the cursor. Used for UNKNOWN, which must never imply 'alive' or 'dead'.

    This is the whole anti-loop mechanism: an UNKNOWN row still moves to the back of the
    queue, so the next batch is a DIFFERENT set of rows. last_seen is left untouched, so
    it keeps meaning 'last confirmed alive' and nothing downstream is misled.
    """
    if not HAVE_LAST_CHECKED:
        return False
    return patch_car(src, car_id, {"last_checked": now_iso()})


def add_price_history(car_id, price, mileage):
    if DRY_RUN:
        return
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/price_history",
                      headers={**SB, "Prefer": "return=minimal"},
                      data=json.dumps({"car_id": car_id, "price": price,
                                       "mileage": mileage, "ts": now_iso()}), timeout=45)
    except Exception as e:
        log("price", f"price_history write failed (non-fatal): {e!r}")


# ---------------------------------------------------------------- fetching
def looks_blocked(html):
    """A block page is SHORT. A real listing page is not.

    BUG FOUND 2026-07-14: a normal autoplius listing page contains the word "captcha"
    (the reCAPTCHA snippet in the contact form) and a "please enable javascript" noscript
    line. The old rule matched those markers on a perfectly good 180 KB page, decided it was
    blocked, and RE-REQUESTED it up to 3 more times -> 4x the pages, 4x the proxy bytes,
    4x the time, on EVERY listing. Measured: 167-235 KB/page instead of 47 KB.
    A block/challenge page from any of these three sites is < 60 KB and has no spec table,
    so require that before believing a marker.
    """
    low = (html or "").lower()
    if len(low) < 400:
        return True
    if len(low) > 60000:          # a real, fully rendered listing page - never a block page
        return False
    has_specs = ("izlaiduma gads" in low or "degviela" in low or "nobraukums" in low
                 or "kuulutus" in low or "mileage" in low)
    if has_specs:
        return False
    return any(m in low for m in BLOCK_MARKERS)


# curl_cffi with a real Chrome-124 TLS fingerprint: proven HTTP 200 from a datacenter IP for
# autoplius/auto24/ss.lv (VPS trial 2026-07-30) where plain requests get 403. ~10x faster than
# the browser (no render), so the full revalidation cycle shrinks from ~10-12 days to ~1-2.
try:
    from curl_cffi import requests as _cc_requests
    _CC = _cc_requests.Session(impersonate="chrome124")
except Exception:
    _CC = None
_CC_HDRS = {"Accept-Language": "lv-LV,lv;q=0.9,en;q=0.8,lt;q=0.7,et;q=0.6"}


def get_html_requests(url):
    """Plain HTTP GET. Works for nothing much any more - kept only as a fallback."""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "lv,en;q=0.8"},
                         timeout=30, allow_redirects=True)
        return r.status_code, (r.text or "")
    except Exception as e:
        return 0, f"__transport_error__ {e!r}"


# ---- browser transport -------------------------------------------------------
# MEASURED 2026-07-14: plain `requests` from the home IP gets HTTP 403 from BOTH
# autoplius and auto24 (bot fingerprinting - it is not an IP block: the same home IP
# fetches those pages perfectly in a real browser). ss.lv answers 200 but serves a
# JS/cookie shell with no spec table. So ALL THREE sources are fetched with a real
# browser (Playwright), exactly like the collector does - and, like the collector,
# DIRECT on the home IP (NO_PROXY_BR): the DataImpulse proxy is out of traffic.
# Images/CSS/fonts are aborted, so each check is still only a few KB.
_BLOCK_TYPES = {"image", "media", "font", "stylesheet"}
_BLOCK_HOSTS = ("google-analytics", "googletagmanager", "doubleclick", "googlesyndication",
                "adservice.google", "facebook.net", "facebook.com", "fbcdn", "hotjar", "criteo",
                "adnxs", "adsystem", "taboola", "outbrain", "scorecardresearch", "quantserve",
                "gemius", "mc.yandex", "metrika", "cookiebot", "onesignal", "cookielaw",
                "clarity.ms", "pubmatic", "rubiconproject", "casalemedia", "sentry", "amplitude")

# ---------------------------------------------------------------- proxy + DOC_ONLY
# PROXY_FOR=autoplius  -> only autoplius goes through DataImpulse; auto24 + ss.lv stay on the
#                         free home IP (they have never blocked us).
# PROXY_FOR=none       -> everything direct (default; use when the proxy has no credit).
#
# DOC_ONLY is FORCED on any proxied source and CANNOT be disabled there.
# MEASURED 2026-07-14 on 10 autoplius listing pages (CDP encodedDataLength = real wire bytes):
#     no blocking ......................... 1424 KB/page
#     image/media/font/css blocked (old) .. 1343 KB/page   <-- the old "90% saving" saved 6%
#     everything except the document ......   47 KB/page   <-- 28x cheaper, same data
# The old blocking never blocked SCRIPT, and ctx.route() disables Chromium's HTTP cache, so
# autoplius' ~1.3 MB JS bundle was re-downloaded for every single listing. That is what ate
# the first EUR 5 of DataImpulse credit. The HTML document alone carries the price, the spec
# table, every photo URL and the dead/alive markers - nothing else is needed.
PROXY_FOR = os.environ.get("PROXY_FOR", "none").strip().lower()


def _proxy_for(src):
    if PROXY_FOR in ("none", ""):
        return None
    if PROXY_FOR != "all" and src not in [s.strip() for s in PROXY_FOR.split(",")]:
        return None
    d = {}
    try:
        with open(os.path.join(HERE, "proxy.txt"), encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    d[k.strip().lower()] = v.strip()
    except Exception:
        return None
    if not d.get("host") or not d.get("port"):
        return None
    user = d.get("user", "")
    if user and "sessid" not in user.lower():
        cc = os.environ.get("PROXY_CC", "lt").strip()
        sess = "rv%d" % random.randint(100000, 999999)
        user = f"{user}__cr.{cc};sessid.{sess}" if cc else f"{user}__sessid.{sess}"
    return {"server": f"http://{d['host']}:{d['port']}", "username": user, "password": d.get("pass", "")}

# A JS interstitial ("Mazliet uzgaidiet" / Cloudflare) clears itself if you wait and re-request.
CHALLENGE_MARKERS = ["just a moment", "challenge-platform", "cf-browser-verification",
                     "checking your browser", "uzgaidiet", "prašau palaukite", "palun oodake"]
# autoplius' per-IP VIEW LIMIT is different: it never self-clears, and hammering it makes it worse.
# The collector dodges it by rotating proxy IPs. On the home IP there is only one IP, so the only
# correct response is to stop and wait.
VIEWLIMIT_MARKERS = ["peržiūros limit", "perziuros limit", "viršijote", "virsijote",
                     "skatījumu limits", "limit exceeded"]


def is_challenge(html):
    low = (html or "").lower()
    if len(low) > 60000:      # see looks_blocked(): a full listing page is never a challenge
        return False
    return any(m in low for m in CHALLENGE_MARKERS)


def is_viewlimit(html):
    low = (html or "").lower()
    return any(m in low for m in VIEWLIMIT_MARKERS)


class Browser:
    """One Playwright browser per worker thread (the sync API is not thread-shareable).

    Copies the collector's proven anti-bot setup, which my first version was missing and
    which is why ~30 % of autoplius hits came back as a captcha interstitial:
      * navigator.webdriver hidden via an init script   <- the big one
      * Chrome/120 UA + lv-LV locale + 1366x900 viewport (same as the collector)
      * trackers/ads aborted as well as images/CSS/fonts
      * challenge pages are WAITED OUT and RE-REQUESTED (6s, 12s, 18s), not counted as blocks
      * autoplius' per-IP view-limit page is detected separately and triggers a long sleep,
        because it never self-clears and hammering it just extends the ban
    """

    def __init__(self, src):
        from playwright.sync_api import sync_playwright
        self.src = src
        self._pw = sync_playwright().start()
        launch = {"headless": True,
                  "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"]}
        prox = _proxy_for(src)
        self.proxied = bool(prox)
        if prox:
            launch["proxy"] = prox
            log(src, "PROXY: %s (DataImpulse, billed per GB)" % prox["server"])
        self.browser = self._pw.chromium.launch(**launch)
        self.ctx = self.browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="lv-LV", viewport={"width": 1366, "height": 900})

        # DOC_ONLY is mandatory on the proxy path (money), optional and default-on elsewhere.
        doc_only = self.proxied or os.environ.get("DOC_ONLY", "1") == "1"
        self.doc_only = doc_only

        def _route(route):
            try:
                req = route.request
                if doc_only:
                    if req.resource_type != "document":
                        return route.abort()
                    return route.continue_()
                if req.resource_type in _BLOCK_TYPES or any(h in req.url.lower() for h in _BLOCK_HOSTS):
                    return route.abort()
                return route.continue_()
            except Exception:
                try:
                    return route.continue_()
                except Exception:
                    return

        self.ctx.route("**/*", _route)
        log(src, "DOC_ONLY=%s (%s)" % (doc_only,
            "~47 KB/page - HTML document only" if doc_only else "~1.4 MB/page - full page"))
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self.page = self.ctx.new_page()
        self.warm(src)

    def warm(self, src):
        """Load the site's home page once so the session picks up its cookies before we start
        pulling listing pages. A warmed cookie jar is what a real visitor has."""
        home = {"autoplius": "https://lv.autoplius.lt/",
                "auto24": "https://eng.auto24.ee/",
                "ss.lv": "https://www.ss.lv/lv/transport/cars/"}.get(src)
        if not home:
            return
        try:
            self.page.goto(home, timeout=45000, wait_until="domcontentloaded")
            self.page.wait_for_timeout(2500)
            log(src, "browser warmed (home page + cookies loaded)")
        except Exception as e:
            log(src, f"warm-up failed (non-fatal): {e!r}")

    def get(self, url):
        """-> (status, html, viewlimit, final_url)

        final_url is self.page.url AFTER any redirect. ss.lv recycles a removed ad's short
        code and 200-redirects the old URL to a different listing, so the final URL is how
        classify() detects a removed ss.lv ad - see _same_listing()/classify()."""
        # Fast path: curl_cffi(chrome124). Returns (status, html, viewlimit, final_url) just like
        # the browser. 404/410 = removed ad -> return immediately. On a block/challenge, fall
        # through to Playwright so a bot-wall is NEVER mistaken for an expired ad.
        if _CC is not None:
            try:
                _r = _CC.get(url, headers=_CC_HDRS, timeout=30, allow_redirects=True)
                _h = _r.text or ""
                if _r.status_code in (404, 410):
                    return _r.status_code, _h, False, str(_r.url)
                if _r.status_code == 200 and not is_challenge(_h) and not looks_blocked(_h):
                    return _r.status_code, _h, is_viewlimit(_h), str(_r.url)
            except Exception:
                pass
        try:
            resp = self.page.goto(url, timeout=45000, wait_until="domcontentloaded")
            status = resp.status if resp else 0
            self.page.wait_for_timeout(900)
            html = self.page.content() or ""

            if is_viewlimit(html):
                return status, html, True, (self.page.url or url)

            # wait out a JS challenge and RE-REQUEST (it will not self-clear from a stale DOM)
            for attempt in range(3):
                if not (is_challenge(html) or (status == 200 and looks_blocked(html))):
                    break
                try:
                    self.page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                self.page.wait_for_timeout(6000 * (attempt + 1))     # 6s, 12s, 18s
                try:
                    resp = self.page.goto(url, timeout=45000, wait_until="domcontentloaded")
                    status = resp.status if resp else status
                except Exception:
                    pass
                html = self.page.content() or ""
                if is_viewlimit(html):
                    return status, html, True, (self.page.url or url)

            return status, html, False, (self.page.url or url)
        except Exception as e:
            return 0, f"__transport_error__ {e!r}", False, url

    def close(self):
        for f in (lambda: self.ctx.close(), lambda: self.browser.close(), lambda: self._pw.stop()):
            try:
                f()
            except Exception:
                pass


def _same_listing(req_url, final_url):
    """True if final_url is the SAME ss.lv listing as req_url (i.e. NO cross-listing
    redirect). ss.lv recycles the short code of a REMOVED ad and 200-redirects its old
    URL to a different listing; a live ad always resolves to its own path. We compare the
    normalised path, ignoring scheme, a leading 'www.', query, fragment, trailing slash
    and case. Missing data is treated as 'same' so we NEVER expire on unknown input."""
    def norm(u):
        u = (u or "").split("?", 1)[0].split("#", 1)[0].strip().lower()
        u = re.sub(r"^https?://", "", u)
        if u.startswith("www."):
            u = u[4:]
        return u.rstrip("/")
    a, b = norm(req_url), norm(final_url)
    if not a or not b:
        return True
    return a == b


def classify(status, html, cfg, req_url=None, final_url=None):
    """-> ('expired'|'alive'|'unknown', reason).  UNKNOWN is never an expiry.

    A block page, transport error or rate-limit is ALWAYS unknown, never an expiry
    (the project's cardinal safety rule). For ss.lv the removed-ad signal is a
    cross-listing REDIRECT (recycled short code), NOT the presence/absence of a price -
    see the long ss.lv note in SOURCES. req_url/final_url are the requested and the
    final (post-redirect) URLs; when a source sets expire_on_redirect they decide expiry.
    """
    if status in (404, 410):
        return "expired", f"http {status}"
    if status == 429:
        return "unknown", "http 429 (rate limited)"
    if status != 200:
        return "unknown", f"http {status}"
    if looks_blocked(html):
        return "unknown", "blocked/captcha"
    low = html.lower()
    # 1) explicit removed / not-found notice (LV + RU) - a hard, unambiguous expiry
    for m in cfg["dead_markers"]:
        if m in low:
            return "expired", f'marker "{m}"'
    # 2) ss.lv: the ad's short code was recycled -> its old URL redirected to a DIFFERENT
    #    listing. That only happens once the original ad is gone, so it is an expiry. This
    #    is checked BEFORE the price/spec signals because a recycled target has its own
    #    price + spec table and would otherwise be mistaken for a live ad.
    if cfg.get("expire_on_redirect") and not _same_listing(req_url, final_url):
        return "expired", f"recycled code: redirected to a different listing ({final_url})"
    # 3) positive alive signal: a real price row
    for m in cfg["alive_markers"]:
        if m in low:
            return "alive", "alive marker"
    # 4) a rendered spec table means a real, live listing is ALIVE. (The OLD code wrongly
    #    returned 'expired' here - that was the classifier bug.) No spec table means a
    #    "Pērku / Покупаю" buy-ad (or other non-sale page): UNKNOWN, never expired.
    tbl = cfg.get("spec_table")
    if tbl:
        if tbl in low:
            return "alive", "spec table present (rendered listing)"
        return "unknown", "no spec table (buy-ad / not a sale listing)"
    return "alive", "no dead marker"


def _selftest_classify():
    """Inline, no-network fixtures proving the ss.lv expiry decision. Run: SELFTEST=1.
    Proves: removed (redirect) -> EXPIRED, explicit notice -> EXPIRED, live ad -> ALIVE,
    buy-ad -> UNKNOWN, block/timeout/rate-limit -> UNKNOWN, and (the whole point of the
    fix) that a code recycled to another *car* with a price is still EXPIRED, while a
    live sale ad with the same price+spec content is ALIVE."""
    ss = SOURCES["ss.lv"]
    pad = " filler wordi " * 60          # push every fixture > 400 chars so looks_blocked
    live_url = "https://www.ss.lv/msg/lv/transport/cars/audi/a3/ddffm.html"   # (len) short-circuit
    live_html = "vieglie auto / audi / a3 / pārdod izlaiduma gads: 2000 cena: 1 590 &#8364;" + pad
    buyad_html = "vieglie auto / audi / a4 / pērk pērkam. покупаем. visu marku auto augstākās cenas" + pad
    parts_html = "rezerves daļas / honda / accord izlaiduma gads: 1995 aizdedzes sadalītājs 80 €" + pad
    car_html = "vieglie auto / audi / q7 izlaiduma gads: 2010 cena: 5 000 &#8364;" + pad
    notice_html = "sludinājumi объявление снято с публикации" + pad
    cases = [
        # name, (status, html, req_url, final_url), expected
        ("live sale ad (price+spec, no redirect)",
         (200, live_html, live_url, live_url), "alive"),
        ("removed -> recycled code redirects to a PARTS listing",
         (200, parts_html, "https://www.ss.lv/msg/lv/transport/cars/mercedes/a35-amg/cbneod.html",
          "https://www.ss.lv/msg/lv/transport/spare-parts/honda/accord/cbneod.html"), "expired"),
        ("removed -> recycled to another CAR that HAS a price (still a redirect)",
         (200, car_html, "https://www.ss.lv/msg/lv/transport/cars/bmw/x5/aaaaaa.html",
          "https://www.ss.lv/msg/lv/transport/cars/audi/q7/aaaaaa.html"), "expired"),
        ("explicit removed notice (RU)",
         (200, notice_html, live_url, live_url), "expired"),
        ("buy-ad (Pērku, no spec table, no redirect)",
         (200, buyad_html, "https://www.ss.lv/msg/lv/transport/cars/audi/a4/zzqxzq.html",
          "https://www.ss.lv/msg/lv/transport/cars/audi/a4/zzqxzq.html"), "unknown"),
        ("blocked / captcha page",
         (200, "just a moment... are you a robot? captcha" + pad, live_url, live_url), "unknown"),
        ("transport error / timeout (status 0, no final url)",
         (0, "__transport_error__ Timeout", live_url, ""), "unknown"),
        ("http 429 rate limited",
         (429, "", live_url, live_url), "unknown"),
        ("same listing but scheme/trailing-slash differ -> NOT a redirect -> alive",
         (200, live_html, live_url, "http://www.ss.lv/msg/lv/transport/cars/audi/a3/ddffm.html/"),
         "alive"),
    ]
    ok = True
    for name, args, expected in cases:
        status, html, req, fin = args
        verdict, reason = classify(status, html, ss, req_url=req, final_url=fin)
        good = (verdict == expected)
        ok = ok and good
        print(("  [PASS] " if good else "  [FAIL] ") + name
              + f": got {verdict!r} ({reason})" + ("" if good else f"  EXPECTED {expected!r}"))
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


PRICE_PATS = {
    "ss.lv":     [r"Cena:\s*</td>\s*<td[^>]*>\s*([\d\s]+)\s*&#8364;", r"Cena:[^\d]{0,40}([\d\s]{3,12})\s*€"],
    # Anchor to the announcement-price block and require the exact class="price". The old
    # r'class="[^"]*price[^"]*"...' matched ANY price-ish class (price-previous / struck / ex-VAT),
    # so it grabbed the wrong number (e.g. a Toyota RAV4 whose real price is 48 500 got an 11 500
    # history point). The real price's euro sign sits in a SEPARATE tag, so allow '<' as terminator.
    "autoplius": [r'announcement-price[\s\S]{0,400}?class="price"[^>]*>\s*([\d\s\xa0]{3,12})\s*(?:€|<)'],
    # auto24 is handled specially in extract_price() - see _extract_auto24(). It must NOT
    # use a bare "<digits> €" pattern: auto24 writes the ASKING price as "EUR 162,700"
    # (currency code BEFORE the number, no euro sign) in the og:description meta and in the
    # labelled "Price"/"Bargain price" element, while a bare "<digits> €" appears ONLY in
    # NOISE - the "loovutuspakett 199 €" handover-fee line, the "sisaldab 7300€
    # registreerimistasu" registration-fee sentence, the "alates 199 €/kuus" monthly
    # financing figure and the related-listings sidebar. The old r'([\d\s]{3,12})\s*€'
    # grabbed the first of those and produced the €199 Mercedes cluster and the €7,300
    # Porsche Cayenne. (fixed 2026-07-22)
    "auto24":    [],
}

# auto24 asking price lives here, as "EUR 62,900":
#   <meta property="og:description" content="2024, ..., 19 400 km, EUR 62,900 - used ...">
# and on-page as  "Price EUR 64,900" / "Bargain price EUR 62,900".
_A24_OGDESC = re.compile(
    r'<meta[^>]+og:description[^>]+content=["\'][^"\']*?EUR\D{0,6}([\d][\d,\s]{2,})', re.I)
_A24_LABEL = re.compile(
    r'(?:bargain\s+price|price|m[üu][üu]gihind|hind)\D{0,40}EUR\D{0,6}([\d][\d,.\s]{2,})', re.I)

# auto24 renders the space after "EUR" as a non-breaking space; in raw HTML that is a
# DIGIT-BEARING entity ("EUR&#160;73,000") that would otherwise be mis-read as "160".
# Normalise every nbsp form to a plain space before matching so only the real price digits
# remain between "EUR" and the number.
_A24_NBSP = re.compile(r'&nbsp;|&#160;|&#xa0;|\xa0', re.I)

# numbers sitting next to any of these are a fee / financing / rate figure, never the price
_A24_FEE_PHRASES = ("/kuus", "/kuu", "/mēn", "/men", "/mo", "kuumakse",
                    "registreerimis", "sisaldab", "loovutuspakett")

# a real car is never under 300 EUR; the €199/€110/€150 rows are all fee/financing artefacts
A24_PRICE_FLOOR = 300
PRICE_CEILING = 900000


def _sane_car_price(v, floor=250):
    """Reject parse artefacts so a bad parse can NEVER overwrite a good price or invent a
    price-history entry. Mirrors sane_price() in the collector; auto24 uses a 300 floor."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if floor <= v <= PRICE_CEILING else None


def _num(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d.isdigit() else None


def _extract_auto24(html):
    """auto24 asking price only, from the labelled EUR value - never a bare '<digits> €'."""
    html = _A24_NBSP.sub(" ", html or "")
    for pat in (_A24_OGDESC, _A24_LABEL):
        m = pat.search(html)
        if not m:
            continue
        # guard: the matched number must not be part of a fee / financing phrase
        tail = (html[m.end(): m.end() + 12] or "").lower()
        if any(fp in tail for fp in _A24_FEE_PHRASES):
            continue
        v = _sane_car_price(_num(m.group(1)), floor=A24_PRICE_FLOOR)
        if v:
            return v
    return None


_AP_PRICE_RE = re.compile(r'class="price"[^>]*>\s*([\d][\d\s\xa0]*)', re.I)
def _ap_price(html):
    """Autoplius asking price from the FIRST <div class="announcement-price"> block ONLY,
    mirroring the collector's proven _ap_price() in balticradar.py. The old 400-char / euro-
    anchored PRICE_PATS regex could latch onto the struck-through 'price-previous', the ex-VAT
    'bez PVN' figure, a leasing monthly, or a partner-ad price -- that is exactly how a real
    99 450 EUR Porsche Cayenne got re-priced to 54 450 EUR. Anchoring to the announcement-price
    block and taking its first class="price" number matches what the collector already stores."""
    i = html.find('announcement-price')
    seg = html[i:i + 1500] if i >= 0 else html
    m = _AP_PRICE_RE.search(seg)
    if m:
        return _sane_car_price(_num(m.group(1)))
    return None

def extract_price(html, src):
    if src == "auto24":
        return _extract_auto24(html)
    if src == "autoplius":
        return _ap_price(html)
    for p in PRICE_PATS.get(src, []):
        m = re.search(p, html)
        if m:
            d = re.sub(r"\D", "", m.group(1))
            if d.isdigit():
                v = int(d)
                if 250 <= v <= PRICE_CEILING:
                    return v
    return None


# ---------------------------------------------------------------- worker
def worker(src):
    cfg = SOURCES[src]
    st = STATE["sources"][src] = {
        "checked": 0, "expired": 0, "alive": 0, "unknown": 0, "price_changed": 0,
        "blocked": False, "last_batch": None, "oldest_last_seen": None, "cycle_rows": 0,
        "oldest_last_checked": None, "benign_unknown": 0, "unknown_batches": 0,
        "last_batch_unknown_pct": None,
    }
    consec = 0
    smoke_left = SMOKE if SMOKE else None

    try:
        br = Browser(src)
    except Exception as e:
        log(src, "!!!! CANNOT START BROWSER:", repr(e))
        log(src, "     run:  python -m playwright install chromium")
        return

    while True:
        try:
            rows = fetch_batch(src, BATCH)
            if not rows:
                log(src, "no rows returned - sleeping 60s")
                time.sleep(60)
                continue
            st["last_batch"] = now_iso()
            st["oldest_last_seen"] = rows[0].get("last_seen")
            st["oldest_last_checked"] = rows[0].get("last_checked")
            # THE number to watch: if oldest_last_checked does not advance between batches,
            # the cursor is stuck and the worker is re-checking the same rows forever.
            log(src, f"batch of {len(rows)} | oldest last_checked {rows[0].get('last_checked')} "
                     f"| oldest last_seen {rows[0].get('last_seen')} "
                     f"| totals checked={st['checked']} expired={st['expired']} "
                     f"alive={st['alive']} unknown={st['unknown']}"
                     + (f" mileage_filled={st.get('mileage_filled',0)} price_filled={st.get('price_filled',0)}" if BACKFILL_MILEAGE else ""))

            b_alive = b_expired = b_unknown = 0
            for row in rows:
                url = row.get("source_url") or ""
                if not url:
                    continue
                time.sleep(cfg["delay"] + random.uniform(0, 0.4))
                status, html, viewlimit, final_url = br.get(url)

                if viewlimit:
                    # per-IP view limit: NOT an expiry, and not something to push through.
                    st["viewlimits"] = st.get("viewlimits", 0) + 1
                    log(src, f"!! VIEW LIMIT hit ({st['viewlimits']} so far). Nothing marked. "
                             f"Sleeping {VIEWLIMIT_SLEEP // 60} min, then continuing.")
                    save_state()
                    time.sleep(VIEWLIMIT_SLEEP)
                    continue

                verdict, reason = classify(status, html, cfg, req_url=url, final_url=final_url)
                st["checked"] += 1
                if smoke_left is not None:
                    smoke_left -= 1

                if verdict == "unknown":
                    st["unknown"] += 1
                    b_unknown += 1
                    # ADVANCE THE CURSOR EVEN THOUGH WE LEARNED NOTHING.
                    # This is the fix for the infinite loop: without it, this row stays at
                    # the head of the queue and is re-fetched on every batch, forever.
                    # It writes last_checked ONLY - never active, never last_seen - so an
                    # UNKNOWN still cannot be mistaken for alive or for an expiry.
                    touch_checked(src, row["car_id"])
                    transport = reason.startswith("http") or reason.startswith("blocked")
                    if not transport:          # e.g. an ss.lv "Perkam" ad - benign, not a block
                        st["benign_unknown"] = st.get("benign_unknown", 0) + 1
                        consec = 0
                        continue
                    consec += 1
                    log(src, f"UNKNOWN ({reason}) {url[:70]}  [consec={consec}]")
                    if consec in (3, 5):
                        time.sleep(20 * consec)
                    if consec >= MAX_CONSEC_BLOCKS:
                        st["blocked"] = True
                        save_state()
                        log(src, "!!!! BLOCKED: %d consecutive transport failures. "
                                 "NOTHING was marked dead (a block is never an expiry). "
                                 "Cooling down %d min, then retrying." % (consec, BLOCK_COOLDOWN // 60))
                        time.sleep(BLOCK_COOLDOWN)
                        consec = 0
                        st["blocked"] = False
                        break
                    continue

                consec = 0

                if verdict == "expired":
                    st["expired"] += 1
                    b_expired += 1
                    log(src, f"EXPIRED {row['car_id']} ({reason}) {url[:70]}")
                    pay = {"active": False, "last_seen": now_iso()}
                    if HAVE_LAST_CHECKED:
                        pay["last_checked"] = now_iso()
                    patch_car(src, row["car_id"], pay)
                    continue

                st["alive"] += 1
                b_alive += 1
                payload = {"last_seen": now_iso()}
                if HAVE_LAST_CHECKED:
                    payload["last_checked"] = now_iso()
                price = extract_price(html, src)
                _oldp = row.get("last_price")
                # ANTI-CORRUPTION GUARD (tightened 2026-08-01): never let a re-parse drop a car's
                # price below 60% of its stored value. A >40% single-step drop is almost always a
                # mis-parse (struck / ex-VAT / monthly / partner figure), not a real price move -
                # this is the class that turned a 99 450 EUR Cayenne into 54 450 EUR and invented a
                # fake price-history drop. On a suspicious drop we KEEP the good stored price and
                # record nothing. Increases and moderate (<40%) drops still apply normally.
                if price and price != _oldp:
                    if _oldp and price < 0.60 * _oldp:
                        st["price_held"] = st.get("price_held", 0) + 1
                        log(src, f"price HELD {row['car_id']}: parsed {price} is <60% of stored "
                                 f"{_oldp} -> kept {_oldp} (suspected mis-parse, nothing written)")
                    else:
                        st["price_changed"] += 1
                        payload["last_price"] = price
                        add_price_history(row["car_id"], price, row.get("last_mileage"))
                if src == "auto24":
                    nb = a24_body_from_html(html)          # 'suv' when auto24 says SUV, else None
                    if nb and nb != row.get("body"):
                        st["body_fixed"] = st.get("body_fixed", 0) + 1
                        payload["body"] = nb
                if BACKFILL_MILEAGE and src == "autoplius" and row.get("last_mileage") is None:
                    # FILL-ONLY: recover mileage/price lost in the broken backfill. Never overwrites a
                    # real value (guarded on IS NULL), never guesses (only sane, in-range parser output;
                    # a failed/blocked parse leaves the row NULL). Reuses the now-fixed collector parser.
                    try:
                        from autoplius_parse import parse_listing
                        pl = parse_listing(html, source_url=url)
                        mk = pl.get("mileage_km")
                        if isinstance(mk, int) and 100 <= mk <= 2_000_000:
                            payload["last_mileage"] = mk
                            st["mileage_filled"] = st.get("mileage_filled", 0) + 1
                        if row.get("last_price") is None and "last_price" not in payload:
                            pe = pl.get("price_eur")
                            if isinstance(pe, int) and 250 <= pe <= 500000:
                                payload["last_price"] = pe
                                st["price_filled"] = st.get("price_filled", 0) + 1
                    except Exception as _e:
                        log(src, f"backfill parse skip {row['car_id']}: {_e!r}")
                patch_car(src, row["car_id"], payload)

                if smoke_left is not None and smoke_left <= 0:
                    break

            # ---------------------------------------------------------- LOUD FAILURE
            # Silent 95 %-unknown for days is the failure mode this project keeps hitting
            # (ss.lv ran 7 days at expired=0 and nobody noticed). Make it impossible to miss.
            b_done = b_alive + b_expired + b_unknown
            if b_done:
                pct = 100.0 * b_unknown / b_done
                st["last_batch_unknown_pct"] = round(pct, 1)
                if pct >= 50.0:
                    st["unknown_batches"] = st.get("unknown_batches", 0) + 1
                    log(src, "!" * 70)
                    log(src, "!! ALERT: %.0f%% of this batch was UNKNOWN (%d/%d). "
                             "alive=%d expired=%d" % (pct, b_unknown, b_done, b_alive, b_expired))
                    log(src, "!! Nothing was marked dead. Either the classifier's markers no "
                             "longer match this site, or the catalogue is full of rows that "
                             "can never classify (e.g. ss.lv 'Perku' wanted-to-buy ads).")
                    log(src, "!! %d such batches so far. Go look at an actual failing URL." %
                             st["unknown_batches"])
                    log(src, "!" * 70)
            if not HAVE_LAST_CHECKED:
                log(src, "!! WARNING: running WITHOUT cars.last_checked - the queue cursor "
                         "is NOT advancing past UNKNOWN rows. This worker is looping. "
                         "Run the migration in balticradar_db_fix.sql.")

            save_state()
            if smoke_left is not None and smoke_left <= 0:
                rate = 100.0 * st["expired"] / max(st["checked"], 1)
                log(src, f"SMOKE DONE checked={st['checked']} expired={st['expired']} "
                         f"alive={st['alive']} unknown={st['unknown']} dead_rate={rate:.1f}%")
                br.close()
                return

        except Exception as e:
            log(src, "!!!! WORKER CRASH:", repr(e))
            with _print_lock:
                traceback.print_exc()
            time.sleep(60)


def main():
    STATE["started"] = now_iso()
    mode = "DRY RUN (writes nothing)" if DRY_RUN else "LIVE (soft-hides expired listings)"
    log("main", "=" * 60)
    log("main", f"BalticRadar ROLLING revalidate | {mode} | batch={BATCH}"
                + (f" | SMOKE={SMOKE}" if SMOKE else ""))
    log("main", "direct fetches from the home IP - no proxy, no ScraperAPI")
    log("main", "order: " + " -> ".join(ORDER) + "  (one thread per host, they run concurrently)")
    log("main", "=" * 60)

    threads = []
    for src in ORDER:
        t = threading.Thread(target=worker, args=(src,), name=src, daemon=False)
        t.start()
        threads.append(t)
        time.sleep(2)          # stagger the starts

    for t in threads:
        t.join()
    log("main", "all workers exited")


if __name__ == "__main__":
    if os.environ.get("SELFTEST") == "1":
        # No network, no threads: just prove the classifier decisions and exit.
        sys.exit(_selftest_classify())
    try:
        main()
    except KeyboardInterrupt:
        log("main", "stopped by user")
