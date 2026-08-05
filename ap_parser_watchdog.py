#!/usr/bin/env python3
"""BalticRadar autoplius list-parser WATCHDOG  (self-healing pipeline: DETECT -> ALERT -> hand off).

The free "2b" route fills autoplius mileage straight off the results page (ap_list_rich in
balticradar.py), dodging autoplius' per-IP detail-page VIEW LIMIT. The ONE thing that breaks it is
autoplius changing their results-page HTML: ap_list_rich then stops finding the <span>NNN km</span>
cell, new autoplius cars go NULL again, and the backfill (br-ap-backfill) silently stalls.

The parser DEGRADES SAFELY -- when the markup changes it writes NULL, never a wrong number -- so this
watchdog's job is fast DETECTION and packaging the evidence a fixer needs, NOT damage control.

It does NOT fetch autoplius itself -- a cold request gets CF-challenged (403), which would be constant
false alarms. Instead the running backfill service (br-ap-backfill, WD_DUMP=1) drops its freshest
successfully-fetched list page at health/ap_last_list.html every few seconds; the watchdog reads THAT.
So it tests the real parser against a real, live page, and a stale/missing dump itself signals that the
fetch path (not the parser) is down.

Each run it:
  1. reads the freshest dumped list page (health/ap_last_list.html),
  2. runs the REAL ap_list_rich on it (so it tests exactly what production uses),
  3. measures mileage-yield = ads-with-mileage / ads-total (and price-yield as a second canary),
  4. classifies:  OK  |  PARSER_BROKEN  |  FETCH_STALE  |  NO_ADS ,
  5. on any not-OK: saves the sample (the exact thing a fixer reads) + a diagnostic,
     writes health/parser_status.json, and emails an alert if RESEND_API_KEY is set.

The distinction matters to whoever fixes it:
  * PARSER_BROKEN  -> page + ads are fine but mileage vanished  => markup changed, fix the regex.
  * FETCH_STALE    -> no fresh page dumped (backfill down / CF / IP block) => not a parser bug.
Deliberately NOT auto-rewriting+deploying the parser: a correct fix needs a human/Claude to LOOK at
the new HTML; a blind auto-guess could write WRONG mileage, which is worse than NULL. See README.
"""
import os, re, json, time, datetime, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
HEALTH = os.path.join(HERE, "health")
os.makedirs(HEALTH, exist_ok=True)

DUMP = os.path.join(HEALTH, "ap_last_list.html")                   # written by br-ap-backfill (WD_DUMP=1)
MIN_MILEAGE_YIELD = float(os.environ.get("WD_MIN_YIELD", "0.55"))   # sample runs ~0.95; alarm well below
MIN_ADS          = int(os.environ.get("WD_MIN_ADS", "8"))
MAX_DUMP_AGE_MIN = int(os.environ.get("WD_MAX_DUMP_AGE_MIN", "30")) # older than this => fetch path is down

def now(): return datetime.datetime.utcnow().isoformat() + "Z"

def load_parser():
    """Import the LIVE ap_list_rich from balticradar.py (env set so it doesn't prompt for a key)."""
    os.environ.setdefault("SUPABASE_URL", "https://wrilvoukvyubgpomuoyn.supabase.co")
    if not os.environ.get("SUPABASE_KEY"):
        try: os.environ["SUPABASE_KEY"] = open(os.path.join(HERE, "balticradar_key.txt")).read().strip()
        except Exception: pass
    spec = importlib.util.spec_from_file_location("br_live", os.path.join(HERE, "balticradar.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m.ap_list_rich

def read_dump():
    """Read the freshest list page the backfill dumped. Returns (html_or_None, note). A missing or
    stale dump means the FETCH path (not the parser) is down -- report that, don't blame the regex."""
    if not os.path.exists(DUMP):
        return None, "no dump yet (is br-ap-backfill running with WD_DUMP=1?)"
    age_min = (time.time() - os.path.getmtime(DUMP)) / 60
    try:
        html = open(DUMP).read()
    except Exception as e:
        return None, f"unreadable: {e!r}"
    if age_min > MAX_DUMP_AGE_MIN:
        return None, f"stale dump ({age_min:.0f} min old > {MAX_DUMP_AGE_MIN})"
    if len(html) < 5000:
        return None, f"dump too small ({len(html)}B)"
    return html, f"ok ({age_min:.1f} min old, {len(html)}B)"

def run():
    html, fnote = read_dump()
    status, yield_m, yield_p, n_ads, sample = "OK", None, None, 0, None
    if html is None:
        status = "FETCH_STALE"
    else:
        ads = load_parser()(html)["ads"]
        n_ads = len(ads)
        if n_ads < MIN_ADS:
            status = "NO_ADS"
        else:
            wm = sum(1 for a in ads if a.get("mileage_km"))
            wp = sum(1 for a in ads if a.get("price_eur"))
            yield_m, yield_p = wm / n_ads, wp / n_ads
            if yield_m < MIN_MILEAGE_YIELD:
                status = "PARSER_BROKEN"
    if status != "OK":                       # capture the exact evidence a fixer will need
        sample = os.path.join(HEALTH, f"ap_list_sample_{status}.html")
        if html is not None:
            try: open(sample, "w").write(html)
            except Exception: pass
    report = {"ts": now(), "status": status, "mileage_yield": yield_m, "price_yield": yield_p,
              "ads": n_ads, "min_yield": MIN_MILEAGE_YIELD, "fetch": fnote, "sample": sample}
    json.dump(report, open(os.path.join(HEALTH, "parser_status.json"), "w"), indent=2)
    with open(os.path.join(HEALTH, "watchdog.log"), "a") as f:
        f.write(json.dumps(report) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    if status != "OK":
        alert(report)
    return 0 if status == "OK" else 2

def alert(report):
    """Notify on a not-OK status. Telegram first (free, phone push, no domain/key ceremony); Resend email
    as a fallback if TG isn't configured. Both are optional -- with neither set, the status file + log are
    still written, so nothing is lost. Sending here costs ZERO Claude tokens (plain HTTP from the VPS)."""
    msg = (f"⚠️ BalticRadar watchdog: autoplius list-parser {report['status']}\n"
           f"mileage_yield={report['mileage_yield']} (min {report['min_yield']}), "
           f"price_yield={report['price_yield']}, ads={report['ads']}\n"
           f"fetch: {report['fetch']}\nts: {report['ts']}\n"
           f"If PARSER_BROKEN: autoplius changed the results-page markup -> fix the mileage regex in "
           f"ap_list_rich against {report['sample']}. If FETCH_STALE: backfill/CF/IP, not a parser bug.")
    sent = False
    tok, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if tok and chat:
        try:
            import requests
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              json={"chat_id": chat, "text": msg}, timeout=20)
            if r.ok: print("alert -> telegram", flush=True); sent = True
            else:    print("telegram send failed:", r.status_code, r.text[:200], flush=True)
        except Exception as e:
            print("telegram send FAILED:", repr(e), flush=True)
    key = os.environ.get("RESEND_API_KEY")
    if not sent and key:
        try:
            import requests
            requests.post("https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"from": "alerts@balticradar.com", "to": [os.environ.get("WD_ALERT_TO", "meowlybusiness@gmail.com")],
                      "subject": f"[BalticRadar] autoplius parser {report['status']}",
                      "html": msg.replace(chr(10), "<br>")}, timeout=20)
            print("alert -> email", flush=True); sent = True
        except Exception as e:
            print("alert email FAILED:", repr(e), flush=True)
    if not sent:
        print("ALERT (no TG/email channel configured, logged only):", report["status"], flush=True)

if __name__ == "__main__":
    raise SystemExit(run())
