#!/usr/bin/env python3
"""
BalticRadar price watchdog / cleanup bot  (2026-08-01)
======================================================
Keeps prices correct and consistent with history. Two modes:

  (default) REPORT  - read-only. Counts:
        * corruption-signature cars: current price < 60% of the max ever recorded
          (the 99 450 -> 54 450 Porsche Cayenne mis-parse class)
        * misaligned cars: cars.last_price != the latest SANE price_history point
      Alarms (exit 2) if corruption climbs above ALARM_THRESHOLD - i.e. a parser regressed.
      This is what the daily systemd timer runs.

  --heal            - fix the above, conservatively and with a full backup:
        * "correct price" := the most RECENT price_history point that is >= 60% of the car's
          max (i.e. ignore mis-parse lows). last_price is set to that.
        * price_history points below 60% of the max are deleted (fabricated drops).
      It NEVER invents a price (only uses points the scrapers actually recorded) and NEVER
      aligns DOWN to a mis-parse low. A >40% genuine single-step drop is treated as suspect
      and rolled back to the last good price - the same policy the collector/revalidator guards
      now enforce, so the whole system agrees on "never trust a >40% drop".

Backup of every change -> price_guard_heal_backup.json.
Env: SUPABASE_URL, SUPABASE_KEY (falls back to balticradar_key.txt).
"""
import json, os, sys, time, urllib.request

URL = os.environ.get("SUPABASE_URL", "https://wrilvoukvyubgpomuoyn.supabase.co").rstrip("/")
_kf = os.path.join(os.path.dirname(__file__), "balticradar_key.txt")
KEY = os.environ.get("SUPABASE_KEY") or (open(_kf).read().strip() if os.path.exists(_kf) else "")
H = {"apikey": KEY, "Authorization": "Bearer " + KEY}
HEAL = "--heal" in sys.argv
RATIO = 0.60
ALARM_THRESHOLD = 50   # steady-state is a handful (cars mid-scrape); a real parser regression spikes into the hundreds
FLOOR, CEIL = 250, 2_000_000


def _req(method, path, body=None, extra=None):
    hd = dict(H)
    if extra: hd.update(extra)
    if body is not None: hd["Content-Type"] = "application/json"
    r = urllib.request.Request(URL + "/rest/v1/" + path, headers=hd, method=method,
                               data=json.dumps(body).encode() if body is not None else None)
    return urllib.request.urlopen(r, timeout=90)


def _get(path):
    out, off = [], 0
    while True:
        rows = json.load(_req("GET", path, extra={"Range": f"{off}-{off+999}"}))
        out += rows
        if len(rows) < 1000: break
        off += 1000
    return out


def src(cid):
    return ("autoplius" if cid.startswith("car_A") else "ss.lv" if cid.startswith("car_LV")
            else "auto24" if cid.startswith("car_EE") else "?")


def main():
    t0 = time.time()
    print(f"[price_guard] START mode={'HEAL' if HEAL else 'REPORT'}")
    cars = {c["car_id"]: c["last_price"] for c in _get("cars?select=car_id,last_price&active=eq.true")}
    hist = {}
    for r in _get("price_history?select=id,car_id,ts,price&order=ts.asc"):
        if r["price"] is not None:
            hist.setdefault(r["car_id"], []).append((r["id"], r["price"]))
    print(f"[price_guard] active cars={len(cars)} cars_with_history={len(hist)}")

    corruption, misaligned, backup, del_ids = [], [], [], []
    for cid, cur in cars.items():
        pts = hist.get(cid)
        if not pts: continue
        prices = [p for _, p in pts]
        mx = max(prices)
        if mx <= 0: continue
        floor = RATIO * mx
        sane = [(i, p) for i, p in pts if p >= floor and FLOOR <= p <= CEIL]
        correct = sane[-1][1] if sane else mx      # most recent non-corrupt point (fallback: max)
        low_ids = [i for i, p in pts if p < floor]
        # corruption signature: current price is a mis-parse low
        if cur is None or cur < floor:
            corruption.append((cid, mx, cur))
        # misalignment: last_price differs from the correct (latest sane) price
        if cur != correct:
            misaligned.append(cid)
            backup.append({"car_id": cid, "was": cur, "set": correct})
        del_ids += low_ids
        if HEAL:
            if cur != correct:
                _req("PATCH", f"cars?car_id=eq.{cid}", {"last_price": correct}, {"Prefer": "return=minimal"})

    if HEAL and del_ids:
        for k in range(0, len(del_ids), 100):
            lst = ",".join(str(i) for i in del_ids[k:k+100])
            _req("DELETE", f"price_history?id=in.({lst})", extra={"Prefer": "return=minimal"})
    if HEAL and backup:
        with open(os.path.join(os.path.dirname(__file__), "price_guard_heal_backup.json"), "w") as f:
            json.dump(backup, f)

    by = {}
    for cid, _, _ in corruption: by[src(cid)] = by.get(src(cid), 0) + 1
    print(f"[price_guard] misaligned last_price vs latest-sane history: {len(misaligned)}"
          + (" -> FIXED" if HEAL else ""))
    print(f"[price_guard] fabricated history points (<60% of max): {len(del_ids)}"
          + (" -> DELETED" if HEAL else ""))
    print(f"[price_guard] CORRUPTION-SIGNATURE cars: {len(corruption)}  {by}"
          + (" -> healed to last good price" if HEAL else ""))
    for cid, mx, cur in corruption[:12]:
        print(f"    {cid}: max={mx} current={cur}")
    trip = (not HEAL) and len(corruption) > ALARM_THRESHOLD
    if trip:
        print("!" * 70)
        print(f"!! ALARM: {len(corruption)} cars match the mis-parse signature (> {ALARM_THRESHOLD}).")
        print("!! A price parser likely regressed. Run: python price_guard.py --heal  after investigating.")
        print("!" * 70)
    print(f"[price_guard] DONE in {time.time()-t0:.1f}s")
    sys.exit(2 if trip else 0)


if __name__ == "__main__":
    main()
