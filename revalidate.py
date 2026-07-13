#!/usr/bin/env python3
"""
BalticRadar revalidator
=======================
Walks listings OLDEST last_seen FIRST, re-fetches each source_url and decides:

  EXPIRED  -> active = false   (SOFT HIDE. Row + price_history stay forever.)
  ALIVE    -> refresh last_seen, and update price/mileage (feeding price_history)
  UNKNOWN  -> touch nothing at all (blocked, timeout, 5xx, CAPTCHA...)

It NEVER deletes a row. There is no DELETE statement anywhere in this file.

Why this exists
---------------
`last_seen` was never a liveness signal: the collector only crawls NEW listings,
so last_seen goes stale for everything regardless of whether the car is still for
sale. The old time-based DEACTIVATE rule therefore produced FALSE POSITIVES --
verified: ss.lv .../mercedes/s300/hckmc.html and auto24 /vehicles/4019670 were
both marked active=false while still live at the source.

This job replaces guessing-by-age with actually asking the source.

Safety
------
* DRY_RUN=1  -> reports what it *would* deactivate, writes nothing. Default ON.
* UNKNOWN is never treated as expired. A block is not an expiry.
* Per-source circuit breaker: consecutive blocks -> back off, then abandon source.
* Plain HTTP GET of the HTML only -- no browser, so images/CSS/fonts are never
  fetched. Each check is a few KB.
"""

import os, sys, time, json, random, traceback
from datetime import datetime, timezone
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]          # service role - bypasses RLS
SCRAPER_KEY  = os.environ.get("SCRAPER_KEY", "")   # existing ScraperAPI key (optional)
PROXY_URL    = os.environ.get("PROXY_URL", "")     # e.g. DataImpulse residential (optional)

DRY_RUN      = os.environ.get("DRY_RUN", "1") == "1"
BATCH        = int(os.environ.get("BATCH", "420"))   # ~10k/day over 24 hourly runs
ONLY_SOURCE  = os.environ.get("ONLY_SOURCE", "")     # optional: ss.lv | autoplius | auto24

SB = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ----------------------------------------------------------------------------
# Per-source config: pacing + how the source says "this ad is dead"
# ----------------------------------------------------------------------------
# Markers verified by hand against real dead listings (2026-07-13).
SOURCES = {
    "ss.lv": {
        "delay": 1.4,                 # seconds between requests to this host
        "proxy": False,
        "dead_markers": [
            "sludinājums ir neaktīvs",
            "sludinajums ir neaktivs",
            "beidzies derīguma termiņš",
            "beidzies deriguma termins",
            "sludinājums ir dzēsts",
            "sludinājums ir arhivēts",
        ],
        # a live ss.lv car page always shows a price line
        "alive_markers": ["cena:"],
    },
    "autoplius": {
        "delay": 2.2,                 # slowest: this is the one that IP-blocked us
        "proxy": True,                # route via residential proxy when available
        "dead_markers": [
            "sludinājums nav aktīvs",   # lv.autoplius.lt  (VERIFIED)
            "sludinajums nav aktivs",
            "skelbimas neaktyvus",      # lt locale
            "objavlenie neaktivno",
        ],
        "alive_markers": [],
    },
    "auto24": {
        "delay": 1.4,
        "proxy": False,
        "dead_markers": [
            "this advertisement is not active",
            "advertisement has expired",
            "kuulutus ei ole aktiivne",
            "kuulutus on aegunud",
            "objavlenie neaktivno",
        ],
        "alive_markers": [],
    },
}

BLOCK_MARKERS = [
    "captcha", "cloudflare", "are you a robot", "access denied",
    "too many requests", "just a moment", "enable javascript and cookies",
    "exhausted api credits", "rate limit",
]

MAX_CONSEC_BLOCKS = 8   # per source -> circuit breaker


def log(*a):
    print(*a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------
# Supabase helpers
# ----------------------------------------------------------------------------
SEL = "car_id,source,source_url,last_price,last_mileage,last_seen"


def fetch_batch(limit):
    """
    Oldest last_seen first.

    Ideally served by:  create index cars_active_lastseen_idx on cars(active, last_seen asc);
    Without that index the ORDER BY does a seq-scan + sort over 93k rows and
    PostgREST kills it with 57014 'statement timeout'. So: try the ordered query,
    and if it times out fall back to an unordered slice of sufficiently-stale rows.
    The fallback still only ever picks rows we haven't confirmed recently, which is
    all the job actually needs.
    """
    base = f"{SUPABASE_URL}/rest/v1/cars?select={SEL}&active=is.true"
    if ONLY_SOURCE:
        base += f"&source=eq.{ONLY_SOURCE}"

    r = requests.get(f"{base}&order=last_seen.asc&limit={limit}", headers=SB, timeout=90)
    if r.status_code == 200:
        return r.json()
    log(f"  ordered query failed ({r.status_code}: {r.text[:90]}) -> using stale-cutoff fallback."
        f"\n  Create index cars_active_lastseen_idx on cars(active,last_seen) to fix properly.")

    # fallback: anything not confirmed alive in the last 2 days, unordered (no sort)
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    r = requests.get(f"{base}&last_seen=lt.{cutoff}&limit={limit}", headers=SB, timeout=90)
    r.raise_for_status()
    return r.json()


def patch_car(car_id, payload):
    if DRY_RUN:
        return True
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/cars?car_id=eq.{car_id}",
        headers={**SB, "Prefer": "return=minimal"},
        data=json.dumps(payload), timeout=45)
    if r.status_code >= 300:
        log(f"  !! patch failed {car_id}: {r.status_code} {r.text[:160]}")
        return False
    return True


def add_price_history(car_id, price, mileage):
    if DRY_RUN:
        return
    requests.post(
        f"{SUPABASE_URL}/rest/v1/price_history",
        headers={**SB, "Prefer": "return=minimal"},
        data=json.dumps({"car_id": car_id, "price": price,
                         "mileage": mileage, "ts": now_iso()}), timeout=45)


def write_run_log(stats, ok, err=""):
    """Best-effort run log so silent failures become visible."""
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/revalidate_runs",
            headers={**SB, "Prefer": "return=minimal"},
            data=json.dumps({
                "ts": now_iso(), "dry_run": DRY_RUN, "ok": ok,
                "stats": stats, "error": err[:2000],
            }), timeout=30)
    except Exception as e:
        log("run-log write failed (non-fatal):", repr(e))


# ----------------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------------
def build_proxies(use_proxy):
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def get_html(url, cfg):
    """
    Returns (status, html, mode).
    Plain GET only -- no browser -> no images/CSS/fonts are ever downloaded.
    Escalates to ScraperAPI only if the direct hit looks blocked.
    """
    proxies = build_proxies(cfg["proxy"])
    headers = {"User-Agent": UA, "Accept-Language": "lv,en;q=0.8"}
    try:
        r = requests.get(url, headers=headers, proxies=proxies,
                         timeout=30, allow_redirects=True)
        html = r.text or ""
        if r.status_code == 404 or r.status_code == 410:
            return r.status_code, html, "direct"
        if r.status_code == 200 and not looks_blocked(html):
            return 200, html, "direct"
        blocked_status = r.status_code
    except Exception as e:
        blocked_status = 0
        html = ""

    if SCRAPER_KEY:
        try:
            r = requests.get("https://api.scraperapi.com/",
                             params={"api_key": SCRAPER_KEY, "url": url,
                                     "country_code": "eu"},
                             timeout=70)
            if r.status_code == 200 and not looks_blocked(r.text):
                return 200, r.text, "scraperapi"
            return r.status_code, r.text or "", "scraperapi"
        except Exception:
            pass
    return blocked_status, html, "blocked"


def looks_blocked(html):
    low = (html or "").lower()
    if len(low) < 400:
        return True
    return any(m in low for m in BLOCK_MARKERS)


# ----------------------------------------------------------------------------
# Decision
# ----------------------------------------------------------------------------
def classify(status, html, cfg):
    """-> ('expired'|'alive'|'unknown', reason)"""
    if status in (404, 410):
        return "expired", f"http {status}"
    if status != 200:
        return "unknown", f"http {status}"
    low = (html or "").lower()
    if looks_blocked(html):
        return "unknown", "blocked/captcha"
    for m in cfg["dead_markers"]:
        if m in low:
            return "expired", f'marker "{m}"'
    for m in cfg["alive_markers"]:
        if m in low:
            return "alive", "alive marker"
    if cfg["alive_markers"]:
        # source has a reliable alive marker and it is missing -> suspicious,
        # but we refuse to guess. Never deactivate on absence alone.
        return "unknown", "no alive marker"
    return "alive", "no dead marker"


PRICE_RE = None
def extract_price(html, source):
    """Cheap price re-read so price_history keeps building. Best effort only."""
    import re
    low = html
    pats = {
        "ss.lv":     [r"Cena:\s*</td>\s*<td[^>]*>\s*([\d\s]+)\s*&#8364;", r"Cena:[^\d]{0,40}([\d\s]{3,12})\s*€"],
        "autoplius": [r'class="[^"]*price[^"]*"[^>]*>\s*([\d\s]{3,12})\s*€'],
        "auto24":    [r'([\d\s]{3,12})\s*€'],
    }
    for p in pats.get(source, []):
        m = re.search(p, low)
        if m:
            digits = re.sub(r)\D", "", m.group(1))
            if digits.isdigit():
                v = int(digits)
                if 100 <= v <= 500000:
                    return v
    return None


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    rows = fetch_batch(BATCH)
    log(f"=== BalticRadar revalidate | DRY_RUN={DRY_RUN} | batch={len(rows)} ===")
    if rows:
        log(f"    oldest last_seen in batch: {rows[0].get('last_seen')}")

    stats = {}
    consec_blocks = {}
    abandoned = set()
    last_hit = {}

    for i, row in enumerate(rows, 1):
        src = row.get("source") or "?"
        cfg = SOURCES.get(src)
        url = row.get("source_url") or ""
        if not cfg or not url:
            continue
        if src in abandoned:
            continue

        st = stats.setdefault(src, {"checked": 0, "expired": 0, "alive": 0,
                                    "unknown": 0, "price_changed": 0})

        # ---- pacing: never hammer a host ----
        gap = time.time() - last_hit.get(src, 0)
        wait = cfg["delay"] - gap
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))
        last_hit[src] = time.time()

        status, html, mode = get_html(url, cfg)
        verdict, reason = classify(status, html, cfg)
        st["checked"] += 1

        if verdict == "unknown":
            st["unknown"] += 1
            consec_blocks[src] = consec_blocks.get(src, 0) + 1
            n = consec_blocks[src]
            if n in (3, 5):
                back = 20 * n
                log(f"  ~ {src}: {n} consecutive blocks -> backing off {back}s")
                time.sleep(back)
            if n >= MAX_CONSEC_BLOCKS:
                log(f"  !! {src}: {n} consecutive blocks -> ABANDONING source this run")
                abandoned.add(src)
            continue

        consec_blocks[src] = 0

        if verdict == "expired":
            st["expired"] += 1
            log(f"  [{i}/{len(rows)}] EXPIRED {src} {row['car_id']} ({reason}) {url[:70]}")
            patch_car(row["car_id"], {"active": False, "last_seen": now_iso()})
            continue

        # alive
        st["alive"] += 1
        payload = {"last_seen": now_iso()}
        price = extract_price(html, src)
        if price and price != row.get("last_price"):
            st["price_changed"] += 1
            payload["last_price"] = price
            add_price_history(row["car_id"], price, row.get("last_mileage"))
        patch_car(row["car_id"], payload)

    # ---- report ----
    log("\n=== RESULT ===")
    tot = {"checked": 0, "expired": 0, "alive": 0, "unknown": 0, "price_changed": 0}
    for src, s in sorted(stats.items()):
        rate = (100.0 * s["expired"] / s["checked"]) if s["checked"] else 0.0
        log(f"  {src:<10} checked={s['checked']:<5} expired={s['expired']:<5} "
            f"alive={s['alive']:<5} unknown={s['unknown']:<5} "
            f"price_changed={s['price_changed']:<4} expiry_rate={rate:.1f}%")
        for k in tot:
            tot[k] += s[k]
    rate = (100.0 * tot["expired"] / tot["checked"]) if tot["checked"] else 0.0
    log(f"  {'TOTAL':<10} checked={tot['checked']:<5} expired={tot['expired']:<5} "
        f"alive={tot['alive']:<5} unknown={tot['unknown']:<5} expiry_rate={rate:.1f}%")
    if DRY_RUN:
        log("\n  DRY_RUN=1 -> nothing was written. Set DRY_RUN=0 to apply.")
    if abandoned:
        log(f"  ABANDONED (blocked): {', '.join(sorted(abandoned))}")

    write_run_log({"by_source": stats, "total": tot,
                   "abandoned": sorted(abandoned)}, ok=True)

    # Fail loudly if a source got blocked out entirely - otherwise this silently
    # does nothing forever and nobody notices.
    if abandoned:
        log("\n::error::revalidate: source(s) blocked -> " + ", ".join(sorted(abandoned)))
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        write_run_log({}, ok=False, err=repr(e))
        log(f"::error::revalidate crashed: {e!r}")
        sys.exit(1)
