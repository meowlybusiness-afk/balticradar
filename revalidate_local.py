#!/usr/bin/env python3
"""
BalticRadar revalidator - LOCAL runner (runs on the PC, not in GitHub Actions).
=============================================================================
WHY THIS EXISTS
    autoplius (LT) and auto24 (EE) hard-block GitHub's datacenter IP, and there is no
    proxy secret in the repo (and we do NOT want one - the password would have to be
    typed into a form). The DataImpulse proxy already lives on THIS PC in `proxy.txt`.

HOW IT AVOIDS RE-INVENTING THE PROXY (which has broken twice)
    It does not build the proxy at all. It imports `balticradar` and calls that module's
    OWN `browser_session()` - the exact code path the collector uses successfully today,
    including the `user__cr.<country>;sessid.<id>` sticky-session username format and the
    BLOCK_IMAGES routing that strips images/CSS/fonts so proxy bandwidth stays tiny.
    Importing balticradar also loads SUPABASE_URL + SUPABASE_KEY (balticradar_key.txt)
    and finds proxy.txt on its own. No credential is ever typed anywhere.

    The verdict logic is NOT duplicated either: it imports `revalidate` and reuses the
    same SOURCES / classify() / patch_car() - so the safety rules are identical:
        EXPIRED -> active=false (soft hide; the row and its price history stay forever)
        ALIVE   -> refresh last_seen
        UNKNOWN -> touch nothing. A block is never an expiry. Nothing is ever deleted.

USAGE (just double-click one of the .bat files, or:)
    set ONLY_SOURCE=autoplius & set BATCH=300 & set SAMPLE=random & python revalidate_local.py
"""
import os, sys, time, random, traceback

os.environ.setdefault("DRY_RUN", "1")          # SAFE BY DEFAULT: writes nothing unless told otherwise
os.environ.setdefault("ONLY_SOURCE", "autoplius")
os.environ.setdefault("BATCH", "300")
os.environ.setdefault("SAMPLE", "oldest")      # oldest = stale tail | random = whole catalogue
os.environ.setdefault("BLOCK_IMAGES", "1")     # keep residential proxy bandwidth tiny
os.environ.setdefault("PROXY_CC", "lt")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import balticradar as BR      # noqa: E402  -> loads Supabase creds + the WORKING proxy session
import revalidate as RV       # noqa: E402  -> same classifier + same safety rules as CI

SELFTEST = [u.strip() for u in (os.environ.get("SELFTEST") or "").split(",") if u.strip()]
PAGE_TIMEOUT = int(os.environ.get("PAGE_TIMEOUT_MS", "45000"))


def log(*a):
    print(*a, flush=True)


def fetch(page, url):
    """-> (status, html). Playwright through the DataImpulse proxy."""
    try:
        resp = page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        html = page.content() or ""
        return status, html
    except Exception as e:
        log(f"    fetch error: {e!r}")
        return 0, ""


def main():
    src = os.environ["ONLY_SOURCE"]
    cfg = RV.SOURCES.get(src)
    if not cfg:
        log(f"unknown source {src!r}; expected one of {list(RV.SOURCES)}")
        return 2

    log("=" * 78)
    log(f"BalticRadar LOCAL revalidate | source={src} | DRY_RUN={RV.DRY_RUN} "
        f"| batch={RV.BATCH} | sample={RV.SAMPLE}")
    log("=" * 78)

    with BR.browser_session(headless=True) as page:

        # ---- PROVE THE PROXY WORKS before touching a single row -----------------
        if SELFTEST:
            log("\n--- SELFTEST (writes nothing) ---")
            ok = True
            for url in SELFTEST:
                status, html = fetch(page, url)
                verdict, reason = RV.classify(status, html, cfg)
                log(f"  {verdict.upper():<8} http={status:<4} len={len(html):<7} {reason}")
                log(f"           {url}")
                if status != 200 and status not in (404, 410):
                    ok = False
            if not ok:
                log("\n  !! proxy/transport is NOT working (no 200s). Fix that before running a batch.")
                return 3
            log("--- SELFTEST OK: the proxy fetches real pages ---\n")
            if os.environ.get("SELFTEST_ONLY") == "1":
                return 0

        rows = RV.fetch_batch(RV.BATCH)
        log(f"batch: {len(rows)} rows")
        if rows and RV.SAMPLE != "random":
            log(f"oldest last_seen in batch: {rows[0].get('last_seen')}")

        st = {"checked": 0, "expired": 0, "alive": 0, "unknown": 0, "price_changed": 0}
        consec_unknown = 0

        for i, row in enumerate(rows, 1):
            if (row.get("source") or "") != src:
                continue
            url = row.get("source_url") or ""
            if not url:
                continue

            time.sleep(cfg["delay"] + random.uniform(0, 0.4))
            status, html = fetch(page, url)
            verdict, reason = RV.classify(status, html, cfg)
            st["checked"] += 1

            log(f"[{i}/{len(rows)}] {verdict.upper():<8} http={status:<4} len={len(html):<7} "
                f"{reason:<46} {url[:66]}")

            if verdict == "unknown":
                st["unknown"] += 1
                consec_unknown += 1
                if consec_unknown >= 8:
                    log("  !! 8 consecutive UNKNOWNs -> the source is blocking us. ABORTING "
                        "(a block is NEVER an expiry; nothing was changed).")
                    break
                continue
            consec_unknown = 0

            if verdict == "expired":
                st["expired"] += 1
                RV.patch_car(row["car_id"], {"active": False, "last_seen": RV.now_iso()})
                continue

            st["alive"] += 1
            payload = {"last_seen": RV.now_iso()}
            price = RV.extract_price(html, src)
            if price and price != row.get("last_price"):
                st["price_changed"] += 1
                payload["last_price"] = price
                RV.add_price_history(row["car_id"], price, row.get("last_mileage"))
            RV.patch_car(row["car_id"], payload)

    rate = (100.0 * st["expired"] / st["checked"]) if st["checked"] else 0.0
    log("\n" + "=" * 78)
    log(f"RESULT  {src}  sample={RV.SAMPLE}")
    log(f"  checked={st['checked']}  expired={st['expired']}  alive={st['alive']}  "
        f"unknown={st['unknown']}  price_changed={st['price_changed']}")
    log(f"  EXPIRY RATE = {rate:.1f}%")
    if RV.DRY_RUN:
        log("  DRY_RUN=1 -> nothing was written. Use the LIVE .bat to apply.")
    log("=" * 78)

    try:
        RV.write_run_log({"by_source": {src: st}, "total": st, "local": True}, ok=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        traceback.print_exc()
        log(f"CRASHED: {e!r}")
        sys.exit(1)
