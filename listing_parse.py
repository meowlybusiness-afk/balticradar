"""
BalticRadar - Supabase storage backend (the scalable replacement for the
in-memory Store + listings.json). Implements the ad_id/car_id identity rule
against Postgres via Supabase REST (PostgREST).

CREDENTIALS: never hard-coded. Set on YOUR machine before running:
    setx SUPABASE_URL "https://xxxx.supabase.co"
    setx SUPABASE_KEY "<service_role key>"     # service key, kept private to you
Run schema.sql first (Supabase -> SQL Editor).
"""
import os, requests, datetime
from autoplius_parse import fingerprint
from identity import strong_signal_match, specs_match, price_sane

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_KEY", "")
H = {"apikey":KEY, "Authorization":f"Bearer {KEY}", "Content-Type":"application/json"}
_S = requests.Session(); _S.headers.update(H)

import time as _t
def _retry(fn, tries=4):
    last=None
    for i in range(tries):
        try: return fn()
        except requests.exceptions.RequestException as e:
            last=e; _t.sleep(1.5*(i+1))
    raise last

def _now(): return datetime.datetime.utcnow().isoformat()+"Z"
def _chk():
    if not URL or not KEY:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY environment variables first.")

def _get(table, params):
    def go():
        r = _S.get(f"{URL}/rest/v1/{table}", params=params, timeout=30)
        r.raise_for_status(); return r.json()
    return _retry(go)

def _post(table, rows, upsert=False):
    h = dict(H)
    if upsert: h["Prefer"] = "resolution=merge-duplicates"
    def go():
        r = _S.post(f"{URL}/rest/v1/{table}", headers=h, json=rows, timeout=30)
        r.raise_for_status(); return r
    return _retry(go)

def _patch(table, params, body):
    def go():
        _S.patch(f"{URL}/rest/v1/{table}", params=params, json=body, timeout=30).raise_for_status()
    _retry(go)

def has_ad(ad_id):
    return bool(_get("ads", {"ad_id":f"eq.{ad_id}", "select":"ad_id", "limit":"1"}))

def candidates(f):
    """Active cars sharing the core specs (small set the matcher inspects)."""
    p = {"active":"eq.true", "select":"*", "limit":"25"}
    for k in ("make","model","year","engine_cc"):
        if f.get(k) not in (None, ""): p[k] = f"eq.{f[k]}"
    return _get("cars", p)

def ingest(f):
    """Apply identity rule and persist. Returns the decision string."""
    _chk()
    if has_ad(f["ad_id"]):
        return "SAME_AD"
    cands = [c for c in candidates(f) if specs_match(f, c)]
    for car in cands:
        sig = strong_signal_match(f, car)
        if sig and price_sane(f.get("price_eur"), car.get("last_price")):
            cid = car["car_id"]
            _post("ads", [{"ad_id":f["ad_id"],"car_id":cid,"source":f.get("source"),
                           "source_url":f.get("source_url"),"active":True,
                           "first_seen":_now(),"last_seen":_now()}], upsert=True)
            if f.get("price_eur")!=car.get("last_price") or f.get("mileage_km")!=car.get("last_mileage"):
                _post("price_history", [{"car_id":cid,"ts":_now(),
                       "price":f.get("price_eur"),"mileage":f.get("mileage_km")}])
            _patch("cars", {"car_id":f"eq.{cid}"},
                   {"active":True,"last_price":f.get("price_eur"),
                    "last_mileage":f.get("mileage_km"),"last_seen":_now()})
            return "REPOST"
    if cands:  # specs match but no strong signal -> human confirms
        _post("review_queue", [{"ad_id":f["ad_id"],"fingerprint":fingerprint(f)[0],
               "reason":"specs match, no VIN/owner/photo signal","payload":f}], upsert=True)
        return "NEEDS_REVIEW"
    # genuinely new car
    cid = f"car_{f['ad_id']}"
    _post("cars", [{"car_id":cid,"fingerprint":fingerprint(f)[0],"source":f.get("source"),
        "country":f.get("country"),"make":f.get("make"),"model":f.get("model"),
        "year":f.get("year"),"engine_cc":f.get("engine_cc"),"engine_l":f.get("engine_l"),
        "power_kw":f.get("power_kw"),"fuel":f.get("fuel"),"gearbox":f.get("gearbox"),
        "body":f.get("body"),"drivetrain":f.get("drivetrain"),"color":f.get("color"),
        "owner_code":f.get("owner_code"),"vin_prefix":f.get("vin_prefix"),
        "location":f.get("location"),"photos":f.get("photos") or [],
        "source_url":f.get("source_url"),"description":f.get("description"),
        "last_price":f.get("price_eur"),"last_mileage":f.get("mileage_km"),
        "active":True,"first_seen":_now(),"last_seen":_now()}], upsert=True)
    _post("ads", [{"ad_id":f["ad_id"],"car_id":cid,"source":f.get("source"),
        "source_url":f.get("source_url"),"active":True,
        "first_seen":_now(),"last_seen":_now()}], upsert=True)
    _post("price_history", [{"car_id":cid,"ts":_now(),
        "price":f.get("price_eur"),"mileage":f.get("mileage_km")}])
    return "NEW_CAR"
